import { test, expect } from '@playwright/test';
import { writeFile } from 'node:fs/promises';
import { candidate, dictionary, echo, hash } from '../fixtures/manuscript.mjs';
import { scalarToUtf16 } from '../../lib/review-report.js';

const PAD = '引'.repeat(70);
const SOURCE = `😀${PAD}今汐与今汐声骸漂泊。`;
const TARGET = '😀Jinhsi / Jinhsi / Echo.';
const LONG_FILE = `${'LongSourceFile'.repeat(12)}.json`;
const LONG_ID = `source-${'Identifier'.repeat(8)}`;

function spanAt(text, needle, occurrence) {
  let from = 0;
  for (let index = 0; index < occurrence; index += 1) {
    const utf = text.indexOf(needle, from);
    if (utf < 0) throw new Error(`missing ${needle}`);
    const start = Array.from(text.slice(0, utf)).length;
    const end = start + Array.from(needle).length;
    if (index === occurrence - 1) return { start, end, text: needle };
    from = utf + needle.length;
  }
  throw new Error(`missing ${needle}`);
}

function mention(span) {
  return `${span.start}:${span.end}:${span.text}`;
}

function reportFor(source, target) {
  const first = spanAt(source, '今汐', 1);
  const second = spanAt(source, '今汐', 2);
  const conflict = spanAt(source, '声骸', 1);
  const unevaluated = spanAt(source, '漂泊', 1);
  const shared = { ...candidate, sources: [{ source_file: LONG_FILE, source_id: LONG_ID }] };
  const findings = [
    { id: mention(first), rule_id: 'review.term_pair', verdict: 'verified_constraint', source_span: first, target_span: spanAt(target, 'Jinhsi', 1), candidates: [shared], candidates_truncated: false },
    { id: mention(second), rule_id: 'review.term_pair', verdict: 'needs_review', source_span: second, target_span: null, candidates: [{ ...shared }], candidates_truncated: false },
    { id: mention(conflict), rule_id: 'review.term_pair', verdict: 'confirmed_conflict', source_span: conflict, target_span: spanAt(target, 'Echo', 1), candidates: [echo], candidates_truncated: false },
    { id: mention(unevaluated), rule_id: 'review.term_pair', verdict: 'not_evaluated', source_span: unevaluated, target_span: null, candidates: [], candidates_truncated: false },
  ];
  return {
    request_id: 'navigation-fixture',
    source_revision: hash(source),
    target_revision: hash(target),
    rule_version: 'review-v2',
    dictionary,
    coverage: { evaluated: 3, not_evaluated: 7, rules: ['review.term_pair'] },
    findings,
    truncated: false,
  };
}

const REPORT = reportFor(SOURCE, TARGET);
const FIRST = REPORT.findings[0];
const SECOND = REPORT.findings[1];
const CONFLICT = REPORT.findings[2];
const FOURTH = REPORT.findings[3];

function preservationReportFor(source, target) {
  const report = reportFor(source, target);
  report.findings[1].verdict = 'confirmed_conflict';
  report.findings[2].verdict = 'needs_review';
  report.findings[3].verdict = 'needs_review';
  return report;
}

async function open(page, respond = reportFor) {
  const reviews = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: {
      status: 'available', translation_enabled: true,
      terms: { used: 0, limit: 100, remaining: 100 },
      translations: { used: 0, limit: 100, remaining: 100 },
      characters: { used: 0, limit: 10000, remaining: 10000 },
      reset_at: '2099-01-01T00:00:00Z',
    } });
    if (path === '/api/reviews') {
      reviews.push(route.request().postDataJSON());
      return route.fulfill({ json: respond(
        route.request().postDataJSON().source,
        route.request().postDataJSON().target,
        reviews.length,
      ) });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto('/');
  await expect(page.locator('.pool-strip')).toContainText('100');
  return reviews;
}

async function downloaded(page, button = '导出当前结果') {
  const waiting = page.waitForEvent('download');
  await page.getByRole('button', { name: button, exact: true }).click();
  const stream = await (await waiting).createReadStream();
  const parts = [];
  for await (const part of stream) parts.push(part);
  return Buffer.concat(parts);
}

async function exported(page) {
  return JSON.parse((await downloaded(page)).toString());
}

