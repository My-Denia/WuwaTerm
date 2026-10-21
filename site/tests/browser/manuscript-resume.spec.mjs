import { test, expect } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';
import { parseWorkfile } from '../../lib/manuscript.js';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';

function getResumeDir(testInfo) {
  const dir = process.env.WUWATERM_RESUME_DIR ?? testInfo.outputPath('resume-downloads');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function sha256(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

async function setupPage(page, { requests = [] } = {}) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    requests.push({ method: route.request().method(), pathname, postData: route.request().postData() });
    if (pathname === '/api/pool') {
      return route.fulfill({ json: {
        status: 'available', translation_enabled: true,
        terms: { used: 0, limit: 100, remaining: 100 },
        translations: { used: 0, limit: 100, remaining: 100 },
        characters: { used: 0, limit: 10000, remaining: 10000 },
        reviews: { used: 0, limit: 60, remaining: 60 },
        reset_at: '2099-01-01T00:00:00Z',
      } });
    }
    if (pathname === '/api/reviews') {
      const body = route.request().postDataJSON();
      return route.fulfill({ json: freshApiReport(fixture(body.source, body.target, body.direction).report) });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
}

test('Chinese UI: disk-save -> close session -> fresh context -> reopen from filesystem path', async ({ browser }, testInfo) => {
  const resumeDir = getResumeDir(testInfo);
  const file1Path = path.join(resumeDir, 'session1-manuscript.json');
  const file2Path = path.join(resumeDir, 'session2-manuscript.json');

  // --- Session 1: Create work, save to actual disk file ---
  const context1 = await browser.newContext();
  const page1 = await context1.newPage();
  const requests1 = [];
  await setupPage(page1, { requests: requests1 });

  // 1. Create public/synthetic manuscript with Unicode text, correspondence, choice, report history
  const unicodeSource = '今汐：乘霄山之行。\n声骸。';
  const unicodeTarget = 'Jinhsi: Journey to Mt. Firmament.\nEcho.';
  await page1.locator('#review-source').fill(unicodeSource);
  await page1.locator('#review-target').fill(unicodeTarget);

  // Add explicit correspondence (alignment)
  await page1.locator('.alignment-panel > summary').click();
  await page1.getByRole('button', { name: '确认全文为一个对应段', exact: true }).click();
  await expect(page1.locator('.alignment-row')).toHaveCount(1);

  // First check -> creates first report in history
  await page1.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page1.locator('.review-current')).toBeVisible();

  // Adopt official choice
  await page1.getByRole('button', { name: '采用此官方词对', exact: true }).first().click();
  await expect(page1.locator('.choice-records')).toContainText('今汐 / Jinhsi');

  // Second check -> creates second report in history
  await page1.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page1.locator('.review-current')).toBeVisible();

  // Save via actual button using Playwright download.saveAs to filesystem path
  const [downloadEvent1] = await Promise.all([
    page1.waitForEvent('download'),
    page1.getByRole('button', { name: '保存稿件', exact: true }).click(),
  ]);
  await downloadEvent1.saveAs(file1Path);
  expect(fs.existsSync(file1Path)).toBe(true);

  // Independently read the saved filesystem path
  const file1Raw = fs.readFileSync(file1Path, 'utf-8');
  const file1Bytes = Buffer.byteLength(file1Raw, 'utf-8');
  const file1Hash = sha256(Buffer.from(file1Raw, 'utf-8'));
  const parsedFile1 = parseWorkfile(file1Raw);
  expect(parsedFile1.source).toBe(unicodeSource);
  expect(parsedFile1.target).toBe(unicodeTarget);
  expect(parsedFile1.direction).toBe('en');
  expect(parsedFile1.alignments).toHaveLength(1);
  expect(parsedFile1.choices).toHaveLength(1);
  expect(parsedFile1.history).toHaveLength(2);

  // Close the original page and context entirely
  await page1.close();
  await context1.close();

  // --- Session 2: Fresh browser context; demonstrate absence of work without import ---
  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  const requests2 = [];
  await setupPage(page2, { requests: requests2 });

  // Demonstrate page work is absent in new context
  await expect(page2.locator('#review-source')).toHaveValue('');
  await expect(page2.locator('#review-target')).toHaveValue('');
  await expect(page2.locator('.choice-records')).toHaveCount(0);
  await expect(page2.locator('.review-findings')).toHaveCount(0);

  // Import using the saved FILESYSTEM PATH (string path, not Buffer fixture)
  await page2.getByLabel('选择稿件文件').setInputFiles(file1Path);

  // Wait for import completion
  await expect(page2.locator('#review-source')).toHaveValue(unicodeSource);
  await expect(page2.locator('#review-target')).toHaveValue(unicodeTarget);
  await expect(page2.locator('.notice-state')).toContainText('已在本地恢复稿件');

  // Verify choices, alignments, reports restored
  await expect(page2.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  // Check correspondence restored
  await page2.locator('.alignment-panel > summary').click();
  await expect(page2.locator('.alignment-row')).toHaveCount(1);
  // Verify reports are untrusted historical reports (summaryImported)
  await expect(page2.locator('.review-stale')).toContainText('来自文件的历史报告，仅供参考，不是当前认证。');

  // Verify NO automatic check was made upon import (reviews request count remains 0 in session 2)
  const reviewCallsSession2 = requests2.filter(r => r.pathname === '/api/reviews');
  expect(reviewCallsSession2).toHaveLength(0);

  // Edit draft in session 2, save a second file to disk
  const editedSource = unicodeSource + '\n追加内容。';
  await page2.locator('#review-source').fill(editedSource);
  const [downloadEvent2] = await Promise.all([
    page2.waitForEvent('download'),
    page2.getByRole('button', { name: '保存稿件', exact: true }).click(),
  ]);
  await downloadEvent2.saveAs(file2Path);

  // Close session 2
  await page2.close();
  await context2.close();

  // Verify file1 on disk remains unchanged
  const file1RawAfter = fs.readFileSync(file1Path, 'utf-8');
  expect(sha256(Buffer.from(file1RawAfter, 'utf-8'))).toBe(file1Hash);

  // Verify file2 on disk has edited content
  const file2Raw = fs.readFileSync(file2Path, 'utf-8');
  const parsedFile2 = parseWorkfile(file2Raw);
  expect(parsedFile2.source).toBe(editedSource);

  // --- Session 3: Open file 2 and verify resumption ---
  const context3 = await browser.newContext();
  const page3 = await context3.newPage();
  await setupPage(page3);
  await page3.getByLabel('选择稿件文件').setInputFiles(file2Path);
  await expect(page3.locator('#review-source')).toHaveValue(editedSource);
  await page3.close();
  await context3.close();

  // Save ledger evidence
  const ledger = {
    test: 'Chinese UI disk-save and fresh-context resume',
    file1: { path: file1Path, bytes: file1Bytes, sha256: file1Hash },
    file2: { path: file2Path, bytes: Buffer.byteLength(file2Raw), sha256: sha256(Buffer.from(file2Raw)) },
    verifiedAbsenceWithoutImport: true,
    restoredFromPathString: true,
    zeroAutomaticNetworkReviews: reviewCallsSession2.length === 0,
  };
  fs.writeFileSync(path.join(resumeDir, 'session-zh-ledger.json'), JSON.stringify(ledger, null, 2));
});

test('English UI: save/reopen journey and in-place language toggle preservation', async ({ browser }, testInfo) => {
  const resumeDir = getResumeDir(testInfo);
  const enFilePath = path.join(resumeDir, 'session-en-manuscript.json');

  const context = await browser.newContext();
  const page = await context.newPage();
  const requests = [];
  await setupPage(page, { requests });

  // Switch to English first
  await page.getByRole('button', { name: 'English', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');

  const enSource = 'Jinhsi and Echo.\nResonance.';
  const enTarget = '今汐与声骸。\n共鸣。';
  // English to Chinese direction
  await page.getByLabel('Translation language').selectOption('zh');
  await page.locator('#review-source').fill(enSource);
  await page.locator('#review-target').fill(enTarget);

  // Save to actual filesystem path
  const [downloadEvent] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Save manuscript', exact: true }).click(),
  ]);
  await downloadEvent.saveAs(enFilePath);
  expect(fs.existsSync(enFilePath)).toBe(true);

  // Switch language with work present: verify text/direction preserved
  await page.getByRole('button', { name: '中文', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page.locator('#review-source')).toHaveValue(enSource);
  await expect(page.locator('#review-target')).toHaveValue(enTarget);

  await page.close();
  await context.close();

  // Reopen in fresh context starting in English
  const freshContext = await browser.newContext();
  const freshPage = await freshContext.newPage();
  await setupPage(freshPage);
  await freshPage.getByRole('button', { name: 'English', exact: true }).click();
  await expect(freshPage.locator('html')).toHaveAttribute('lang', 'en');

  // Import saved English manuscript from filesystem path
  await freshPage.getByLabel('Choose a manuscript file').setInputFiles(enFilePath);
  await expect(freshPage.locator('#review-source')).toHaveValue(enSource);
  await expect(freshPage.locator('#review-target')).toHaveValue(enTarget);
  await expect(freshPage.locator('.notice-state')).toContainText('Manuscript restored locally');

  await freshPage.close();
  await freshContext.close();
});

test('Read back TXT, result JSON, Markdown outputs from actual saved disk paths', async ({ browser }, testInfo) => {
  const resumeDir = getResumeDir(testInfo);
  const txtPath = path.join(resumeDir, 'exported-target.txt');
  const jsonPath = path.join(resumeDir, 'exported-result.json');
  const mdPath = path.join(resumeDir, 'exported-report.md');

  const context = await browser.newContext();
  const page = await context.newPage();
  await setupPage(page);

  const sampleSource = '今汐。\n声骸。';
  const sampleTarget = 'Jinhsi.\nEcho.';
  await page.locator('#review-source').fill(sampleSource);
  await page.locator('#review-target').fill(sampleTarget);

  // Perform a check to have reports and results
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-current')).toBeVisible();

  // 1. Export TXT to actual path
  const [txtDownload] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '导出译文', exact: true }).click(),
  ]);
  await txtDownload.saveAs(txtPath);
  const txtContent = fs.readFileSync(txtPath, 'utf-8');
  expect(txtContent).toBe(sampleTarget);

  // 2. Export Result JSON to actual path
  const [jsonDownload] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '导出当前结果', exact: true }).click(),
  ]);
  await jsonDownload.saveAs(jsonPath);
  const jsonRaw = fs.readFileSync(jsonPath, 'utf-8');
  const resultObj = JSON.parse(jsonRaw);
  expect(resultObj.format).toBe('wuwaterm-result-v2');
  expect(resultObj.source).toBe(sampleSource);
  expect(resultObj.target).toBe(sampleTarget);
  expect(resultObj.report_is_current).toBe(true);
  expect(resultObj.sentence_meaning_evaluated).toBe(false);
  expect(resultObj.verified_stamp.valid).toBe(false);

  // Confirm result JSON is NOT a resumable manuscript (format is result-v2, not manuscript-v1)
  expect(() => parseWorkfile(jsonRaw)).toThrow();

  // 3. Export Markdown to actual path
  const [mdDownload] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: '导出 Markdown 报告', exact: true }).click(),
  ]);
  await mdDownload.saveAs(mdPath);
  const mdContent = fs.readFileSync(mdPath, 'utf-8');
  expect(mdContent).toContain('# WuwaTerm 术语审校报告');
  expect(mdContent).toContain('Jinhsi');

  // Confirm Markdown is NOT a resumable manuscript
  expect(() => parseWorkfile(mdContent)).toThrow();

  await page.close();
  await context.close();

  // Save evidence of outputs
  const outputEvidence = {
    txt: { path: txtPath, bytes: Buffer.byteLength(txtContent), sha256: sha256(Buffer.from(txtContent)) },
    resultJson: { path: jsonPath, bytes: Buffer.byteLength(jsonRaw), sha256: sha256(Buffer.from(jsonRaw)) },
    markdown: { path: mdPath, bytes: Buffer.byteLength(mdContent), sha256: sha256(Buffer.from(mdContent)) },
    nonResumableConfirmed: true,
  };
  fs.writeFileSync(path.join(resumeDir, 'output-exports-ledger.json'), JSON.stringify(outputEvidence, null, 2));
});
