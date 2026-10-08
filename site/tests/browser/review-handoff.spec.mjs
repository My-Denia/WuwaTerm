import { test, expect } from '@playwright/test';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { makeChoice, serializeWorkfile } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const A = { source: '今汐。', target: 'Jinhsi.', direction: 'en' };
const B = { source: '声骸。', target: 'Echo.', direction: 'en' };

async function open(page) {
  const reviews = [];
  const gate = { next: null };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: {
      status: 'available', translation_enabled: true,
      terms: { used: 0, limit: 100, remaining: 100 },
      translations: { used: 0, limit: 100, remaining: 100 },
      characters: { used: 0, limit: 10000, remaining: 10000 },
      reviews: { used: 0, limit: 60, remaining: 60 },
      reset_at: '2099-01-01T00:00:00Z',
    } });
    if (path === '/api/translations') {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: { kind: 'exact', text: body.text === A.source ? A.target : B.target,
        direction: body.to ?? 'en', dictionary_miss: false, request_id: 'synthetic-translation' } });
    }
    if (path === '/api/reviews') {
      const body = route.request().postDataJSON();
      reviews.push(body);
      const pending = gate.next; gate.next = null;
      if (pending) await pending;
      return route.fulfill({ json: freshApiReport(fixture(body.source, body.target, body.direction).report) });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  // Proves hydration and effects ran before the workbench is edited.
  // Warm-up gate, not the behavior under test: in built mode the worker
  // cold-start can push the first pool read past the default 5s on shared
  // CI runners, so allow a generous wait here.
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  return { reviews, gate };
}

async function translate(page, draft = B) {
  await page.locator('#translation-source').fill(draft.source);
  await page.getByRole('button', { name: '翻译整句', exact: true }).click();
  await expect(page.locator('.translated-text')).toHaveText(draft.target);
}

async function downloadText(page, button) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: button, exact: true }).click();
  const stream = await (await waiting).createReadStream();
  const parts = [];
  for await (const part of stream) parts.push(part);
  return Buffer.concat(parts).toString();
}

async function workfile(page, button = '保存稿件') {
  return JSON.parse(await downloadText(page, button));
}

async function prepare(page) {
  await page.locator('#review-source').fill(A.source);
  await page.locator('#review-target').fill(A.target);
  await page.locator('.alignment-panel > summary').click();
  await page.getByRole('button', { name: '确认全文为一个对应段', exact: true }).click();
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-current')).toBeVisible();
  await page.getByRole('button', { name: '采用此官方词对', exact: true }).click();
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
}

test('empty reception focuses review without a review request', async ({ page }) => {
  const { reviews } = await open(page);
  await translate(page);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('#review-target')).toHaveValue(B.target);
  await expect(page.locator('#review-source')).toBeFocused();
  await expect(page.getByRole('dialog')).toBeHidden();
  expect(reviews).toHaveLength(0);
});

test('post-mount work survives cancel and identical send; replacement and undo restore associated work', async ({ page }) => {
  const { reviews } = await open(page);
  await prepare(page);
  const before = await workfile(page);
  await translate(page);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '替换审校稿件？' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: '保留当前稿件' })).toBeFocused();
  await dialog.getByRole('button', { name: '保留当前稿件' }).click();
  expect(await workfile(page)).toEqual(before);

  await translate(page, A);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(await workfile(page)).toEqual(before);
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  await expect(page.locator('.alignment-row')).toHaveCount(1);
  await expect(page.locator('.review-findings .term-result')).toHaveCount(1);

  await translate(page);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await dialog.getByRole('button', { name: '替换稿件', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.choice-records')).toHaveCount(0);
  await expect(page.locator('.review-findings')).toHaveCount(0);
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  expect(await workfile(page)).toEqual(before);
  expect((await workfile(page, '导出当前结果')).report_is_current).toBe(false);
  await expect(page.locator('.review-current')).toHaveCount(0);
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  await expect(page.locator('.alignment-row')).toHaveCount(1);
  await expect(page.locator('.review-findings .term-result')).toHaveCount(1);
  // An older undo entry survives the replacement as well.
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('.choice-records')).toHaveCount(0);
  await expect(page.locator('#review-source')).toHaveValue(A.source);
  expect(reviews).toHaveLength(1);
});

test('identical send and cancelled replacement keep a running check alive', async ({ page }) => {
  const { reviews, gate } = await open(page);
  await prepare(page);
  await translate(page, A);
  let release;
  gate.next = new Promise(resolve => { release = resolve; });
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews.length).toBe(2);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await expect(page.getByRole('button', { name: '核对中…', exact: true })).toBeVisible();
  await expect(page.getByRole('dialog')).toBeHidden();
  await translate(page);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await page.getByRole('button', { name: '保留当前稿件' }).click();
  await expect(page.getByRole('button', { name: '核对中…', exact: true })).toBeVisible();
  release();
  await expect(page.locator('.review-current')).toBeVisible();
  await expect(page.locator('#review-source')).toHaveValue(A.source);
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  expect(reviews).toHaveLength(2);
});

