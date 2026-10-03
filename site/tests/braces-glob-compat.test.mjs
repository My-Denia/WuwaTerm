import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { globFiles } from 'vite-plugin-dynamic-import';
import { parse } from 'acorn';

const require = createRequire(import.meta.url);
const nextRequire = createRequire(require.resolve('@next/eslint-plugin-next'));
const dynamicRequire = createRequire(require.resolve('vite-plugin-dynamic-import'));

function fixture(t) {
  const cwd = mkdtempSync(path.join(tmpdir(), 'wuwaterm-glob-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  for (const file of ['a.ts', 'b.js', '.hidden.ts', 'x/a.ts', 'x/b.js', 'y/a.ts',
    'num/1.ts', 'num/2.ts', 'num/3.ts', 'num/01.ts', 'num/02.ts', 'num/03.ts',
    'literal/{a,b}.ts', 'views/a.ts', 'views/b.js', 'views/c/index.ts']) {
    const filename = path.join(cwd, file);
    mkdirSync(path.dirname(filename), { recursive: true });
    writeFileSync(filename, 'export default 1;\n');
  }
  symlinkSync('x', path.join(cwd, 'link-dir'));
  symlinkSync('a.ts', path.join(cwd, 'link.ts'));
  symlinkSync('missing', path.join(cwd, 'broken'));
  return cwd;
}

test('both original fast-glob versions preserve matching, options, links and filesystem failures', async t => {
  const cwd = fixture(t);
  for (const consumer of [nextRequire, dynamicRequire]) {
    const glob = consumer('fast-glob');
    const version = consumer('fast-glob/package.json').version;
    const cases = [
      ['{a,b}.{js,ts}', {}, ['a.ts', 'b.js']],
      ['{x,{y,x}}/*.ts', {}, ['x/a.ts', 'y/a.ts']],
      ['num/{1..3..2}.ts', {}, ['num/1.ts', 'num/3.ts']],
      ['num/{01..03}.ts', {}, ['num/01.ts', 'num/02.ts', 'num/03.ts']],
      ['num/{3..1}.ts', {}, ['num/1.ts', 'num/2.ts', 'num/3.ts']],
      [['{x,y}/*', '!**/*.js'], {}, ['x/a.ts', 'y/a.ts']],
      ['{x,y}/*', { ignore: ['y/**'] }, ['x/a.ts', 'x/b.js']],
      ['*.ts', { dot: true }, ['.hidden.ts', 'a.ts', 'link.ts']],
      ['{x,y,link-dir}', { onlyDirectories: true }, ['link-dir', 'x', 'y']],
      ['link-dir/*', { followSymbolicLinks: true }, ['link-dir/a.ts', 'link-dir/b.js']],
      ['link-dir', { onlyDirectories: true, followSymbolicLinks: false }, []],
      ['broken', {}, []],
      ['{missing,absent}/*', {}, []],
      [['x/*.ts', 'x/*.ts'], {}, ['x/a.ts']],
      ['literal/\\{a,b\\}.ts', {}, version === '3.3.1' ? [] : ['literal/{a,b}.ts']],
      ['x/a.ts', { absolute: true }, [path.join(cwd, 'x/a.ts')]],
      [path.join(cwd, '{x,y}/*.ts'), {}, [path.join(cwd, 'x/a.ts'), path.join(cwd, 'y/a.ts')]],
      ['../{x,y}/*.ts', { cwd: path.join(cwd, 'num') }, ['../x/a.ts', '../y/a.ts']],
    ];
    for (const [patterns, options, expected] of cases) {
      assert.deepEqual(glob.sync(patterns, { cwd, ...options }).sort(), expected.sort(), `${version}: ${patterns}`);
      assert.deepEqual((await glob(patterns, { cwd, ...options })).sort(), expected.sort(), `${version} async: ${patterns}`);
    }
    assert.deepEqual(glob.sync(['y/*.ts', 'x/*.ts'], { cwd }), ['y/a.ts', 'x/a.ts']);
    assert.throws(() => glob.sync('', { cwd }), TypeError);
    assert.throws(() => glob.sync(['x/*', ''], { cwd }), TypeError);
    assert.deepEqual(glob.sync([], { cwd }), []);
    const denied = Object.assign(new Error('fixture permission denied'), { code: 'EACCES' });
    assert.throws(() => glob.sync('**/*', { cwd, fs: { readdirSync() { throw denied; } } }), error => error === denied);
  }
});

test('Next ESLint root-directory discovery retains brace ranges and directory-only results', t => {
  const cwd = fixture(t);
  const { getRootDirs } = require(path.join(path.dirname(require.resolve('@next/eslint-plugin-next')), 'utils/get-root-dirs.js'));
  assert.deepEqual(getRootDirs({ cwd, settings: {} }), [cwd]);
  assert.deepEqual(getRootDirs({ cwd, settings: { next: { rootDir: path.join(cwd, '{x,y}') } } }).sort(),
    [path.join(cwd, 'x'), path.join(cwd, 'y')]);
  assert.deepEqual(getRootDirs({ cwd, settings: { next: { rootDir: [path.join(cwd, 'x'), path.join(cwd, 'y')] } } }),
    [path.join(cwd, 'x'), path.join(cwd, 'y')]);
  assert.throws(() => getRootDirs({ cwd, settings: { next: { rootDir: '{'.repeat(4500) + 'a,b' + '}'.repeat(4500) } } }), /maximum depth/);
});

test('Vite dynamic imports still discover extension alternatives and directory index modules', async t => {
  const cwd = fixture(t);
  const expression = 'import(`./views/${name}`)';
  const ast = parse(expression, { ecmaVersion: 'latest', sourceType: 'module' });
  const result = await globFiles({
    importeeNode: ast.body[0].expression.source,
    importExpression: expression,
    importer: path.join(cwd, 'entry.ts'),
    resolve: { tryResolve: async () => undefined },
    extensions: ['.ts', '.js'],
    loose: false,
  });
  assert.deepEqual(result.files.sort(), ['./views/a.ts', './views/b.js', './views/c/index.ts']);
});
