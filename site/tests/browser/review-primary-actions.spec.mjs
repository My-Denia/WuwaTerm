import { test, expect } from '@playwright/test';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { serializeWorkfile } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const SOURCE = '今汐。';
const TARGET = 'Jinhsi.';

const ZH = {
  check: '核对术语', fresh: '仅获取新依据', undo: '撤销修订',
  clear: '清空稿件', save: '保存稿件', import: '导入稿件',
  exportTxt: '导出译文', exportResult: '导出当前结果', exportMarkdown: '导出 Markdown 报告',
  importFile: '选择稿件文件',
};
const EN = {
  check: 'Check terms', fresh: 'Fetch fresh basis only', undo: 'Undo edit',
  clear: 'Clear manuscript', save: 'Save manuscript', import: 'Import manuscript',
  exportTxt: 'Export translation', exportResult: 'Export current result', exportMarkdown: 'Export Markdown report',
  importFile: 'Choose a manuscript file',
};

function poolBody(reviewsRemaining) {
  return {
    status: 'available',
    translation_enabled: true,
    terms: { used: 0, limit: 100, remaining: 100 },
    translations: { used: 0, limit: 30, remaining: 30 },
    characters: { used: 0, limit: 12000, remaining: 12000 },
    reviews: { used: 60 - reviewsRemaining, limit: 60, remaining: reviewsRemaining },
    reset_at: '2099-01-01T00:00:00Z',
  };
}

/** Synthetic local routes only: /api/pool plus an optional inert /api/reviews recorder. */
async function open(page, reviewsRemaining) {
  const reviews = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: poolBody(reviewsRemaining) });
    if (path === '/api/reviews') {
      const body = route.request().postDataJSON();
      reviews.push(body);
      return route.fulfill({ json: freshApiReport(fixture(body.source, body.target, body.direction).report) });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.review-quota')).toContainText(`${reviewsRemaining} / 60`, { timeout: 30_000 });
  return reviews;
}

async function switchTo(page, language) {
  const button = page.locator(`.lang-toggle button:text-is("${language}")`);
  await expect(button).toBeVisible();
  await expect(async () => {
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }).toPass({ timeout: 10_000 });
}

async function fillPair(page) {
  await page.locator('#review-source').fill(SOURCE);
  await page.locator('#review-target').fill(TARGET);
}

async function downloadFrom(page, name) {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name, exact: true }).click();
  const download = await waiting;
  const stream = await download.createReadStream();
  const parts = [];
  for await (const part of stream) parts.push(part);
  return { name: download.suggestedFilename(), text: Buffer.concat(parts).toString() };
}

/** Primary form vs draft-tool block, measured from the rendered DOM. */
async function documentOrder(page) {
  return page.evaluate(() => {
    const card = document.querySelector('.review-card');
    const form = card.querySelector('form');
    const draft = card.querySelector('.draft-actions');
    const submit = form.querySelector('button[type="submit"]');
    const controls = [...draft.querySelectorAll('button, input[type="file"]')];
    const buttons = controls.filter(node => node.tagName === 'BUTTON');
    const follows = (anchor, node) => !!(anchor.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING);
    let collapsed = false;
    for (let parent = draft.parentElement; parent; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS' || parent.hasAttribute('hidden') || parent.getAttribute('aria-hidden') === 'true') collapsed = true;
    }
    return {
      formCount: card.querySelectorAll('form').length,
      draftAfterForm: follows(form, draft),
      draftPreviousIsForm: draft.previousElementSibling === form,
      draftOutsideForm: !form.contains(draft),
      checkBeforeDraftButtons: buttons.map(button => follows(submit, button)),
      labels: buttons.map(button => button.textContent),
      controlTags: controls.map(node => node.tagName + (node.type === 'file' ? '[file]' : '')),
      collapsed,
    };
  });
}

