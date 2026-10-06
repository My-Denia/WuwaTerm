import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { msg } from '../lib/messages.ts';

const root = new URL('../../', import.meta.url);

function read(path) {
  return readFileSync(new URL(path, root), 'utf8');
}

test('privacy copy states the leave limits and the unverified download', () => {
  const zh = msg('zh').privacy;
  const en = msg('en').privacy;
  assert.match(zh.s2p1, /不能确认文件已写入磁盘/);
  assert.match(zh.s4p1, /会先询问/);
  assert.match(zh.s4p1, /崩溃/);
  assert.match(zh.s4p1, /强制结束/);
  assert.equal(zh.footerUpdated, '说明更新：2026-10-06');
  assert.match(en.s2p1, /cannot confirm the file reached disk/);
  assert.match(en.s4p1, /asks first/);
  assert.match(en.s4p1, /crash/);
  assert.match(en.s4p1, /force-quitting/);
  assert.equal(en.footerUpdated, 'Note updated: 2026-10-06');
});

test('repo sentences that would otherwise overclaim now state the same limits', () => {
  const privacy = read('docs/privacy-and-llm.md').replace(/\s+/g, ' ');
  const sites = read('docs/sites.md').replace(/\s+/g, ' ');
  assert.doesNotMatch(privacy, /Results remain only in the current page and are cleared by refresh\./);
  assert.match(privacy, /asks before/);
  assert.match(privacy, /cannot confirm a manuscript download reached disk/);
  assert.match(privacy, /crash or forced quit/);
  assert.match(sites, /cannot confirm that download reached/);
  assert.match(sites, /ask first/);
  assert.match(sites, /crash or forced quit/);
  const readme = read('README.md');
  const readmeZh = read('README.zh-CN.md');
  assert.match(readme, /cannot confirm the download reached disk/);
  assert.match(readme, /ask first/);
  assert.match(readme, /crash or forced quit/);
  assert.match(readmeZh, /不能确认这次下载已写入磁盘/);
  assert.match(readmeZh, /会先询问/);
  assert.match(readmeZh, /崩溃或强制结束/);
});

test('home product note still describes a completed refresh and is not rewritten', () => {
  const zh = msg('zh').productNotes.find(item => item.title === '不保存历史');
  const en = msg('en').productNotes.find(item => item.title === 'No history kept');
  assert.equal(zh?.body, '结果仅在当前页面显示，刷新页面即清空。');
  assert.equal(en?.body, 'Results live only on this page; refreshing clears them.');
});
