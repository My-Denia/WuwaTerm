// Only aggregate admission windows belong here. No Request/headers/visitor keys.
export const POOL_LIMITS = Object.freeze({
  upstreamPerMinute: 6,
  translationsPerMinute: 1,
  termsPerDay: 240,
  translationsPerDay: 30,
  charactersPerDay: 12_000,
  metaPerDay: 60,
  reviewsPerDay: 60,
});
const ACQUIRE_TIMEOUT_MS = 1_000;
const HEADERS = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', 'x-robots-tag': 'noindex, nofollow, noarchive' };

function response(status, body, retry) {
  return new Response(JSON.stringify(body), { status, headers: { ...HEADERS, ...(retry ? { 'retry-after': String(retry) } : {}) } });
}
function deny(reason, retry = 0) {
  const status = reason.endsWith('_exhausted') || reason === 'shared_pool_busy' ? 429 : 503;
  return { ok: false, response: response(status, { status: 'unavailable', reason, ...(retry ? { retry_after_seconds: retry } : {}) }, retry) };
}
function configured(environment) {
  return environment?.WUWATERM_SHARED_POOL_ENABLED === 'true'
    && ['true', 'false'].includes(environment?.WUWATERM_TRANSLATION_ENABLED)
    && typeof environment?.DB?.prepare === 'function';
}

// A single SQLite statement owns all checks and increments. No read-before-write
// or multi-counter partial reservation. unixepoch is evaluated by the database.
const SQL = `INSERT INTO shared_pool
  (id, second_key, minute_key, day_key, upstream_used, translation_minute_used, terms_used, translation_used, character_used, meta_used, review_used)
  VALUES (1, unixepoch(), unixepoch()/60, unixepoch()/86400, 1, ?1, ?2, ?1, ?3, ?4, ?11)
  ON CONFLICT(id) DO UPDATE SET
    second_key=excluded.second_key, minute_key=excluded.minute_key, day_key=excluded.day_key,
    upstream_used=(CASE WHEN minute_key=excluded.minute_key THEN upstream_used ELSE 0 END)+1,
    translation_minute_used=(CASE WHEN minute_key=excluded.minute_key THEN translation_minute_used ELSE 0 END)+?1,
    terms_used=(CASE WHEN day_key=excluded.day_key THEN terms_used ELSE 0 END)+?2,
    translation_used=(CASE WHEN day_key=excluded.day_key THEN translation_used ELSE 0 END)+?1,
    character_used=(CASE WHEN day_key=excluded.day_key THEN character_used ELSE 0 END)+?3,
    meta_used=(CASE WHEN day_key=excluded.day_key THEN meta_used ELSE 0 END)+?4,
    review_used=(CASE WHEN day_key=excluded.day_key THEN review_used ELSE 0 END)+?11
  WHERE second_key < excluded.second_key
    AND minute_key <= excluded.minute_key AND day_key <= excluded.day_key
    AND (CASE WHEN minute_key=excluded.minute_key THEN upstream_used ELSE 0 END) < ?5
    AND (?1=0 OR (CASE WHEN minute_key=excluded.minute_key THEN translation_minute_used ELSE 0 END) < ?6)
    AND (?2=0 OR (CASE WHEN day_key=excluded.day_key THEN terms_used ELSE 0 END) < ?7)
    AND (?1=0 OR (CASE WHEN day_key=excluded.day_key THEN translation_used ELSE 0 END) < ?8)
    AND (?1=0 OR (CASE WHEN day_key=excluded.day_key THEN character_used ELSE 0 END)+?3 <= ?9)
    AND (?4=0 OR (CASE WHEN day_key=excluded.day_key THEN meta_used ELSE 0 END) < ?10)
    AND (?11=0 OR (CASE WHEN day_key=excluded.day_key THEN review_used ELSE 0 END) < ?12)
  RETURNING *, unixepoch() AS clock`;

const SNAPSHOT_SQL = `SELECT unixepoch() AS clock,
  COALESCE(second_key,0) AS second_key, COALESCE(minute_key,0) AS minute_key, COALESCE(day_key,0) AS day_key,
  COALESCE(upstream_used,0) AS upstream_used, COALESCE(translation_minute_used,0) AS translation_minute_used,
  COALESCE(terms_used,0) AS terms_used, COALESCE(translation_used,0) AS translation_used,
  COALESCE(character_used,0) AS character_used, COALESCE(meta_used,0) AS meta_used,
  COALESCE(review_used,0) AS review_used
  FROM (SELECT 1) LEFT JOIN shared_pool ON id=1`;

function validRow(row) {
  if (!row || typeof row !== 'object') return false;
  return ['clock', 'second_key', 'minute_key', 'day_key', 'upstream_used', 'translation_minute_used', 'terms_used', 'translation_used', 'character_used', 'meta_used', 'review_used']
    .every(key => Number.isSafeInteger(row[key]) && row[key] >= 0)
    && row.clock > 0
    && row.upstream_used <= POOL_LIMITS.upstreamPerMinute
    && row.translation_minute_used <= POOL_LIMITS.translationsPerMinute
    && row.terms_used <= POOL_LIMITS.termsPerDay
    && row.translation_used <= POOL_LIMITS.translationsPerDay
    && row.character_used <= POOL_LIMITS.charactersPerDay
    && row.meta_used <= POOL_LIMITS.metaPerDay
    && row.review_used <= POOL_LIMITS.reviewsPerDay;
}

