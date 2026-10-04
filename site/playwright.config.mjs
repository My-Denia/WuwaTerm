import { defineConfig } from '@playwright/test';

const built = process.env.WUWATERM_BROWSER_BUILT === '1';

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  retries: 0,
  reporter: 'list',
  outputDir: './.wrangler/browser-results',
  use: { baseURL: 'http://127.0.0.1:4173', trace: 'retain-on-failure' },
  webServer: {
    ...(built ? {
      command: 'node scripts/serve-built-browser.mjs',
      // The named group is exported to WUWATERM_BROWSER_URL without the
      // trailing slash; browser specs navigate to BASE + '/...'.
      wait: { stdout: /BUILT_SITE_URL=(?<WUWATERM_BROWSER_URL>http:\/\/127\.0\.0\.1:\d+)/ },
    } : {
      command: 'npm run dev -- --hostname 127.0.0.1 --port 4173',
      url: 'http://127.0.0.1:4173',
    }),
    stdout: 'pipe',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5000 },
  },
});
