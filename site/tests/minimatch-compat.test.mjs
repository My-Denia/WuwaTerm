import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);

test('legacy ESLint consumers retain callable matching and defaults with patched minimatch', () => {
  for (const consumer of ['eslint-plugin-react', 'eslint-plugin-import', 'eslint-plugin-jsx-a11y']) {
    const fromConsumer = createRequire(require.resolve(`${consumer}/package.json`));
    const match = fromConsumer('minimatch');
    assert.equal(typeof match, 'function', consumer);
    assert.equal(match('Component', 'Comp*'), true);
    assert.equal(match('Other', 'Comp*'), false);
    assert.equal(match('src/a.ts', '**/*.{js,ts}'), true);
    assert.equal(match.defaults({ nocase: true })('ABC', 'abc'), true);
    assert.equal(new match.Minimatch('*.js').match('a.js'), true);
    assert.deepEqual(match.match(['a.js', 'b.ts'], '*.js'), ['a.js']);
  }
});