async function bounded(operation) {
  // This deadline bounds dispatch, not D1 cancellation. A late reservation may
  // still commit after rejection; it stays charged, with no refund or retry.
  let timer;
  const started = performance.now();
  try {
    const result = await Promise.race([
      operation(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(Error('quota_timeout')), ACQUIRE_TIMEOUT_MS); }),
    ]);
    if (performance.now() - started >= ACQUIRE_TIMEOUT_MS) throw Error('stale_quota');
    return result;
  } finally { clearTimeout(timer); }
}

function snapshot(row, environment) {
  const sameDay = row.day_key === Math.floor(row.clock / 86400);
  const sameMinute = row.minute_key === Math.floor(row.clock / 60);
  const daily = (field, limit) => ({ used: sameDay ? row[field] : 0, limit, remaining: Math.max(0, limit - (sameDay ? row[field] : 0)) });
  return {
    status: 'available', shared: true, fairness_guaranteed: false,
    translation_enabled: environment.WUWATERM_TRANSLATION_ENABLED === 'true',
    terms: daily('terms_used', POOL_LIMITS.termsPerDay),
    translations: daily('translation_used', POOL_LIMITS.translationsPerDay),
    characters: daily('character_used', POOL_LIMITS.charactersPerDay),
    reviews: daily('review_used', POOL_LIMITS.reviewsPerDay),
    upstream: { used: sameMinute ? row.upstream_used : 0, limit: POOL_LIMITS.upstreamPerMinute },
    reset_at: new Date((Math.floor(row.clock / 86400) + 1) * 86400_000).toISOString(),
    retry_after_seconds: 60 - row.clock % 60,
  };
}

export async function admitRequest(environment, kind, characters = 0) {
  if (!configured(environment)) return deny('shared_pool_unavailable');
  if (!['terms', 'translations', 'meta', 'review'].includes(kind) || !Number.isSafeInteger(characters) || characters < 0 || characters > 2000 || (kind === 'translations' && characters === 0) || (kind === 'review' && characters !== 0)) return deny('shared_pool_unavailable');
  if (kind === 'translations' && environment.WUWATERM_TRANSLATION_ENABLED !== 'true') return deny('translation_disabled');
  try {
    const row = await bounded(() => environment.DB.prepare(SQL).bind(
      Number(kind === 'translations'), Number(kind === 'terms'), kind === 'translations' ? characters : 0, Number(kind === 'meta'),
      POOL_LIMITS.upstreamPerMinute, POOL_LIMITS.translationsPerMinute, POOL_LIMITS.termsPerDay,
      POOL_LIMITS.translationsPerDay, POOL_LIMITS.charactersPerDay, POOL_LIMITS.metaPerDay,
      Number(kind === 'review'), POOL_LIMITS.reviewsPerDay,
    ).first());
    if (row !== null) {
      if (!validRow(row) || row.second_key !== row.clock || row.minute_key !== Math.floor(row.clock / 60) || row.day_key !== Math.floor(row.clock / 86400)) return deny('shared_pool_unavailable');
      return { ok: true };
    }
    // A denial read classifies the message only. It can never grant admission.
    const current = await bounded(() => environment.DB.prepare(SNAPSHOT_SQL).first());
    if (!validRow(current)) return deny('shared_pool_unavailable');
    const s = snapshot(current, environment); const untilDay = 86400 - current.clock % 86400;
    if (kind === 'translations' && (!s.translations.remaining || s.characters.remaining < characters)) return deny('translation_pool_exhausted', untilDay);
    if (kind === 'terms' && !s.terms.remaining) return deny('terms_pool_exhausted', untilDay);
    const sameDay = current.day_key === Math.floor(current.clock / 86400);
    if (kind === 'meta' && sameDay && current.meta_used >= POOL_LIMITS.metaPerDay) return deny('meta_pool_exhausted', untilDay);
    if (kind === 'review' && sameDay && current.review_used >= POOL_LIMITS.reviewsPerDay) return deny('reviews_pool_exhausted', untilDay);
    const minuteFull = current.minute_key === Math.floor(current.clock / 60)
      && (current.upstream_used >= POOL_LIMITS.upstreamPerMinute
        || (kind === 'translations' && current.translation_minute_used >= POOL_LIMITS.translationsPerMinute));
    if (current.second_key > current.clock) return deny('shared_pool_unavailable');
    return deny('shared_pool_busy', minuteFull ? s.retry_after_seconds : 1);
  } catch { return deny('shared_pool_unavailable'); }
}

export async function poolStatus(environment) {
  if (!configured(environment)) return deny('shared_pool_unavailable').response;
  try {
    const row = await bounded(() => environment.DB.prepare(SNAPSHOT_SQL).first());
    if (!validRow(row)) return deny('shared_pool_unavailable').response;
    return response(200, snapshot(row, environment));
  } catch { return deny('shared_pool_unavailable').response; }
}
