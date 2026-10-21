import { test, expect } from '@playwright/test';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { serializeWorkfile } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const A = { source: '今汐。', target: 'Jinhsi.', direction: 'en' };
const B = { source: '声骸。', target: 'Echo.', direction: 'en' };
const C = { source: '共鸣。', target: 'Resonance.', direction: 'en' };

function savedManuscript(draft) {
  const saved = fixture(draft.source, draft.target, draft.direction);
  return serializeWorkfile({
    ...draft,
    alignments: null,
    choices: [],
    history: [{ ...draft, alignments: null, report: saved.report, resolutions: [] }],
  });
}

async function open(page) {
  const reviews = [];
  const translations = [];
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
      translations.push(body);
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
    return route.fulfill({ status: 404 });
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  return { reviews, translations, gate };
}

async function setupFileReadInterception(page) {
  await page.addInitScript(() => {
    const original = File.prototype.text;
    window.__fileReads = [];
    File.prototype.text = function () {
      const name = this.name;
      return new Promise((resolve, reject) => {
        window.__fileReads.push({
          name,
          resolve: async () => resolve(await original.call(this)),
          reject: (err) => reject(new Error(err || 'Read error')),
        });
      });
    };
    window.__resolveRead = async (name) => {
      const idx = window.__fileReads.findIndex(r => r.name === name);
      if (idx === -1) throw new Error('No read found for ' + name);
      const [item] = window.__fileReads.splice(idx, 1);
      await item.resolve();
    };
    window.__rejectRead = (name, err) => {
      const idx = window.__fileReads.findIndex(r => r.name === name);
      if (idx === -1) throw new Error('No read found for ' + name);
      const [item] = window.__fileReads.splice(idx, 1);
      item.reject(err);
    };
    window.__pendingReads = () => window.__fileReads.map(r => r.name);
  });
}

test('A selected then B; B succeeds then A rejects: candidate must not show obsolete error from A', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  await page.locator('#review-source').fill('初始。');
  await page.locator('#review-target').fill('Initial.');

  // Select A
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json',
    mimeType: 'application/json',
    buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');

  // Select B
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b.json',
    mimeType: 'application/json',
    buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b.json');

  // B succeeds
  await page.evaluate(() => window.__resolveRead('file-b.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');
  await expect(page.locator('.error-panel')).toHaveCount(0);

  // A rejects (e.g. read error)
  await page.evaluate(() => window.__rejectRead('file-a.json', 'File read error'));

  // Obsolete A rejection must NOT display an error in candidate
  await expect(page.locator('.error-panel')).toHaveCount(0);
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');
});

test('both success completion orders: A-before-B and B-before-A must end at B', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  // Subcase 1: B-before-A
  await page.locator('#review-source').fill('初始1。');
  await page.locator('#review-target').fill('Initial 1.');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a1.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a1.json');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b1.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b1.json');

  await page.evaluate(() => window.__resolveRead('file-b1.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await page.evaluate(() => window.__resolveRead('file-a1.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.error-panel')).toHaveCount(0);

  // Subcase 2: A-before-B
  await page.locator('#review-source').fill('初始2。');
  await page.locator('#review-target').fill('Initial 2.');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a2.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a2.json');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-c2.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(C)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-c2.json');

  // A finishes first, but is obsolete because C was selected
  await page.evaluate(() => window.__resolveRead('file-a2.json'));
  // Manuscript should still be Initial 2, because C has not resolved yet and A is obsolete
  await expect(page.locator('#review-source')).toHaveValue('初始2。');

  // Now C finishes
  await page.evaluate(() => window.__resolveRead('file-c2.json'));
  await expect(page.locator('#review-source')).toHaveValue(C.source);
  await expect(page.locator('.error-panel')).toHaveCount(0);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');
});