async function layout(page) {
  return page.evaluate(() => {
    const card = document.querySelector('.review-card');
    const form = card.querySelector('form');
    const draft = card.querySelector('.draft-actions');
    const submit = form.querySelector('button[type="submit"]');
    const box = node => {
      const b = node.getBoundingClientRect();
      return { x: b.x, y: b.y, width: b.width, height: b.height, bottom: b.bottom, right: b.right };
    };
    return {
      form: box(form), draft: box(draft), check: box(submit),
      draftButtons: [...draft.querySelectorAll('button')].map(button => ({ label: button.textContent, ...box(button) })),
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });
}

test('the input form and its Check terms action precede every manuscript file tool', async ({ page }) => {
  await open(page, 60);
  const order = await documentOrder(page);
  expect(order.formCount).toBe(1);
  // The exact existing draft-tool block, still one uncollapsed sibling block.
  expect(order.draftOutsideForm).toBe(true);
  expect(order.collapsed).toBe(false);
  expect(order.controlTags).toEqual(['BUTTON', 'BUTTON', 'BUTTON', 'INPUT[file]', 'BUTTON', 'BUTTON', 'BUTTON']);
  expect(order.labels).toEqual([ZH.clear, ZH.save, ZH.import, ZH.exportTxt, ZH.exportResult, ZH.exportMarkdown]);
  // DOM hierarchy: the block sits immediately after the form, not before it.
  expect(order.draftPreviousIsForm).toBe(true);
  expect(order.draftAfterForm).toBe(true);
  // Rendered/accessibility-tree order: primary submit before every draft button.
  expect(order.checkBeforeDraftButtons).toEqual([true, true, true, true, true, true]);
  await expect(page.locator('.draft-actions button')).toHaveCount(6);
});

for (const [name, viewport] of [['desktop', { width: 1280, height: 720 }], ['390px', { width: 390, height: 844 }]]) {
  test.describe(`${name} primary action geometry`, () => {
    test.use({ viewport });
    test('Check terms renders above all draft tools with no horizontal overflow', async ({ page }, testInfo) => {
      await open(page, 60);
      for (const [language, labels, toggle] of [['中文', ZH, null], ['English', EN, 'English']]) {
        if (toggle) await switchTo(page, toggle);
        await fillPair(page);
        await page.screenshot({ path: testInfo.outputPath(`primary-actions-${name}-${language}.png`), fullPage: true });
        const box = await layout(page);
        const check = page.getByRole('button', { name: labels.check, exact: true });
        await expect(check).toBeEnabled();
        await expect(check).toBeVisible();
        // The whole form, including its primary action, ends before draft tools start.
        expect(box.form.bottom).toBeLessThanOrEqual(box.draft.y + 0.5);
        expect(box.check.bottom).toBeLessThanOrEqual(box.draft.y + 0.5);
        expect(box.check.y).toBeLessThan(box.draftButtons[0].y);
        // Review delta: the relocated block keeps the existing 17px action-row
        // top spacing. Strictly positive here; the legacy `.draft-actions`
        // margin override must not collapse it to 0.
        const actionGap = box.draft.y - box.form.bottom;
        expect(actionGap).toBeGreaterThan(0);
        expect(actionGap).toBeGreaterThanOrEqual(16);
        // Same control count and order in both UI languages, unreduced by wrapping.
        expect(box.draftButtons).toHaveLength(6);
        expect(box.draftButtons.map(button => button.label)).toEqual(
          [labels.clear, labels.save, labels.import, labels.exportTxt, labels.exportResult, labels.exportMarkdown],
        );
        for (const button of box.draftButtons) {
          expect(button.x).toBeGreaterThanOrEqual(0);
          expect(button.right).toBeLessThanOrEqual(box.innerWidth + 1);
          expect(button.y).toBeGreaterThanOrEqual(box.draft.y - 0.5);
        }
        expect(box.scrollWidth).toBeLessThanOrEqual(box.innerWidth + 1);
      }
    });
  });
}

test('keyboard order keeps the primary action before draft tools and skips it while disabled', async ({ page }) => {
  await open(page, 60);
  const active = () => page.evaluate(() => {
    const node = document.activeElement;
    return {
      tag: node?.tagName ?? '',
      id: node?.id ?? '',
      label: node?.textContent?.trim() ?? '',
      inDraftActions: !!node?.closest('.draft-actions'),
      isCheck: node === document.querySelector('.review-card form button[type="submit"]'),
    };
  });

  // Empty form: the primary is disabled, so it is not a tab stop.
  await page.locator('#review-source').focus();
  await page.keyboard.press('Tab');
  expect((await active()).id).toBe('review-target');
  await page.keyboard.press('Tab');
  expect((await active()).tag).toBe('SUMMARY');
  await page.keyboard.press('Tab');
  const emptyNext = await active();
  expect(emptyNext.isCheck).toBe(false);

  // Filled form: source -> target -> existing alignment disclosure -> Check terms.
  await fillPair(page);
  await page.locator('#review-source').focus();
  await page.keyboard.press('Tab');
  expect((await active()).id).toBe('review-target');
  await page.keyboard.press('Tab');
  const disclosure = await active();
  expect(disclosure.tag).toBe('SUMMARY');
  expect(disclosure.label).toBe('分段对应 · 按句序检查');
  await page.keyboard.press('Tab');
  const onCheck = await active();
  expect(onCheck.isCheck).toBe(true);
  expect(onCheck.label).toBe(ZH.check);
  // The next stop is the form's secondary action, not a manuscript file tool.
  await page.keyboard.press('Tab');
  const onFresh = await active();
  expect(onFresh.label).toBe(ZH.fresh);
  expect(onFresh.inDraftActions).toBe(false);
  // The existing undo action belongs to the form action group too.
  await page.keyboard.press('Tab');
  const onUndo = await active();
  expect(onUndo.label).toBe(ZH.undo);
  expect(onUndo.inDraftActions).toBe(false);
  // Draft tools are only reached after the form's own action group.
  await page.keyboard.press('Tab');
  const afterForm = await active();
  expect(afterForm.inDraftActions).toBe(true);
  expect(afterForm.label).toBe(ZH.clear);
  expect(afterForm.isCheck).toBe(false);
});

test('with no report yet, save/import/export really work and send no review request', async ({ page }) => {
  const reviews = await open(page, 60);
  await expect(page.locator('.draft-actions button')).toHaveCount(6);

  // Save is available before typing anything: a valid empty workfile, not a disabled stub.
  const blank = await downloadFrom(page, ZH.save);
  expect(blank.name).toBe('wuwaterm-manuscript.json');
  const blankJson = JSON.parse(blank.text);
  expect(blankJson.format).toBe('wuwaterm-manuscript-v1');
  expect(blankJson.source).toBe('');
  expect(blankJson.target).toBe('');
  expect(blankJson.history).toEqual([]);

  await fillPair(page);
  await expect(page.locator('.review-summary')).toHaveCount(0);

  const txt = await downloadFrom(page, ZH.exportTxt);
  expect(txt.name).toBe('wuwaterm-translation.txt');
  expect(txt.text).toBe(TARGET);

  const result = JSON.parse((await downloadFrom(page, ZH.exportResult)).text);
  expect(result.format).toBe('wuwaterm-result-v2');
  expect(result.status).toBe('requires_recheck');
  expect(result.report_is_current).toBe(false);
  expect(result.report).toBeNull();
  expect(result.source).toBe(SOURCE);
  expect(result.target).toBe(TARGET);

  const markdown = (await downloadFrom(page, ZH.exportMarkdown)).text;
  expect(markdown).toContain('当前没有审校报告；以下仅包含稿件文本与本地选择。');
  expect(markdown).toContain(SOURCE);

  const file = serializeWorkfile({ source: SOURCE, target: TARGET, direction: 'en', alignments: null, choices: [], history: [] });
  await page.getByLabel(ZH.importFile).setInputFiles({ name: 'draft.json', mimeType: 'application/json', buffer: Buffer.from(file) });
  await expect(page.locator('.manuscript-save-state')).toContainText('当前工作与这次导入的稿件一致');
  await expect(page.locator('.notice-state')).toContainText('已在本地恢复稿件，未上传');
  await expect(page.locator('#review-source')).toHaveValue(SOURCE);
  await expect(page.locator('#review-target')).toHaveValue(TARGET);

  const resaved = await downloadFrom(page, ZH.save);
  expect(resaved.text).toBe(file);

  expect(reviews).toHaveLength(0);
});

test('at zero review quota the local file tools stay usable and send no review request', async ({ page }) => {
  const reviews = await open(page, 0);
  await fillPair(page);
  const check = page.getByRole('button', { name: ZH.check, exact: true });
  await expect(check).toBeDisabled();
  await expect(page.getByRole('button', { name: ZH.fresh, exact: true })).toBeDisabled();

  const saved = JSON.parse((await downloadFrom(page, ZH.save)).text);
  expect(saved.source).toBe(SOURCE);
  expect(saved.target).toBe(TARGET);
  expect(saved.history).toEqual([]);
  expect((await downloadFrom(page, ZH.exportTxt)).text).toBe(TARGET);
  expect(JSON.parse((await downloadFrom(page, ZH.exportResult)).text).status).toBe('requires_recheck');
  expect((await downloadFrom(page, ZH.exportMarkdown)).text).toContain('当前没有审校报告');

  // Disabled primary is not a tab stop even with a filled form.
  await page.locator('#review-source').focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  expect(await check.evaluate(node => node === document.activeElement)).toBe(false);
  await expect(check).toHaveCSS('cursor', 'not-allowed');

  await switchTo(page, 'English');
  const file = serializeWorkfile({ source: SOURCE, target: TARGET, direction: 'en', alignments: null, choices: [], history: [] });
  await page.getByLabel(EN.importFile).setInputFiles({ name: 'draft.json', mimeType: 'application/json', buffer: Buffer.from(file) });
  await expect(page.locator('#review-source')).toHaveValue(SOURCE);
  for (const name of [EN.clear, EN.save, EN.import, EN.exportTxt, EN.exportResult, EN.exportMarkdown]) {
    await expect(page.getByRole('button', { name, exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name, exact: true })).toBeEnabled();
  }
  const englishSave = JSON.parse((await downloadFrom(page, EN.save)).text);
  expect(englishSave.source).toBe(SOURCE);
  expect(englishSave.target).toBe(TARGET);
  await expect(page.getByRole('button', { name: EN.check, exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: EN.fresh, exact: true })).toBeDisabled();

  expect(reviews).toHaveLength(0);
});
