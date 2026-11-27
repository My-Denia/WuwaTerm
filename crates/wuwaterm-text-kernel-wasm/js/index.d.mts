export const SEGMENT_LIMIT: number;

export class WuwaTermKernelError extends Error {}

export interface Segment {
  start: number;
  end: number;
  core_start: number;
  core_end: number;
  hard_cut: boolean;
  utf16_start?: number;
  utf16_end?: number;
  utf16_core_start?: number;
  utf16_core_end?: number;
}

export interface Region {
  start: number;
  end: number;
  core_start: number;
  core_end: number;
  utf16_start?: number;
  utf16_end?: number;
  utf16_core_start?: number;
  utf16_core_end?: number;
}

export type PairStatus =
  | "ready"
  | "needs_manual_split"
  | "untranslated"
  | "unpaired_target";

export interface SegmentPair {
  index: number;
  status: PairStatus;
  hard_cut: boolean;
  source: Region;
  target: Region;
}

export interface AlignmentSpan {
  start: number;
  end: number;
  text: string;
}

export interface AlignmentItem {
  source: AlignmentSpan;
  target: AlignmentSpan | null;
}

export interface TextSpansResult {
  core: [number, number];
  paragraphs: [number, number][];
  sentences: [number, number][];
  sentences_v2: [number, number][];
  review_sentences_v2: [number, number][];
}

export type PatternSpec = string | { surface: string; order: number };

export interface TermMatch {
  start: number;
  end: number;
  order: number;
  pattern_idx: number;
  utf16_start?: number;
  utf16_end?: number;
}

export interface CoordinateMaps {
  scalar_len: number;
  utf16_len: number;
  scalar_to_utf16: number[];
  utf16_to_scalar: number[];
}

export interface ScanOptions {
  select?: boolean;
  includeUtf16?: boolean;
}

export interface CoordinateOptions {
  includeUtf16?: boolean;
}

export class TermAutomatonHandle {
  readonly patternCount: number;
  readonly stateCount: number;
  scan(text: string, options?: ScanOptions): TermMatch[];
  free(): void;
}

export class WuwaTermTextKernel {
  version(): { version: string; segment_limit: number };
  textSpans(text: string): TextSpansResult;
  segmentText(text: string, limit?: number, options?: CoordinateOptions): Segment[];
  pairSegments(
    source: string,
    target: string,
    limit?: number,
    alignments?: AlignmentItem[] | null,
    options?: CoordinateOptions,
  ): SegmentPair[];
  scanTerms(surfaces: PatternSpec[], text: string, options?: ScanOptions): TermMatch[];
  createTermAutomaton(surfaces: PatternSpec[]): TermAutomatonHandle;
  scalarToUtf16(text: string, scalar: number): number;
  utf16ToScalar(text: string, utf16: number): number;
  coordinateMaps(text: string): CoordinateMaps;
}

export function loadKernel(
  source: BufferSource | WebAssembly.Module | Response | Promise<Response>,
): Promise<WuwaTermTextKernel>;