test('obsolete A parse failure and obsolete A success after B result do not pollute B', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  await page.locator('#review-source').fill('初始。');
  await page.locator('#review-target').fill('Initial.');

  // Select A with invalid JSON
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a-bad.json', mimeType: 'application/json', buffer: Buffer.from('{"corrupt json}'),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a-bad.json');

  // Select B with valid manuscript
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b-good.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b-good.json');

  // B succeeds first
  await page.evaluate(() => window.__resolveRead('file-b-good.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');
  await expect(page.locator('.error-panel')).toHaveCount(0);

  // A resolves with corrupt JSON: should be silently dropped without showing parse error
  await page.evaluate(() => window.__resolveRead('file-a-bad.json'));
  await expect(page.locator('.error-panel')).toHaveCount(0);
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');
});

test('B invalid/unreadable/oversize supersedes pending A; current manuscript retained, B error shown, A never fallback', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  const initialSource = '当前稿件源文。';
  const initialTarget = 'Current draft target.';
  await page.locator('#review-source').fill(initialSource);
  await page.locator('#review-target').fill(initialTarget);

  // 1. B unreadable / rejects
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a1.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a1.json');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b1-unreadable.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b1-unreadable.json');

  // B rejects
  await page.evaluate(() => window.__rejectRead('file-b1-unreadable.json', 'IO error'));
  await expect(page.locator('.error-panel')).toHaveText('文件无效；当前稿件未被替换。');
  await expect(page.locator('#review-source')).toHaveValue(initialSource);

  // A resolves after B rejection: must NOT import as fallback
  await page.evaluate(() => window.__resolveRead('file-a1.json'));
  await expect(page.locator('#review-source')).toHaveValue(initialSource);
  await expect(page.locator('.error-panel')).toHaveText('文件无效；当前稿件未被替换。');

  // 2. B invalid JSON
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a2.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a2.json');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b2-corrupt.json', mimeType: 'application/json', buffer: Buffer.from('not json at all'),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b2-corrupt.json');

  await page.evaluate(() => window.__resolveRead('file-b2-corrupt.json'));
  await expect(page.locator('.error-panel')).toHaveText('无法读取 JSON 稿件文件。');
  await expect(page.locator('#review-source')).toHaveValue(initialSource);

  // A resolves: must NOT import
  await page.evaluate(() => window.__resolveRead('file-a2.json'));
  await expect(page.locator('#review-source')).toHaveValue(initialSource);
  await expect(page.locator('.error-panel')).toHaveText('无法读取 JSON 稿件文件。');

  // 3. B oversized (>1 MiB)
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a3.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a3.json');

  // Select oversize B: size check happens synchronously
  const largeBuffer = Buffer.alloc(1024 * 1024 + 10, 0x20);
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b3-huge.json', mimeType: 'application/json', buffer: largeBuffer,
  });
  await expect(page.locator('.error-panel')).toHaveText('稿件文件不能超过 1 MiB；当前稿件未被替换。');
  await expect(page.locator('#review-source')).toHaveValue(initialSource);

  // Now A resolves: must NOT import
  await page.evaluate(() => window.__resolveRead('file-a3.json'));
  await expect(page.locator('#review-source')).toHaveValue(initialSource);
  await expect(page.locator('.error-panel')).toHaveText('稿件文件不能超过 1 MiB；当前稿件未被替换。');
});

test('the same file can be selected again and late A cannot clear later selection', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  await page.locator('#review-source').fill('初始。');

  // Select file A
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');

  // Select file A AGAIN with same filename
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads().length)).toBe(2);

  // First A resolves: obsolete, should not import A
  await page.evaluate(() => window.__resolveRead('file-a.json'));
  await expect(page.locator('#review-source')).toHaveValue('初始。');

  // Second A resolves: owning, imports B
  await page.evaluate(() => window.__resolveRead('file-a.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件');

  // Now select the same file once more after completion
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');
  await page.evaluate(() => window.__resolveRead('file-a.json'));
  await expect(page.locator('#review-source')).toHaveValue(A.source);
});

