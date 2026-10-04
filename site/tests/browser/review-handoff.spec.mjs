import { test, expect } from '@playwright/test';
import { fixture } from '../fixtures/manuscript.mjs';
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
      return route.fulfill({ json: fixture(body.source, body.target, body.direction).report });
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

async function workfile(page, button = '保存稿件') {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: button, exact: true }).click();
  const stream = await (await waiting).createReadStream();
  const parts = [];
  for await (const part of stream) parts.push(part);
  return JSON.parse(Buffer.concat(parts).toString());
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