async function attachFile(info, name, bytes, contentType) {
  const path = info.outputPath(name);
  await writeFile(path, bytes);
  await info.attach(name, { path, contentType });
}

async function exportsFor(page) {
  return {
    result: await downloaded(page),
    manuscript: await downloaded(page, '保存稿件'),
    translation: await downloaded(page, '导出译文'),
  };
}

async function invariantPhase(page, info, reviews, phase, navigate) {
  const before = await exportsFor(page);
  const requestCount = reviews.length;
  const summary = await page.locator('.review-summary').innerText();
  const comparison = await page.locator('.report-changes').innerText();
  const choiceStatus = await page.locator('.choice-records').innerText();
  await navigate();
  const after = await exportsFor(page);
  expect(reviews).toHaveLength(requestCount);
  expect(await page.locator('.review-summary').innerText()).toBe(summary);
  expect(await page.locator('.report-changes').innerText()).toBe(comparison);
  expect(await page.locator('.choice-records').innerText()).toBe(choiceStatus);
  const exportHashes = {};
  for (const name of Object.keys(before)) {
    expect(after[name].equals(before[name]), `${phase}: ${name} download bytes`).toBe(true);
    exportHashes[name] = { before: hash(before[name]), after: hash(after[name]), bytes: before[name].length };
    const extension = name === 'translation' ? 'txt' : 'json';
    const type = name === 'translation' ? 'text/plain' : 'application/json';
    await attachFile(info, `${phase}-${name}-before.${extension}`, before[name], type);
    await attachFile(info, `${phase}-${name}-after.${extension}`, after[name], type);
  }
  await attachFile(info, `${phase}-invariants.json`, JSON.stringify({
    requestsBefore: requestCount, requestsAfter: reviews.length, reviewRequests: reviews,
    exportHashes, summary, comparison, choiceStatus,
  }, null, 2), 'application/json');
  return before;
}