for (const restoreBeforeResponse of [false, true]) test(`late old response cannot overwrite ${restoreBeforeResponse ? 'the restored draft' : 'the replacement'}`, async ({ page }) => {
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = (input, init) => {
      if (new URL(String(input), location.href).pathname === '/api/reviews') {
        window.oldReviewSignal = init.signal;
        return original(input, { ...init, signal: undefined });
      }
      return original(input, init);
    };
  });
  const { reviews, gate } = await open(page);
  await prepare(page);
  const before = await workfile(page);
  await translate(page);
  let release;
  gate.next = new Promise(resolve => { release = resolve; });
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews.length).toBe(2);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await page.getByRole('button', { name: '替换稿件', exact: true }).click();
  expect(await page.evaluate(() => window.oldReviewSignal.aborted)).toBe(true);
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  if (restoreBeforeResponse) await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  const response = page.waitForResponse('**/api/reviews');
  release();
  await (await response).finished();
  if (!restoreBeforeResponse) {
    await expect(page.locator('.review-findings')).toHaveCount(0);
    await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  }
  expect(await workfile(page)).toEqual(before);
  expect((await workfile(page, '导出当前结果')).report_is_current).toBe(false);
  expect(reviews).toHaveLength(2);
});

test('confirmation snapshots a report that finished while the decision was open', async ({ page }) => {
  const { reviews, gate } = await open(page);
  await prepare(page);
  await translate(page);
  let release;
  gate.next = new Promise(resolve => { release = resolve; });
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews.length).toBe(2);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  release();
  // Inspect DOM directly because a modal makes the background inert.
  await expect(page.locator('.review-current')).toBeVisible();
  await page.getByRole('button', { name: '替换稿件', exact: true }).click();
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  const restored = await workfile(page);
  expect(restored.history).toHaveLength(2);
  expect(restored.choices).toHaveLength(1);
  expect(restored.alignments).toHaveLength(1);
  expect((await workfile(page, '导出当前结果')).report_is_current).toBe(false);
  expect(reviews).toHaveLength(2);
});

test('invalid events leave the edited workbench unchanged', async ({ page }) => {
  const { reviews } = await open(page);
  await prepare(page);
  const before = await workfile(page);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('wuwaterm-send-review', {
    detail: { source: 'new', target: 42, direction: 'en' },
  })));
  expect(await workfile(page)).toEqual(before);
  await expect(page.getByRole('dialog')).toBeHidden();
  expect(reviews).toHaveLength(1);
});

test('undo preserves imported choice provenance instead of treating it as a new local confirmation', async ({ page }) => {
  const { reviews } = await open(page);
  const saved = fixture(A.source, A.target, A.direction);
  const choice = makeChoice({ ...saved, finding: saved.report.findings[0], candidate: null });
  const history = [{ ...A, alignments: null, report: saved.report, resolutions: [] }];
  const content = serializeWorkfile({ ...A, alignments: null, choices: [choice], history });
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'draft.json', mimeType: 'application/json', buffer: Buffer.from(content) });
  await expect(page.locator('.choice-records')).toContainText('待确认');
  const before = await workfile(page);
  await translate(page);
  await page.getByRole('button', { name: '送去审校', exact: true }).click();
  await page.getByRole('button', { name: '替换稿件', exact: true }).click();
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  expect(await workfile(page)).toEqual(before);
  expect(reviews).toHaveLength(0);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-summary')).toContainText('规则 review-v2');
  await expect(page.locator('.choice-records')).toContainText('0 处适用 / 1 处待确认');
  expect(reviews).toHaveLength(1);
  expect(reviews[0].resolutions).toBeUndefined();
});

function savedManuscript(draft = B) {
  const saved = fixture(draft.source, draft.target, draft.direction);
  return serializeWorkfile({ ...draft, alignments: null, choices: [],
    history: [{ ...draft, alignments: null, report: saved.report, resolutions: [] }] });
}

async function importManuscript(page, content) {
  await page.getByLabel('选择稿件文件').setInputFiles({ name: 'draft.json', mimeType: 'application/json', buffer: Buffer.from(content) });
}

