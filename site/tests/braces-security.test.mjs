import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
// This override lets the same regression suite demonstrate failure against
// the untouched official 3.0.3 source without keeping it in our dependency tree.
const target = process.env.WUWATERM_BRACES_UNDER_TEST || require.resolve('braces');
const braces = require(target);
const fixtures = JSON.parse(readFileSync(new URL('./fixtures/braces-3.0.3-semantics.json', import.meta.url)));

test('normal compile, expand and stringify semantics match official braces 3.0.3', () => {
  for (const { input, options, ...expected } of fixtures) {
    for (const method of ['compile', 'expand', 'stringify']) {
      assert.deepEqual(braces[method](input, options), expected[method], `${method}: ${input}`);
    }
    assert.deepEqual(braces(input, { ...options, expand: true }), input.length < 3 ? [input] : expected.expand);
    assert.equal(braces.compile(braces.parse(input, options), options), expected.compile);
  }
  assert.deepEqual(braces(['{a,b}', '{c,d}'], { expand: true }), ['a', 'b', 'c', 'd']);
  assert.throws(() => braces.expand('{1..1001}'), /range limit/);
  assert.deepEqual(braces.expand('{1..1001..500}'), ['1', '501', '1001']);
});

test('100 nested blocks remain valid, including mixed braces and parentheses', () => {
  for (const pattern of [
    '{'.repeat(100) + 'a,b' + '}'.repeat(100),
    '('.repeat(100) + 'a' + ')'.repeat(100),
    '{('.repeat(50) + 'a,b' + ')}'.repeat(50),
  ]) {
    for (const method of ['parse', 'compile', 'expand', 'stringify']) {
      assert.doesNotThrow(() => braces[method](pattern), method);
    }
  }
  assert.doesNotThrow(() => braces.expand('\\{'.repeat(1000)));
  assert.doesNotThrow(() => braces.expand('"' + '{'.repeat(1000) + '"'));
  assert.doesNotThrow(() => braces.expand('[' + '{'.repeat(1000) + ']'));
});

function isolated(body) {
  const result = spawnSync(process.execPath, ['--max-old-space-size=128', '-e', `
    const assert = require('node:assert/strict');
    const path = require('node:path');
    const target = ${JSON.stringify(target)};
    const braces = require(target);
    const rejected = (fn) => assert.throws(fn, error =>
      error instanceof SyntaxError && /maximum depth/.test(error.message));
    ${body}
  `], { encoding: 'utf8', timeout: 5000, maxBuffer: 16384 });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.signal, null, result.stderr);
  assert.equal(result.status, 0, result.stderr);
}

test('all string APIs deliberately reject deep nested, mixed and unbalanced patterns', () => {
  isolated(`
    for (const pattern of [
      '{'.repeat(4500) + 'a,b' + '}'.repeat(4500),
      '('.repeat(4500) + 'a' + ')'.repeat(4500),
      '{('.repeat(2200) + 'a,b' + ')}'.repeat(2200),
      '{'.repeat(4500) + 'a,b',
      '('.repeat(4500) + '{a,b}',
    ]) {
      for (const method of ['parse', 'compile', 'expand', 'stringify', 'create']) {
        rejected(() => braces[method](pattern));
      }
      rejected(() => braces(pattern));
      rejected(() => braces([pattern], { expand: true }));
    }
  `);
});

test('direct ASTs, internal walkers and cyclic child/parent paths cannot bypass the guard', () => {
  isolated(`
    const ast = () => {
      const root = { type: 'root', nodes: [] };
      let parent = root;
      for (let i = 0; i < 4500; i++) {
        const child = { type: 'paren', nodes: [], parent };
        parent.nodes.push(child);
        parent = child;
      }
      parent.nodes.push({ type: 'text', value: 'x' });
      return root;
    };
    for (const method of ['compile', 'expand', 'stringify']) {
      rejected(() => braces[method](ast()));
      const internal = require(path.join(path.dirname(target), 'lib', method));
      rejected(() => internal(ast()));
      const cyclic = { type: 'root', nodes: [] };
      cyclic.nodes.push(cyclic);
      rejected(() => braces[method](cyclic));
    }
    const parentCycle = { type: 'paren', nodes: [] };
    parentCycle.parent = parentCycle;
    rejected(() => braces.expand(parentCycle));
    let value = 'x';
    for (let i = 0; i < 15000; i++) value = [value];
    const cyclicValue = [];
    cyclicValue.push(cyclicValue);
    for (const nestedValue of [value, cyclicValue]) {
      for (const method of ['compile', 'expand', 'stringify']) {
        const invalid = { type: 'root', nodes: [{ type: 'text', value: nestedValue }] };
        assert.throws(() => braces[method](invalid), /AST node value must be a string/);
        const internal = require(path.join(path.dirname(target), 'lib', method));
        assert.throws(() => internal(invalid), /AST node value must be a string/);
      }
    }
  `);
});

test('maxDepth can tighten the bound but cannot disable it', () => {
  isolated(`
    const deep = '{'.repeat(101) + 'a,b' + '}'.repeat(101);
    for (const maxDepth of [1000, Infinity, NaN, -1, '1000', false, null]) {
      for (const method of ['parse', 'compile', 'expand', 'stringify']) {
        rejected(() => braces[method](deep, { maxDepth }));
      }
    }
    for (const method of ['parse', 'compile', 'expand', 'stringify']) {
      rejected(() => braces[method]('{{a,b}}', { maxDepth: 1 }));
      assert.doesNotThrow(() => braces[method]('{a,b}', { maxDepth: 1 }));
    }
    rejected(() => braces.parse('{a,b}', { maxDepth: 0 }));
    rejected(() => braces.parse('{{a,b}}', { maxDepth: 1.9 }));
  `);
});

test('both fast-glob dependency paths resolve the patched implementation and reject the trigger', () => {
  if (process.env.WUWATERM_BRACES_UNDER_TEST) return;
  const nextRequire = createRequire(require.resolve('@next/eslint-plugin-next'));
  const dynamicRequire = createRequire(require.resolve('vite-plugin-dynamic-import'));
  for (const consumer of [nextRequire, dynamicRequire]) {
    const globPath = consumer.resolve('fast-glob');
    const mmRequire = createRequire(createRequire(globPath).resolve('micromatch'));
    assert.equal(mmRequire('braces'), braces);
    assert.equal(mmRequire('braces/package.json').name, '@wuwaterm/braces-patched');
    const glob = consumer('fast-glob');
    assert.throws(() => glob.sync('{'.repeat(4500) + 'a,b' + '}'.repeat(4500)), /maximum depth/);
  }
});
