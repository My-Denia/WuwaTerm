#!/usr/bin/env node
/**
 * Node.js verification for `wuwaterm-text-kernel-wasm`:
 * - Verifies all 40 fixtures in `contracts/segmentation-v1.json` (20 segment + 20 pair)
 *   with 0 diffs (including error cases).
 * - Verifies `scanTerms`, reusable `TermAutomatonHandle`, overlapping/priority semantics,
 *   and scalar <-> UTF-16 coordinate parity against JS UTF-16 string slicing.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { WuwaTermKernelError, loadKernel } from "./index.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..", "..");
const defaultWasmPath = path.join(
  repoRoot,
  "crates",
  "target",
  "wasm32-unknown-unknown",
  "release",
  "wuwaterm_text_kernel_wasm.wasm",
);

const wasmPath = process.argv[2] ? path.resolve(process.argv[2]) : defaultWasmPath;
const wasmBytes = fs.readFileSync(wasmPath);
const kernel = await loadKernel(wasmBytes);

// 1. Version metadata
const ver = kernel.version();
assert.equal(ver.segment_limit, 1800);

// 2. contracts/segmentation-v1.json (20 segment + 20 pair = 40 cases)
const fixturePath = path.join(repoRoot, "contracts", "segmentation-v1.json");
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf-8"));
const limit = fixture.limit;

let segPassed = 0;
for (const c of fixture.segment_cases) {
  const got = kernel.segmentText(c.text, limit);
  assert.deepEqual(got, c.segments, `segment_case mismatch on ${c.name}`);
  segPassed += 1;
}

let pairPassed = 0;
for (const c of fixture.pair_cases) {
  const alignments = Object.prototype.hasOwnProperty.call(c, "alignments")
    ? c.alignments
    : null;
  if (Object.prototype.hasOwnProperty.call(c, "error")) {
    let threw = false;
    try {
      kernel.pairSegments(c.source, c.target, limit, alignments);
    } catch (err) {
      threw = err instanceof WuwaTermKernelError;
    }
    assert.ok(threw, `expected error on pair_case ${c.name}`);
  } else {
    const got = kernel.pairSegments(c.source, c.target, limit, alignments);
    assert.deepEqual(got, c.pairs, `pair_case mismatch on ${c.name}`);
  }
  pairPassed += 1;
}

// 3. UTF-16 <-> Unicode scalar coordinate conversion & JS String.prototype.slice parity
const astralText = "🌊椿A🚀𠀀终";
const scalars = Array.from(astralText);
const maps = kernel.coordinateMaps(astralText);
assert.equal(maps.scalar_len, scalars.length);
assert.equal(maps.utf16_len, astralText.length);

let expectedU16 = 0;
for (let s = 0; s <= scalars.length; s += 1) {
  assert.equal(kernel.scalarToUtf16(astralText, s), expectedU16);
  assert.equal(maps.scalar_to_utf16[s], expectedU16);
  assert.equal(kernel.utf16ToScalar(astralText, expectedU16), s);
  assert.equal(maps.utf16_to_scalar[expectedU16], s);
  if (s < scalars.length) {
    const width = scalars[s].length;
    if (width === 2) {
      // Mid-surrogate offset clamps back to owning scalar index
      assert.equal(kernel.utf16ToScalar(astralText, expectedU16 + 1), s);
      assert.equal(maps.utf16_to_scalar[expectedU16 + 1], s);
    }
    expectedU16 += width;
  }
}

// 4. Aho-Corasick scanTerms + reusable TermAutomatonHandle + overlaps & UTF-16 slicing
const surfaces = ["声骸", "声骸技能", "技能", "ab", "bab"];
const scanText = "🌊使用声骸技能ababa✨";

const oneShotSelected = kernel.scanTerms(surfaces, scanText, {
  select: true,
  includeUtf16: true,
});
assert.deepEqual(
  oneShotSelected.map((m) => [m.start, m.end, m.order]),
  [
    [3, 7, 1],
    [8, 11, 4],
  ],
);
// Verify UTF-16 slices match exact matched substrings in JS!
assert.equal(
  scanText.slice(oneShotSelected[0].utf16_start, oneShotSelected[0].utf16_end),
  "声骸技能",
);
assert.equal(
  scanText.slice(oneShotSelected[1].utf16_start, oneShotSelected[1].utf16_end),
  "bab",
);

const rawOverlaps = kernel.scanTerms(surfaces, scanText, { select: false });
// Must emit all overlapping occurrences: 声骸(3,5), 声骸技能(3,7), 技能(5,7), ab(7,9), bab(8,11), ab(9,11)
const rawTriples = rawOverlaps
  .map((m) => [m.start, m.end, m.order])
  .sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
assert.deepEqual(rawTriples, [
  [3, 5, 0],
  [3, 7, 1],
  [5, 7, 2],
  [7, 9, 3],
  [8, 11, 4],
  [9, 11, 3],
]);

// Reusable handle lifecycle
const ac = kernel.createTermAutomaton(surfaces);
assert.equal(ac.patternCount, surfaces.length);
assert.ok(ac.stateCount > 0);
assert.deepEqual(ac.scan(scanText, { select: true, includeUtf16: true }), oneShotSelected);
ac.free();
assert.throws(() => ac.scan(scanText), WuwaTermKernelError);

// 5. textSpans sanity check
const spans = kernel.textSpans("1. First item. 3.7 和 3²．5。下一句。");
assert.deepEqual(spans.sentences_v2, [
  [0, 2],
  [2, 14],
  [14, 26],
  [26, 30],
]);
assert.deepEqual(spans.review_sentences_v2, [
  [0, 14],
  [14, 26],
  [26, 30],
]);

console.log(
  `WASM_CONTRACTS PASS: segment=${segPassed}/${fixture.segment_cases.length} ` +
    `pair=${pairPassed}/${fixture.pair_cases.length} ` +
    `wasm_bytes=${wasmBytes.length} version=${ver.version}`,
);
