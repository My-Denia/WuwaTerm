import { test, expect } from '@playwright/test';
import { fixture, freshApiReport } from '../fixtures/manuscript.mjs';

const BASE = process.env.WUWATERM_BROWSER_URL ?? '';

const POOL = {
  status: 'available', translation_enabled: true,
  terms: { used: 0, limit: 100, remaining: 100 },
  translations: { used: 0, limit: 100, remaining: 100 },
  characters: { used: 0, limit: 10000, remaining: 10000 },
  reviews: { used: 0, limit: 60, remaining: 60 },
  reset_at: '2099-01-01T00:00:00Z',
};

function reviewBody() {
  const body = fixture('声骸', 'Echo', 'en');
  const known = body.report.findings[0].candidates[0];
  known.category = 'echo';
  body.report.findings[0].candidates.push({
    ...known,
    candidate_id: 'a'.repeat(64),
    category: 'not_a_category',
  });
  return freshApiReport(body.report);
}

async function open(page) {
  const counts = { terms: 0, reviews: 0 };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/pool') return route.fulfill({ json: POOL });
    if (path === '/api/terms') {
      counts.terms += 1;
      return route.fulfill({ json: {
        request_id: 'label-fixture',
        matches: [
          { zh: '声骸', en: 'Echo', category: 'echo', reason: 'pinyin-abbrev', score: 92 },
          { zh: '自定义', en: 'Custom', category: 'not_a_category', reason: 'not_a_reason', score: 10 },
        ],
      } });
    }
    if (path === '/api/reviews') {
      counts.reviews += 1;
      return route.fulfill({ json: reviewBody() });
    }
    throw new Error(`Unexpected API request: ${path}`);
  });
  await page.goto(BASE + '/');
  await expect(page.locator('.pool-strip')).toContainText('100', { timeout: 30_000 });
  return counts;
}

async function switchTo(page, language) {
  const button = page.locator(`.lang-toggle button:text-is("${language}")`);
  await expect(button).toBeVisible();
  await expect(async () => {
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }).toPass({ timeout: 10_000 });
}

test('lookup and review labels follow the interface language and leave unknown keys raw', async ({ page }) => {
  const counts = await open(page);
  await page.locator('#term-query').fill('声骸');
  await page.locator('.terms-card').getByRole('button', { name: '查术语', exact: true }).click();
  const known = page.locator('.terms-results .term-result').nth(0);
  const unknown = page.locator('.terms-results .term-result').nth(1);
  await expect(known.locator('dd').nth(0)).toHaveText('声骸');
  await expect(known.locator('dd').nth(1)).toHaveText('拼音缩写');
  await expect(unknown.locator('dd').nth(0)).toHaveText('not_a_category');
  await expect(unknown.locator('dd').nth(1)).toHaveText('not_a_reason');

  await page.locator('#review-source').fill('声骸');
  await page.locator('#review-target').fill('Echo');
  await page.getByRole('button', { name: '核对术语', exact: true }).click();
  const chips = page.locator('.review-findings .term-pair small');
  await expect(chips.nth(0)).toHaveText('类别：声骸');
  await expect(chips.nth(1)).toHaveText('类别：not_a_category');

  await switchTo(page, 'English');
  await expect(known.locator('dd').nth(0)).toHaveText('Echo');
  await expect(known.locator('dd').nth(1)).toHaveText('Pinyin abbreviation');
  await expect(unknown.locator('dd').nth(0)).toHaveText('not_a_category');
  await expect(unknown.locator('dd').nth(1)).toHaveText('not_a_reason');
  await expect(chips.nth(0)).toHaveText('Category: Echo');
  await expect(chips.nth(1)).toHaveText('Category: not_a_category');
  expect(counts.terms).toBe(1);
  expect(counts.reviews).toBe(1);
});
