import { bootBuilt } from '../tests/helpers/built-harness.mjs';

const runtime = await bootBuilt();
console.log('BUILT_SITE_URL=' + runtime.url.href);
async function stop() { await runtime.close(); }
process.once('SIGTERM', stop);
process.once('SIGINT', stop);