async function preservationSequence(page) {
  await page.getByRole('button', { name: '已核 1', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${FIRST.id}"]`)).toHaveAttribute('aria-current', 'true');
  await page.getByRole('button', { name: '全部 4', exact: true }).click();
  await page.getByRole('button', { name: '下一项', exact: true }).click();
  await page.getByRole('button', { name: '冲突 1', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toHaveAttribute('data-active', 'true');
  await page.getByRole('button', { name: '全部 4', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toHaveAttribute('data-active', 'true');
  await page.getByRole('button', { name: '需核对 2', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('aria-current', 'true');
  await expect(page.locator('[data-active="true"]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '下一项', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${FOURTH.id}"]`)).toHaveAttribute('data-active', 'true');
  await expect(page.locator('.finding-position')).toHaveText('当前 2 / 2');
  await expect(page.getByRole('button', { name: '下一项', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '上一项', exact: true }).click();
  await page.getByRole('button', { name: '全部 4', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
  await expect(page.locator('.finding-position')).toHaveText('当前 3 / 4');
  await page.getByRole('button', { name: '未评估 0', exact: true }).click();
  await expect(page.locator('.finding-position')).toHaveText('当前 0 / 0');
  await expect(page.locator('[data-active="true"]')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '下一项', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '全部 4', exact: true }).click();
  await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
}

async function selection(page, selector) {
  return page.locator(selector).evaluate(element => ({
    start: element.selectionStart,
    end: element.selectionEnd,
    value: element.value,
  }));
}

async function check(page) {
  await page.locator('#review-source').fill(SOURCE);
  await page.locator('#review-target').fill(TARGET);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-current')).toBeVisible();
}

async function navigationEvidence(page, info, name, reviews) {
  const state = {
    viewport: page.viewportSize(),
    position: await page.locator('.finding-position').innerText(),
    activeIds: await page.locator('[data-active="true"]').evaluateAll(cards => cards.map(card => card.dataset.mentionId)),
    reviewRequests: reviews.length,
  };
  await attachFile(info, `${name}.json`, JSON.stringify(state, null, 2), 'application/json');
  await attachFile(info, `${name}.png`, await page.screenshot({ fullPage: true }), 'image/png');
}

for (const viewport of [
  { name: 'desktop', width: 1280, height: 800 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test.describe(viewport.name, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    test('filter-displayed finding becomes canonical on return to all', async ({ page }, info) => {
      const reviews = await open(page, preservationReportFor);
      await check(page);
      // Explicitly select A, rather than relying on the initial derived default.
      await page.getByRole('button', { name: '下一项', exact: true }).click();
      await page.getByRole('button', { name: '上一项', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${FIRST.id}"]`)).toHaveAttribute('data-active', 'true');
      await navigationEvidence(page, info, 'canonical-before-filter', reviews);

      await page.getByRole('button', { name: '需核对 2', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
      await expect(page.locator('.finding-position')).toHaveText('当前 1 / 2');
      await navigationEvidence(page, info, 'fallback-after-filter', reviews);

      await page.getByRole('button', { name: '全部 4', exact: true }).click();
      await navigationEvidence(page, info, 'preserved-after-broadening', reviews);
      expect(reviews).toHaveLength(1);
      await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
      await expect(page.locator('.finding-position')).toHaveText('当前 3 / 4');
    });

    test('navigation preserves populated exports, stale reports and imported history', async ({ page }, info) => {
      const reviews = await open(page, preservationReportFor);
      await page.locator('#review-source').fill(SOURCE);
      await page.locator('#review-target').fill(TARGET);
      await page.locator('.alignment-panel > summary').click();
      await page.getByRole('button', { name: '确认全文为一个对应段', exact: true }).click();
      await page.getByRole('button', { name: '核对术语', exact: true }).click();
      await expect(page.locator('.review-current')).toBeVisible();
      await page.locator(`[data-mention-id="${FIRST.id}"]`).getByRole('button', { name: '采用此官方词对', exact: true }).click();
      await page.getByRole('button', { name: '核对术语', exact: true }).click();
      await expect(page.locator('.review-current')).toBeVisible();
      expect(reviews).toHaveLength(2);
      expect(reviews[1].alignments).toHaveLength(1);
      expect(reviews[1].resolutions).toEqual([{ mention_id: FIRST.id, choice: 'official_pair', candidate_id: FIRST.candidates[0].candidate_id }]);
      await expect(page.locator('.report-changes')).toBeVisible();

      const saved = await invariantPhase(page, info, reviews, 'current', async () => {
        await preservationSequence(page);
        const firstCard = page.locator(`[data-mention-id="${FIRST.id}"]`);
        await firstCard.getByText('查看完整上下文', { exact: true }).click();
        await expect(firstCard.locator('.review-context')).toContainText(PAD);
        await firstCard.getByText('查看来源', { exact: true }).click();
        await expect(firstCard.locator('.candidate-sources')).toContainText(LONG_ID);
        await page.locator(`[data-mention-id="${CONFLICT.id}"]`).getByRole('button', { name: '定位原文', exact: true }).click();
        expect(await selection(page, '#review-source')).toEqual({
          start: scalarToUtf16(SOURCE, CONFLICT.source_span.start),
          end: scalarToUtf16(SOURCE, CONFLICT.source_span.end), value: SOURCE,
        });
      });
      const result = JSON.parse(saved.result);
      const manuscript = JSON.parse(saved.manuscript);
      expect(result.choices).toHaveLength(1);
      expect(result.alignments).toHaveLength(1);
      expect(result.report_is_current).toBe(true);
      expect(result.changes).not.toBeNull();
      expect(manuscript.history).toHaveLength(2);
      expect(manuscript.history[1].resolutions).toEqual(reviews[1].resolutions);
      expect(saved.translation.toString()).toBe(TARGET);

      await page.locator('#review-source').fill(`${SOURCE}注`);
      await page.locator('#review-target').fill(`${TARGET}注`);
      await expect(page.locator('.review-current')).toHaveCount(0);
      for (const button of await page.getByRole('button', { name: /定位原文|定位译文/ }).all()) await expect(button).toBeDisabled();
      const stale = await invariantPhase(page, info, reviews, 'stale', () => preservationSequence(page));
      expect(JSON.parse(stale.result).report_is_current).toBe(false);
      expect(JSON.parse(stale.result).changes).toBeNull();

      await page.getByLabel('选择稿件文件').setInputFiles({ name: 'history.json', mimeType: 'application/json', buffer: saved.manuscript });
      await expect(page.locator('.review-summary')).toContainText('来自文件的历史报告');
      await expect(page.locator('.choice-records')).toContainText('待确认');
      const historical = await invariantPhase(page, info, reviews, 'historical', () => preservationSequence(page));
      expect(JSON.parse(historical.result).report_is_current).toBe(false);
      expect(JSON.parse(historical.manuscript).history).toHaveLength(2);
      expect(JSON.parse(historical.manuscript).alignments).toHaveLength(1);
      await expect(page.locator('.review-current')).toHaveCount(0);
      expect(reviews).toHaveLength(2);
    });

    test('replacement reports reconcile the current filter without resurrecting old IDs', async ({ page }, info) => {
      const reviews = await open(page, (source, target, count) => {
        const report = preservationReportFor(source, target);
        report.request_id = `navigation-fixture-${count}`;
        if (count >= 3) report.findings[0].verdict = 'confirmed_conflict';
        if (count === 3) report.findings.splice(1, 1);
        if (count === 5) report.findings = [];
        return report;
      });
      await check(page);
      await page.getByRole('button', { name: '下一项', exact: true }).click();
      await page.getByRole('button', { name: '冲突 1', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toHaveAttribute('data-active', 'true');

      for (const [count, activeId, position] of [
        [2, SECOND.id, '当前 1 / 1'],
        [3, FIRST.id, '当前 1 / 1'],
        [4, FIRST.id, '当前 1 / 2'],
        [5, null, '当前 0 / 0'],
        [6, FIRST.id, '当前 1 / 2'],
      ]) {
        await page.getByRole('button', { name: '核对术语', exact: true }).click();
        await expect.poll(() => reviews.length).toBe(count);
        await expect(page.locator('.review-current')).toBeVisible();
        await expect(page.locator('.finding-position')).toHaveText(position);
        await expect(page.locator('[data-active="true"]')).toHaveCount(activeId ? 1 : 0);
        if (activeId) await expect(page.locator(`[data-mention-id="${activeId}"]`)).toHaveAttribute('data-active', 'true');
        else {
          await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeDisabled();
          await expect(page.getByRole('button', { name: '下一项', exact: true })).toBeDisabled();
        }
        await navigationEvidence(page, info, `report-${count}`, reviews);
      }
      await page.getByRole('button', { name: '全部 4', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${FIRST.id}"]`)).toHaveAttribute('data-active', 'true');
      await page.getByRole('button', { name: '需核对 2', exact: true }).click();
      await page.getByRole('button', { name: '全部 4', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
      expect(reviews).toHaveLength(6);
    });

    test('finding navigation is local and occurrence-specific', async ({ page }) => {
      const reviews = await open(page);
      await check(page);
      const summary = await page.locator('.review-summary').innerText();
      expect(summary).toContain('未评估 7');
      await expect(page.getByRole('button', { name: '全部 4', exact: true })).toHaveAttribute('aria-pressed', 'true');
      await expect(page.getByRole('button', { name: '冲突 1', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '需核对 1', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '未评估 1', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '已核 1', exact: true })).toBeVisible();
      await expect(page.getByRole('button', { name: '已核 1', exact: true })).not.toHaveClass(/review-current/);

      const before = await exported(page);
      for (const [name, count, text] of [
        ['冲突 1', 1, '声骸'],
        ['需核对 1', 1, '今汐'],
        ['未评估 1', 1, '漂泊'],
        ['已核 1', 1, '今汐'],
        ['全部 4', 4, '声骸'],
      ]) {
        await page.getByRole('button', { name, exact: true }).click();
        await expect(page.getByRole('button', { name, exact: true })).toHaveAttribute('aria-pressed', 'true');
        await expect(page.locator('.review-findings .term-result')).toHaveCount(count);
        await expect(page.locator('.review-findings')).toContainText(text);
        expect(await page.locator('.review-summary').innerText()).toBe(summary);
        await expect(page.locator('.review-current')).toHaveCount(1);
      }

      await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeDisabled();
      await expect(page.locator('.finding-position')).toHaveText('当前 1 / 4');
      await expect(page.locator(`[data-mention-id="${FIRST.id}"]`)).toHaveAttribute('data-active', 'true');
      await page.getByRole('button', { name: '下一项', exact: true }).click();
      await expect(page.locator('.finding-position')).toHaveText('当前 2 / 4');
      await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toHaveAttribute('data-active', 'true');
      await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toContainText('当前项');
      await page.getByRole('button', { name: '冲突 1', exact: true }).click();
      await expect(page.locator('.finding-position')).toHaveText('当前 1 / 1');
      await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
      await expect(page.getByRole('button', { name: '下一项', exact: true })).toBeDisabled();
      await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeDisabled();
      await page.getByRole('button', { name: '全部 4', exact: true }).click();
      // The displayed filter fallback replaces the old hidden selection.
      await expect(page.locator(`[data-mention-id="${CONFLICT.id}"]`)).toHaveAttribute('data-active', 'true');
      await page.getByRole('button', { name: '上一项', exact: true }).click();
      await expect(page.locator(`[data-mention-id="${SECOND.id}"]`)).toHaveAttribute('data-active', 'true');

      const secondCard = page.locator(`[data-mention-id="${SECOND.id}"]`);
      await expect(secondCard.locator('.review-excerpt').first()).not.toContainText(PAD);
      await secondCard.getByText('查看完整上下文', { exact: true }).click();
      await expect(secondCard.locator('.review-context')).toContainText(PAD);
      await secondCard.getByRole('button', { name: '定位原文', exact: true }).click();
      expect(await selection(page, '#review-source')).toEqual({
        start: scalarToUtf16(SOURCE, SECOND.source_span.start),
        end: scalarToUtf16(SOURCE, SECOND.source_span.end),
        value: SOURCE,
      });
      expect(scalarToUtf16(SOURCE, SECOND.source_span.start)).toBeGreaterThan(SECOND.source_span.start);

      await page.getByRole('button', { name: '上一项', exact: true }).click();
      const firstCard = page.locator(`[data-mention-id="${FIRST.id}"]`);
      await firstCard.getByRole('button', { name: '定位译文', exact: true }).click();
      expect(await selection(page, '#review-target')).toEqual({
        start: scalarToUtf16(TARGET, FIRST.target_span.start),
        end: scalarToUtf16(TARGET, FIRST.target_span.end),
        value: TARGET,
      });
      await expect(secondCard.getByRole('button', { name: '定位译文', exact: true })).toBeDisabled();
      await firstCard.getByText('查看来源', { exact: true }).click();
      await expect(firstCard.locator('.candidate-sources')).toContainText(LONG_FILE);
      await expect(firstCard.locator('.candidate-sources')).toContainText(LONG_ID);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
      expect(overflow).toBe(true);
      await expect(page.getByRole('button', { name: '上一项', exact: true })).toBeVisible();

      const after = await exported(page);
      expect(after).toEqual(before);
      expect(reviews).toHaveLength(1);

      await firstCard.getByRole('button', { name: '采用此官方词对', exact: true }).click();
      await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
      await expect(secondCard.getByRole('button', { name: '采用此官方词对', exact: true })).toBeEnabled();
      const chosen = await exported(page);
      expect(chosen.choices).toHaveLength(1);
      expect(chosen.choices[0].source_span).toEqual(FIRST.source_span);
      expect(reviews).toHaveLength(1);

      await page.locator('#review-source').fill(`${SOURCE}注`);
      await expect(page.locator('.review-current')).toHaveCount(0);
      await expect(page.locator('.review-stale').first()).toBeVisible();
      await expect(page.getByRole('button', { name: '定位原文' })).toHaveCount(4);
      for (const button of await page.getByRole('button', { name: '定位原文' }).all()) await expect(button).toBeDisabled();
      await expect(page.locator(`[data-mention-id="${FIRST.id}"]`).getByRole('button', { name: '定位译文', exact: true })).toBeEnabled();
      await page.locator('#review-target').fill(`${TARGET}注`);
      for (const button of await page.getByRole('button', { name: '定位译文' }).all()) await expect(button).toBeDisabled();
      await expect(page.locator('.review-findings .term-result')).toHaveCount(4);
      await page.getByRole('button', { name: '未评估 1', exact: true }).click();
      await expect(page.locator('.review-findings')).toContainText('漂泊');
      await expect(page.locator('.review-current')).toHaveCount(0);
      const staleExport = await exported(page);
      expect(staleExport.report_is_current).toBe(false);
      await page.getByRole('button', { name: '全部 4', exact: true }).click();
      await expect(page.getByRole('button', { name: '下一项', exact: true })).toBeDisabled();
      await page.getByRole('button', { name: '上一项', exact: true }).click();
      expect(await exported(page)).toEqual(staleExport);
      expect(reviews).toHaveLength(1);
    });
  });
}
