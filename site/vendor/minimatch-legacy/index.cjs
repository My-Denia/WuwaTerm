// Preserve the callable CommonJS API used by older ESLint plugins while all
// matching and defaults use the patched upstream implementation.
// CommonJS is required by the legacy consumers this adapter serves.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const modern = require('minimatch-modern');
module.exports = modern.minimatch;
module.exports.minimatch = modern.minimatch;
module.exports.Minimatch = modern.Minimatch;
module.exports.defaults = modern.defaults;
module.exports.filter = modern.filter;
module.exports.match = modern.match;
module.exports.makeRe = modern.minimatch.makeRe;
module.exports.braceExpand = modern.braceExpand;
module.exports.escape = modern.escape;
module.exports.unescape = modern.unescape;
module.exports.sep = modern.sep;
module.exports.GLOBSTAR = modern.GLOBSTAR;