for (const origin of ['checked', 'imported']) test(`import undo restores both ${origin} reports and the full manuscript without certifying it`, async ({ page }, info) => {
  const { reviews } = await open(page);
  await prepare(page);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-current')).toBeVisible();
  if (origin === 'imported') {
    await importManuscript(page, JSON.stringify(await workfile(page)));
    await expect(page.locator('.choice-records')).toContainText('待确认');
  }
  const before = await workfile(page);
  expect(before.history).toHaveLength(2);
  await importManuscript(page, savedManuscript());
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  const restored = await workfile(page);
  await info.attach('restored-manuscript', { body: JSON.stringify({ before, restored }), contentType: 'application/json' });
  expect(restored).toEqual(before);
  const result = await workfile(page, '导出当前结果');
  expect(result.report_is_current).toBe(false);
  expect(result.sentence_meaning_evaluated).toBe(false);
  expect(result.verified_stamp.valid).toBe(false);
  expect(result.report).toEqual(before.history.at(-1));
  const markdown = await downloadText(page, '导出 Markdown 报告');
  expect(markdown).toContain(A.source);
  expect(markdown).not.toContain(B.source);
  expect(markdown).toContain(origin === 'imported' ? '导入的历史报告' : '需要重新核对');
  await expect(page.locator('.review-current')).toHaveCount(0);
  if (origin === 'imported') await expect(page.locator('.choice-records')).toContainText('待确认');
  // The earlier ordinary choice undo entry still exists beneath both imports.
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  if (origin === 'imported') await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('.choice-records')).toHaveCount(0);
  expect(reviews).toHaveLength(2);
});

test('import undo clears imported reports when the previous manuscript had none', async ({ page }) => {
  const { reviews } = await open(page);
  await page.locator('#review-source').fill(A.source);
  await page.locator('#review-target').fill(A.target);
  const before = await workfile(page);
  expect(before.history).toEqual([]);
  await importManuscript(page, savedManuscript());
  await expect(page.locator('.review-findings')).toBeVisible();
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  expect(await workfile(page)).toEqual(before);
  await expect(page.locator('.review-findings')).toHaveCount(0);
  expect((await workfile(page, '导出当前结果')).report).toBeNull();
  expect(reviews).toHaveLength(0);
});

test('import undo snapshots reports completed during the pending file read', async ({ page }) => {
  await page.addInitScript(() => {
    const original = File.prototype.text;
    File.prototype.text = function () {
      const read = () => original.call(this);
      window.importReadStarted = true;
      return new Promise(resolve => { window.finishImportRead = async () => resolve(await read()); });
    };
  });
  const { reviews, gate } = await open(page);
  await prepare(page);
  let release;
  gate.next = new Promise(resolve => { release = resolve; });
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews.length).toBe(2);
  await importManuscript(page, savedManuscript());
  await expect.poll(() => page.evaluate(() => window.importReadStarted)).toBe(true);
  release();
  await expect(page.locator('.review-current')).toBeVisible();
  const acceptedTime = await workfile(page);
  expect(acceptedTime.history).toHaveLength(2);
  await page.evaluate(() => window.finishImportRead());
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  expect(await workfile(page)).toEqual(acceptedTime);
  expect((await workfile(page, '导出当前结果')).report_is_current).toBe(false);
  expect(reviews).toHaveLength(2);
});

// The synthetic responder proves client request/local currency behavior only;
// it does not evaluate the engine's not_a_term verdict.
test('a narrow keyboard resume fetches fresh basis before explicitly reconfirming not_a_term and rechecking', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { reviews } = await open(page);
  const saved = fixture(A.source, A.target, A.direction);
  const choice = makeChoice({ ...saved, finding: saved.report.findings[0], candidate: null });
  await importManuscript(page, serializeWorkfile({ ...A, alignments: null, choices: [choice], history: [{ ...A, alignments: null, report: saved.report, resolutions: [] }] }));
  await expect(page.locator('.choice-records')).toContainText('0 处适用 / 1 处待确认');
  expect(reviews).toHaveLength(0);
  const fresh = page.getByRole('button', { name: '仅获取新依据', exact: true });
  await fresh.focus();
  await page.keyboard.press('Enter');
  // Imported history already displays its rule; wait for the fresh check.
  await expect(page.locator('.review-current')).toBeVisible();
  expect(reviews).toHaveLength(1);
  expect(reviews[0].resolutions).toBeUndefined();
  await expect(page.locator('.choice-records')).toContainText('0 处适用 / 1 处待确认');
  const confirm = page.getByRole('button', { name: '这里不是术语', exact: true });
  await confirm.focus();
  await page.keyboard.press('Enter');
  await expect(page.locator('.choice-records')).toContainText('1 处适用 / 0 处待确认');
  expect(reviews).toHaveLength(1);
  const check = page.getByRole('button', { name: '核对术语', exact: true });
  await check.focus();
  await page.keyboard.press('Enter');
  await expect.poll(() => reviews.length).toBe(2);
  await expect(page.locator('.review-current')).toBeVisible();
  expect(reviews[1].resolutions).toEqual([{ mention_id: '0:2:今汐', choice: 'not_a_term' }]);
  expect(reviews[1].resolution_context.matcher_revision).toMatch(/^[0-9a-f]{64}$/u);
  const result = await workfile(page, '导出当前结果');
  expect(result.sentence_meaning_evaluated).toBe(false);
  expect(result.verified_stamp.valid).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
