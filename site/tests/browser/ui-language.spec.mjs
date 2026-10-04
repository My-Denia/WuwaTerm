import { test, expect } from '@playwright/test';
import { fixture } from '../fixtures/manuscript.mjs';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';
const SOURCE = '今汐。\n声骸。';
const TARGET = 'Jinhsi.\nEcho.';

async function open(page) {
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
      const body = route.request().postDataJSON();
      reviews.push(body);
      return route.fulfill({ json: fixture(body.source, body.target, body.direction).report });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  // Warm-up gate, not the behavior under test: in built mode the worker
  // cold-start can push the first pool read past the default 5s on shared
  // CI runners, so allow a generous wait here.
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  return { reviews };
}

async function prepare(page) {
  await page.locator('#review-source').fill(SOURCE);
  await page.locator('#review-target').fill(TARGET);
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  await expect(page.locator('.review-current')).toBeVisible();
}

async function switchTo(page, language) {
  // Retry the click until the pressed state flips: clicking before React
  // hydration attaches handlers is a no-op, and the flipped state is also
  // proof the cookie write inside the handler has run.
  const button = page.locator(`.lang-toggle button:text-is("${language}")`);
  await expect(button).toBeVisible();
  await expect(async () => {
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }).toPass({ timeout: 10_000 });
}

test('first visit defaults to Chinese with server-rendered lang and metadata', async ({ page }) => {
  await open(page);
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page).toHaveTitle(/鸣潮中英术语与整句翻译/u);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('准确抵达');
  await expect(page.locator('.lang-toggle button[aria-pressed="true"]')).toHaveText('中文');
});

test('toggling language switches copy in place, keeps workbench state, and sends no requests', async ({ page }) => {
  const { reviews } = await open(page);
  await prepare(page);
  await page.getByRole('button', { name: '采用此官方词对', exact: true }).first().click();
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  // The staged-choice notice stays visible and re-localizes when the language
  // switches (notices store catalog keys, not rendered strings).
  await expect(page.locator('.notice-state')).toContainText('已暂存此处选择');
  await switchTo(page, '中文'); // active filter stays Chinese while in Chinese…
  await page.getByRole('button', { name: '已核 2', exact: false }).click();
  const activeBefore = await page.locator('.review-findings [aria-current="true"]').getAttribute('data-mention-id');
  await page.locator('.candidate-sources > summary').first().click();
  await expect(page.locator('.candidate-sources').first()).toHaveAttribute('open', '');
  await page.evaluate(() => { window.__survivesToggle = 'marker'; });

  const apiRequests = [];
  page.on('request', request => { if (request.url().includes('/api/')) apiRequests.push(request.url()); });
  await switchTo(page, 'English');
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page).toHaveTitle(/Terms & Sentence Translation/u);
  await expect(page.getByRole('heading', { level: 1 })).toContainText('delivered accurately');
  await expect(page.getByRole('button', { name: 'Check terms', exact: true })).toBeVisible();
  // Workbench state survives: manuscript, currency semantics, choice, filter, active finding, expansion.
  await expect(page.locator('#review-source')).toHaveValue(SOURCE);
  await expect(page.locator('#review-target')).toHaveValue(TARGET);
  // The staged choice reset the phase, so the honest state is "stale / re-check";
  // that semantics must survive the language switch unchanged.
  await expect(page.locator('.review-summary .review-stale')).toContainText('please check again');
  await expect(page.locator('.choice-records')).toContainText('今汐 / Jinhsi');
  await expect(page.locator('.review-findings [aria-current="true"]')).toHaveAttribute('data-mention-id', activeBefore);
  await expect(page.locator('.candidate-sources').first()).toHaveAttribute('open', '');
  await expect(page.locator('.notice-state')).toContainText('Choice staged here');
  expect(await page.evaluate(() => window.__survivesToggle)).toBe('marker');
  await switchTo(page, '中文');
  await expect(page.getByRole('button', { name: '核对术语', exact: true })).toBeVisible();
  expect(apiRequests).toHaveLength(0);
  expect(reviews).toHaveLength(1);
});

test('translation direction is independent of interface language', async ({ page }) => {
  await open(page);
  await page.locator('#review-source').fill(SOURCE);
  await page.locator('#review-target').fill(TARGET);
  await page.getByLabel('译文语言').selectOption('zh');
  await switchTo(page, 'English');
  await expect(page.getByLabel('Translation language')).toHaveValue('zh');
  await expect(page.locator('#review-source')).toHaveValue(SOURCE);
  await expect(page.locator('#review-target')).toHaveValue(TARGET);
  await switchTo(page, '中文');
  await expect(page.getByLabel('译文语言')).toHaveValue('zh');
});

test('language preference persists across reload with matching SSR markup and no hydration errors', async ({ page }) => {
  await open(page);
  await switchTo(page, 'English');
  const problems = [];
  page.on('pageerror', error => problems.push(String(error)));
  page.on('console', message => { if (message.type() === 'error') problems.push(message.text()); });
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('delivered accurately');
  await switchTo(page, '中文');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('准确抵达');
  expect(problems.filter(item => !item.includes('favicon'))).toHaveLength(0);
});

test('document pages follow the language preference and disclose the cookie in English', async ({ page }) => {
  await open(page);
  await switchTo(page, 'English');
  await page.goto(BASE + '/limits');
  await expect(page.locator('h1')).toContainText('One quota, shared by everyone.');
  await expect(page.locator('.document-top .lang-toggle')).toBeVisible();
  await page.goto(BASE + '/privacy');
  await expect(page.locator('h1')).toContainText('For this one lookup and translation.');
  await expect(page.locator('.document')).toContainText('wuwaterm-lang');
  await switchTo(page, '中文');
  await expect(page.locator('h1')).toContainText('只为这一次查询与翻译。');
});

test('both languages fit the narrow viewport without horizontal overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  await switchTo(page, 'English');
  await expect(page.locator('.lang-toggle button[aria-pressed="true"]')).toHaveText('English');
  const overflowEn = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  await switchTo(page, '中文');
  const overflowZh = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflowEn).toBeLessThanOrEqual(1);
  expect(overflowZh).toBeLessThanOrEqual(1);
});
