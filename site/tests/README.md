# Site regression tests

From `site/`, install the locked dependencies with `npm ci`.

`npm test` runs the fast proxy, manuscript and local D1 tests without requiring
a production build. The synthetic runtime Worker remains a separate fixture;
its success does not prove vinext route parsing or runtime environment bindings.

For production integration, run these commands in order:

```sh
npm run build
npm run verify:no-client-secret
npm run test:built
```

`test:built` fails if the server configuration, Worker entry or client manifest
is missing. It uses Wrangler's locked `createTestHarness()` to load the generated
configuration and actual server/client artifacts. Each test starts fresh local
D1 storage and applies the committed SQL migrations. Only the synthetic
upstream origin is intercepted; other non-local network requests are rejected.
No production API, Telegram traffic or public quota is used. Wrangler tool
state is isolated under the ignored `.wrangler/` directory for the test process.

The suite checks `review-v2`, text revisions, alignment/choice forwarding, real
request parsing, error envelopes, environment bindings and quota accounting.
Unknown routes currently return the locked framework's HTML 404 without a
cache header. They are checked separately from known API errors, which return
JSON with `no-store`; the synthetic Worker's JSON `not_found` is not treated as
the production unknown-route contract. Inside the parser-rejection test the
text/plain case stays last: the handler drops that request without reading its
body, and wrangler's dispatch transport then fails the single next request, so
nothing may follow it on the same harness.

For the real browser regressions:

```sh
npx playwright install chromium
npm run test:browser
```

The browser suite covers the translation/review handoff, findings navigation,
dictionary provenance, interface language and the Markdown export. API calls
are intercepted with synthetic responses and review requests are counted; the
tests exercise the real client bundle in the browser. CI runs the independent
artifact suite after build and client-secret scanning. CI also runs the whole
browser suite against the emitted client bundle. To do the same locally after
building:

```sh
WUWATERM_BROWSER_BUILT=1 npm run test:browser
```

The build server URL is allocated by the harness and captured by Playwright;
the browser API responses remain synthetic.