test('source/target edit, clear, check initiation during read preserve newer work', async ({ page }) => {
  await setupFileReadInterception(page);
  const { gate } = await open(page);

  // 1. Source edit during read
  await page.locator('#review-source').fill('初始1。');
  await page.locator('#review-target').fill('Initial 1.');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');

  // User edits source while reading
  await page.locator('#review-source').fill('初始1已修改。');

  // Read resolves: generation changed, so A must NOT replace newer work
  await page.evaluate(() => window.__resolveRead('file-a.json'));
  await expect(page.locator('#review-source')).toHaveValue('初始1已修改。');
  await expect(page.locator('.notice-state')).toHaveText('读取文件期间稿件发生变化，未覆盖当前内容，请重新导入。');

  // 2. Clear draft during read
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b.json');

  await page.getByRole('button', { name: '清空稿件', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('');

  await page.evaluate(() => window.__resolveRead('file-b.json'));
  // Newer clear is preserved
  await expect(page.locator('#review-source')).toHaveValue('');
  await expect(page.locator('.notice-state')).toHaveText('读取文件期间稿件发生变化，未覆盖当前内容，请重新导入。');

  // Undo restores the draft before clear
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue('初始1已修改。');

  // 3. Check initiation during read
  let releaseCheck;
  gate.next = new Promise(resolve => { releaseCheck = resolve; });

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-c.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(C)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-c.json');

  // Start check (increments generation)
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  // C resolves while check is in flight: check initiation changed generation
  await page.evaluate(() => window.__resolveRead('file-c.json'));
  await expect(page.locator('#review-source')).toHaveValue('初始1已修改。');
  await expect(page.locator('.notice-state')).toHaveText('读取文件期间稿件发生变化，未覆盖当前内容，请重新导入。');
  releaseCheck();
});

test('owning A failure after edit does not overwrite newer work or its notice with import error', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  await page.locator('#review-source').fill('用户新文本。');
  await page.locator('#review-target').fill('User new target.');

  // Select A
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from('corrupt content'),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');

  // User edits while A is reading
  await page.locator('#review-source').fill('用户再次修改。');

  // A rejects or fails
  await page.evaluate(() => window.__rejectRead('file-a.json', 'Late read failure'));

  // Newer draft is preserved, NO importInvalid error shown
  await expect(page.locator('#review-source')).toHaveValue('用户再次修改。');
  await expect(page.locator('.error-panel')).toHaveCount(0);
});

test('review finishing during pending read: import then undo restores acceptance-time reports', async ({ page }) => {
  await setupFileReadInterception(page);
  const { reviews, gate } = await open(page);

  await page.locator('#review-source').fill(A.source);
  await page.locator('#review-target').fill(A.target);

  // Gate the review so it stays in flight
  let release;
  gate.next = new Promise(resolve => { release = resolve; });

  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect.poll(() => reviews.length).toBe(1);

  // While review is in flight, select B
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b.json');

  // Review finishes while B is still reading
  release();
  await expect(page.locator('.review-current')).toBeVisible();

  // B finishes reading and is accepted
  await page.evaluate(() => window.__resolveRead('file-b.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);

  // Undo import: must restore the acceptance-time reports from the check that just finished
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue(A.source);
  // Findings from the restored report should be visible
  await expect(page.locator('.review-findings')).toBeVisible();
});

test('owning read error and changed-during-read relocalize correctly on Chinese/English toggle', async ({ page }) => {
  await setupFileReadInterception(page);
  await open(page);

  // Trigger changed-during-read
  await page.locator('#review-source').fill('测试。');
  await page.locator('#review-target').fill('Test.');

  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-a.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(A)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-a.json');

  await page.locator('#review-source').fill('测试变动。');
  await page.evaluate(() => window.__resolveRead('file-a.json'));
  await expect(page.locator('.notice-state')).toHaveText('读取文件期间稿件发生变化，未覆盖当前内容，请重新导入。');

  // Switch language to English
  await page.getByRole('button', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.locator('.notice-state')).toHaveText('The manuscript changed while the file was being read; nothing was overwritten. Please import again.');

  // Trigger owning read error
  await page.getByLabel('Choose a manuscript file').setInputFiles({
    name: 'file-b.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b.json');
  await page.evaluate(() => window.__rejectRead('file-b.json', 'Disk read failure'));
  await expect(page.locator('.error-panel')).toHaveText('The file is invalid; the current manuscript was not replaced.');

  // Switch language back to Chinese
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page.locator('.error-panel')).toHaveText('文件无效；当前稿件未被替换。');
});

test('import and undo issue zero translation/review POSTs and exports remain historical', async ({ page }) => {
  await setupFileReadInterception(page);
  const { reviews, translations } = await open(page);

  await page.locator('#review-source').fill(A.source);
  await page.locator('#review-target').fill(A.target);

  // Import B
  await page.getByLabel('选择稿件文件').setInputFiles({
    name: 'file-b.json', mimeType: 'application/json', buffer: Buffer.from(savedManuscript(B)),
  });
  await expect.poll(() => page.evaluate(() => window.__pendingReads())).toContain('file-b.json');
  await page.evaluate(() => window.__resolveRead('file-b.json'));
  await expect(page.locator('#review-source')).toHaveValue(B.source);

  // Undo
  await page.getByRole('button', { name: '撤销修订', exact: true }).click();
  await expect(page.locator('#review-source')).toHaveValue(A.source);

  // Zero network calls made
  expect(reviews).toHaveLength(0);
  expect(translations).toHaveLength(0);

  // Export current result
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出当前结果', exact: true }).click();
  const download = await waiting;
  const stream = await download.createReadStream();
  const parts = [];
  for await (const chunk of stream) parts.push(chunk);
  const resultJson = JSON.parse(Buffer.concat(parts).toString('utf-8'));

  expect(resultJson.report_is_current).toBe(false);
  expect(resultJson.sentence_meaning_evaluated).toBe(false);
  expect(resultJson.verified_stamp.valid).toBe(false);
});
