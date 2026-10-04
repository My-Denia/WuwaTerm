import { test, expect } from '@playwright/test';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const POOL = {
  status: 'available',
  translation_enabled: true,
  terms: { used: 0, limit: 100, remaining: 100 },
  translations: { used: 0, limit: 100, remaining: 100 },
  characters: { used: 0, limit: 10000, remaining: 10000 },
  reset_at: '2099-01-01T00:00:00Z',
};

const OLD = {
  schema_version: '2',
  source_commit: 'abc123',
  game_version: null,
  resource_version: null,
  changelist: null,
  term_count: 10951,
  request_id: 'req-old',
};

const REPORTED = {
  schema_version: '2',
  source_commit: '9218d612ad815e398e064e577e42aaf878899968',
  game_version: '3.7.0',
  resource_version: '3.7.8',
  changelist: '8975829',
  term_count: 11329,
  request_id: 'req-new',
};

const UNRECORDED = {
  ...REPORTED,
  game_version: 'unavailable',
  resource_version: 'unavailable',
  changelist: 'unavailable',
  request_id: 'req-unrecorded',
};

function row(page, label) {
  return page.locator('.provenance-row', { has: page.getByText(label, { exact: true }) });
}

async function openPage(page, metaFor) {
  const counts = { meta: 0 };
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: POOL });
    if (path === '/api/meta') {
      counts.meta += 1;
      return metaFor(route, counts.meta);
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100');
  expect(counts.meta).toBe(0);
  return counts;
}

async function exercise(page, counts, body) {
  const open = page.getByRole('button', { name: '查看词典版本', exact: true });
  await open.click();
  await expect(row(page, '游戏数据')).toBeVisible();
  expect(counts.meta).toBe(1);
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await expect(page.getByRole('button', { name: '查看词典版本', exact: true })).toBeVisible();
  expect(counts.meta).toBe(1);
  await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
  await expect(row(page, '游戏数据')).toContainText(body.game_version === null ? '版本字段未提供' : body.game_version === 'unavailable' ? '当前服务未记录该版本' : body.game_version);
  expect(counts.meta).toBe(1);
  await page.getByRole('button', { name: '重新读取', exact: true }).click();
  await expect(row(page, '问题反馈编号')).toContainText(body.request_id);
  expect(counts.meta).toBe(2);
}

for (const viewport of [
  { width: 1280, height: 800, label: 'desktop' },
  { width: 390, height: 844, label: 'mobile' },
]) {
  test.describe(`dictionary provenance ${viewport.label}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test('old service fields stay unprovided until an explicit refresh', async ({ page }) => {
      const counts = await openPage(page, (route) => route.fulfill({ json: OLD }));
      await exercise(page, counts, OLD);
      await expect(row(page, '资源版本')).toContainText('版本字段未提供');
      await expect(row(page, 'Changelist')).toContainText('版本字段未提供');
      await expect(row(page, '数据库结构版本')).toContainText('2');
      await expect(row(page, 'source commit')).toContainText('abc123');
      await expect(row(page, '词条数')).toContainText('10,951');
      await expect(page.locator('.provenance')).not.toContainText('3.7.0');
    });

    test('reported versions stay on their own rows', async ({ page }) => {
      const counts = await openPage(page, (route) => route.fulfill({ json: REPORTED }));
      await exercise(page, counts, REPORTED);
      await expect(row(page, '游戏数据')).toContainText('3.7.0');
      await expect(row(page, '资源版本')).toContainText('3.7.8');
      await expect(row(page, 'Changelist')).toContainText('8975829');
      await expect(row(page, '数据库结构版本')).toContainText('2');
      await expect(row(page, '数据库结构版本')).not.toContainText('3.7.0');
      await expect(page.locator('.provenance')).toContainText('当前服务实际报告的词典数据');
    });

    test('a failed read does not invent a version', async ({ page }) => {
      const counts = await openPage(page, (route) => route.fulfill({
        status: 502,
        json: { status: 'unavailable', reason: 'upstream_unavailable' },
      }));
      await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
      await expect(page.getByText('当前版本无法确认。', { exact: true })).toBeVisible();
      expect(counts.meta).toBe(1);
      await page.getByRole('button', { name: '收起', exact: true }).click();
      await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
      await expect(page.getByText('当前版本无法确认。', { exact: true })).toBeVisible();
      expect(counts.meta).toBe(1);
      await expect(page.locator('.provenance')).not.toContainText('3.7');
    });

    test('reopening an active first read or refresh keeps one request in flight', async ({ page }) => {
      let releaseFirst;
      let releaseRefresh;
      const first = new Promise((resolve) => { releaseFirst = resolve; });
      const refresh = new Promise((resolve) => { releaseRefresh = resolve; });
      const counts = await openPage(page, async (route, call) => {
        await (call === 1 ? first : refresh);
        return route.fulfill({ json: { ...OLD, request_id: `req-held-${call}` } });
      });
      const loading = page.getByRole('status').filter({ hasText: '正在读取当前服务报告的词典数据' });
      try {
        await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
        await expect(loading).toBeVisible();
        await expect.poll(() => counts.meta).toBe(1);
        await page.getByRole('button', { name: '收起', exact: true }).click();
        await expect(loading).not.toBeVisible();
        await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
        await expect(loading).toBeVisible({ timeout: 1500 });
        expect(counts.meta).toBe(1);
        releaseFirst();
        await expect(row(page, '问题反馈编号')).toContainText('req-held-1');
        await page.getByRole('button', { name: '重新读取', exact: true }).click();
        await expect(loading).toBeVisible();
        await expect.poll(() => counts.meta).toBe(2);
        await page.getByRole('button', { name: '收起', exact: true }).click();
        await page.getByRole('button', { name: '查看词典版本', exact: true }).click();
        await expect(loading).toBeVisible({ timeout: 1500 });
        expect(counts.meta).toBe(2);
        releaseRefresh();
        await expect(row(page, '问题反馈编号')).toContainText('req-held-2');
      } finally {
        releaseFirst();
        releaseRefresh();
      }
    });
  });
}

test.describe('dictionary provenance single flight', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test('a second click during the first read does not ask again', async ({ page }) => {
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const counts = await openPage(page, async (route) => {
      await held;
      return route.fulfill({ json: UNRECORDED });
    });
    const button = page.getByRole('button', { name: '查看词典版本', exact: true });
    await button.evaluate((node) => {
      node.click();
      node.click();
    });
    release();
    await expect(row(page, '游戏数据')).toContainText('当前服务未记录该版本');
    expect(counts.meta).toBe(1);
  });
});
