//! Reference segmentation-v1 implementation in pure Rust (`std`-only).
//!
//! Direct port of `wuwaterm.segmentation` (`docs/segmentation-v1.md`). All
//! offsets are Unicode scalar indices.

use std::fmt;

use crate::text_spans::{
    is_spec_whitespace, paragraph_ranges_chars, segment_core_chars, sentence_ranges_v2_chars,
};

pub const SEGMENT_LIMIT: usize = 1800;
pub const HARD_CUT_WINDOW: usize = 200;

pub const STATUS_READY: &str = "ready";
pub const STATUS_NEEDS_MANUAL_SPLIT: &str = "needs_manual_split";
pub const STATUS_UNTRANSLATED: &str = "untranslated";
pub const STATUS_UNPAIRED_TARGET: &str = "unpaired_target";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PairStatus {
    Ready,
    NeedsManualSplit,
    Untranslated,
    UnpairedTarget,
}

impl PairStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Ready => STATUS_READY,
            Self::NeedsManualSplit => STATUS_NEEDS_MANUAL_SPLIT,
            Self::Untranslated => STATUS_UNTRANSLATED,
            Self::UnpairedTarget => STATUS_UNPAIRED_TARGET,
        }
    }

    pub fn code(self) -> u8 {
        match self {
            Self::Ready => 0,
            Self::NeedsManualSplit => 1,
            Self::Untranslated => 2,
            Self::UnpairedTarget => 3,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Segment {
    pub start: usize,
    pub end: usize,
    pub core_start: usize,
    pub core_end: usize,
    pub hard_cut: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Region {
    pub start: usize,
    pub end: usize,
    pub core_start: usize,
    pub core_end: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SegmentPair {
    pub index: usize,
    pub status: PairStatus,
    pub hard_cut: bool,
    pub source: Region,
    pub target: Region,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlignmentSpan {
    pub start: usize,
    pub end: usize,
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlignmentItem {
    pub source: AlignmentSpan,
    pub target: Option<AlignmentSpan>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SegError(pub String);

impl fmt::Display for SegError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

#[inline]
pub fn is_boundary_punctuation(c: char) -> bool {
    matches!(c, '，' | '、' | '；' | '：' | ',' | ';' | ':')
}

#[inline]
pub fn is_natural(c: char) -> bool {
    is_spec_whitespace(c) || is_boundary_punctuation(c)
}

#[inline]
fn core_len(chars: &[char], start: usize, end: usize) -> usize {
    let (a, b) = segment_core_chars(chars, start, end);
    b - a
}

fn sentence_pieces(chars: &[char], start: usize, end: usize) -> Vec<(usize, usize)> {
    let (cs, ce) = segment_core_chars(chars, start, end);
    if cs == ce {
        return vec![(start, end)];
    }
    let mut cuts: Vec<usize> = Vec::new();
    for &(range_start, _) in sentence_ranges_v2_chars(&chars[cs..ce]).iter().skip(1) {
        let mut p = cs + range_start;
        while p < ce && is_spec_whitespace(chars[p]) {
            p += 1;
        }
        let prev = cuts.last().copied().unwrap_or(cs);
        if p < ce && p > prev {
            cuts.push(p);
        }
    }
    let mut out = Vec::with_capacity(cuts.len() + 1);
    let mut prev = start;
    for c in cuts {
        out.push((prev, c));
        prev = c;
    }
    out.push((prev, end));
    out
}

fn hard_cut_point(chars: &[char], pos: usize, limit: usize) -> usize {
    let hi = pos + limit;
    let lo = hi.saturating_sub(HARD_CUT_WINDOW).max(pos + 1);
    for p in (lo..=hi).rev() {
        if is_spec_whitespace(chars[p - 1]) {
            return p;
        }
    }
    for p in (lo..=hi).rev() {
        if is_boundary_punctuation(chars[p - 1]) {
            return p;
        }
    }
    hi
}

fn hard_cut(chars: &[char], start: usize, end: usize, limit: usize) -> Vec<(usize, usize)> {
    let (s, e) = segment_core_chars(chars, start, end);
    let mut cuts: Vec<usize> = Vec::new();
    let mut pos = s;
    while e - pos > limit {
        pos = hard_cut_point(chars, pos, limit);
        cuts.push(pos);
    }
    let mut bounds = Vec::with_capacity(cuts.len() + 2);
    bounds.push(start);
    bounds.extend_from_slice(&cuts);
    bounds.push(end);

    let mut pieces: Vec<(usize, usize)> = Vec::new();
    for w in bounds.windows(2) {
        let (a, b) = (w[0], w[1]);
        if !pieces.is_empty() && core_len(chars, a, b) == 0 {
            let last = pieces.last_mut().expect("non-empty pieces");
            last.1 = b;
        } else {
            pieces.push((a, b));
        }
    }
    pieces
}

fn check_partition(regions: &[(usize, usize)], length: usize, name: &str) {
    let mut pos = 0usize;
    for &(start, end) in regions {
        assert!(
            start == pos && end >= start,
            "{name} regions do not partition the text"
        );
        pos = end;
    }
    assert_eq!(pos, length, "{name} regions do not cover the text");
}

pub fn segment_text(text: &str, limit: usize) -> Vec<Segment> {
    let chars: Vec<char> = text.chars().collect();
    segment_chars(&chars, limit)
}

pub fn segment_chars(chars: &[char], limit: usize) -> Vec<Segment> {
    let length = chars.len();
    if length == 0 {
        return Vec::new();
    }
    let paragraphs = paragraph_ranges_chars(chars);
    if paragraphs.is_empty() {
        return vec![Segment {
            start: 0,
            end: length,
            core_start: 0,
            core_end: 0,
            hard_cut: false,
        }];
    }

    let mut units: Vec<(usize, usize, bool)> = Vec::new();
    for (p_start, p_end) in paragraphs {
        if core_len(chars, p_start, p_end) <= limit {
            units.push((p_start, p_end, false));
            continue;
        }
        for (s_start, s_end) in sentence_pieces(chars, p_start, p_end) {
            if core_len(chars, s_start, s_end) <= limit {
                units.push((s_start, s_end, false));
            } else {
                for (h_start, h_end) in hard_cut(chars, s_start, s_end, limit) {
                    units.push((h_start, h_end, true));
                }
            }
        }
    }

    let mut segments: Vec<Segment> = Vec::new();
    let mut cur: Option<Segment> = None;
    for (start, end, hard) in units {
        let (core_start, core_end) = segment_core_chars(chars, start, end);
        let nxt = Segment {
            start,
            end,
            core_start,
            core_end,
            hard_cut: hard,
        };
        match cur {
            None => cur = Some(nxt),
            Some(c)
                if !c.hard_cut && !nxt.hard_cut && nxt.core_end - c.core_start <= limit =>
            {
                cur = Some(Segment {
                    start: c.start,
                    end: nxt.end,
                    core_start: c.core_start,
                    core_end: nxt.core_end,
                    hard_cut: false,
                });
            }
            Some(c) => {
                segments.push(c);
                cur = Some(nxt);
            }
        }
    }
    if let Some(c) = cur {
        segments.push(c);
    }

    let bounds: Vec<(usize, usize)> = segments.iter().map(|s| (s.start, s.end)).collect();
    check_partition(&bounds, length, "segment");
    segments
}

#[derive(Debug, Clone, Copy)]
struct RawPair {
    status: PairStatus,
    hard_cut: bool,
    s_start: usize,
    s_end: usize,
    t_start: usize,
    t_end: usize,
}

fn coerce_span(name: &str, span: &AlignmentSpan, chars: &[char]) -> Result<(usize, usize), SegError> {
    if span.start >= span.end || span.end > chars.len() {
        return Err(SegError(format!("{name} span is out of bounds")));
    }
    if span.text.chars().ne(chars[span.start..span.end].iter().copied()) {
        return Err(SegError(format!("{name} span text does not match the text")));
    }
    Ok((span.start, span.end))
}

#[inline]
fn has_non_ws(chars: &[char], start: usize, end: usize) -> bool {
    chars[start..end].iter().any(|&ch| !is_spec_whitespace(ch))
}

fn alignment_region_pairs(
    src_chars: &[char],
    tgt_chars: &[char],
    alignments: &[AlignmentItem],
) -> Result<Option<Vec<(usize, usize, usize, usize)>>, SegError> {
    let mut src_spans: Vec<(usize, usize)> = Vec::with_capacity(alignments.len());
    let mut tgt_spans: Vec<Option<(usize, usize)>> = Vec::with_capacity(alignments.len());
    let mut prev_s = 0usize;
    let mut prev_t = 0usize;

    for item in alignments {
        let s_span = coerce_span("source", &item.source, src_chars)?;
        let t_span = match &item.target {
            None => None,
            Some(ts) => Some(coerce_span("target", ts, tgt_chars)?),
        };
        if s_span.0 < prev_s {
            return Err(SegError("source spans are out of order or overlap".to_string()));
        }
        prev_s = s_span.1;
        if let Some(ts) = t_span {
            if ts.0 < prev_t {
                return Err(SegError("target spans are out of order or overlap".to_string()));
            }
            prev_t = ts.1;
        }
        src_spans.push(s_span);
        tgt_spans.push(t_span);
    }

    let mut pos = 0usize;
    for &(start, end) in &src_spans {
        if has_non_ws(src_chars, pos, start) {
            return Ok(None);
        }
        pos = end;
    }
    if has_non_ws(src_chars, pos, src_chars.len()) {
        return Ok(None);
    }

    pos = 0;
    for &t_span in &tgt_spans {
        let Some((start, end)) = t_span else {
            continue;
        };
        if has_non_ws(tgt_chars, pos, start) {
            return Ok(None);
        }
        pos = end;
    }
    if has_non_ws(tgt_chars, pos, tgt_chars.len()) {
        return Ok(None);
    }

    let n = alignments.len();
    let mut s_bounds = Vec::with_capacity(n + 1);
    s_bounds.push(0usize);
    for span in src_spans.iter().skip(1) {
        s_bounds.push(span.0);
    }
    s_bounds.push(src_chars.len());

    let mut t_bounds = vec![0usize; n + 1];
    t_bounds[n] = tgt_chars.len();
    for i in (1..n).rev() {
        t_bounds[i] = match tgt_spans[i] {
            Some((start, _)) => start,
            None => t_bounds[i + 1],
        };
    }

    let out = (0..n)
        .map(|i| (s_bounds[i], s_bounds[i + 1], t_bounds[i], t_bounds[i + 1]))
        .collect();
    Ok(Some(out))
}

fn drop_empty_pairs(
    src_chars: &[char],
    tgt_chars: &[char],
    regions: &[(usize, usize, usize, usize)],
) -> Vec<(usize, usize, usize, usize)> {
    let kept: Vec<(usize, usize, usize, usize)> = regions
        .iter()
        .copied()
        .filter(|&(ss, se, ts, te)| {
            core_len(src_chars, ss, se) > 0 || core_len(tgt_chars, ts, te) > 0
        })
        .collect();

    let mut out = Vec::with_capacity(kept.len());
    for (k, &r) in kept.iter().enumerate() {
        let s_start = if k == 0 { 0 } else { r.0 };
        let t_start = if k == 0 { 0 } else { r.2 };
        let (s_end, t_end) = if k + 1 < kept.len() {
            (kept[k + 1].0, kept[k + 1].2)
        } else {
            (src_chars.len(), tgt_chars.len())
        };
        out.push((s_start, s_end, t_start, t_end));
    }
    out
}

fn fallback_cuts(chars: &[char], a: usize, b: usize, k: usize) -> Option<Vec<usize>> {
    let mut cuts = Vec::with_capacity(k.saturating_sub(1));
    let mut prev = a;
    let n = b - a;
    for i in 1..k {
        let t_i = a + (i * n) / k;
        let mut found = None;
        for d in 0..=HARD_CUT_WINDOW {
            for cand in [t_i.checked_sub(d), t_i.checked_add(d)].into_iter().flatten() {
                if a < cand && cand < b && cand > prev && is_natural(chars[cand - 1]) {
                    found = Some(cand);
                    break;
                }
            }
            if found.is_some() {
                break;
            }
        }
        let p = found?;
        cuts.push(p);
        prev = p;
    }
    Some(cuts)
}

fn fallback_pairs(
    src_chars: &[char],
    tgt_chars: &[char],
    s_range: (usize, usize),
    t_range: (usize, usize),
    limit: usize,
) -> Vec<RawPair> {
    let (s_a, s_b) = segment_core_chars(src_chars, s_range.0, s_range.1);
    let (t_a, t_b) = segment_core_chars(tgt_chars, t_range.0, t_range.1);
    let k = (s_b - s_a).div_ceil(limit).max((t_b - t_a).div_ceil(limit));
    let manual = || {
        vec![RawPair {
            status: PairStatus::NeedsManualSplit,
            hard_cut: false,
            s_start: s_range.0,
            s_end: s_range.1,
            t_start: t_range.0,
            t_end: t_range.1,
        }]
    };
    let (Some(s_cuts), Some(t_cuts)) = (
        fallback_cuts(src_chars, s_a, s_b, k),
        fallback_cuts(tgt_chars, t_a, t_b, k),
    ) else {
        return manual();
    };
    let mut s_bounds = Vec::with_capacity(s_cuts.len() + 2);
    s_bounds.push(s_range.0);
    s_bounds.extend_from_slice(&s_cuts);
    s_bounds.push(s_range.1);

    let mut t_bounds = Vec::with_capacity(t_cuts.len() + 2);
    t_bounds.push(t_range.0);
    t_bounds.extend_from_slice(&t_cuts);
    t_bounds.push(t_range.1);

    let mut pairs = Vec::with_capacity(k);
    for i in 0..k {
        let s_piece = (s_bounds[i], s_bounds[i + 1]);
        let t_piece = (t_bounds[i], t_bounds[i + 1]);
        let s_len = core_len(src_chars, s_piece.0, s_piece.1);
        let t_len = core_len(tgt_chars, t_piece.0, t_piece.1);
        if !(0 < s_len && s_len <= limit && 0 < t_len && t_len <= limit) {
            return manual();
        }
        pairs.push(RawPair {
            status: PairStatus::Ready,
            hard_cut: true,
            s_start: s_piece.0,
            s_end: s_piece.1,
            t_start: t_piece.0,
            t_end: t_piece.1,
        });
    }
    pairs
}

fn classify_region_pair(
    src_chars: &[char],
    tgt_chars: &[char],
    r: (usize, usize, usize, usize),
    limit: usize,
    aligned: bool,
) -> Vec<RawPair> {
    let (s_start, s_end, t_start, t_end) = r;
    let s_len = core_len(src_chars, s_start, s_end);
    let t_len = core_len(tgt_chars, t_start, t_end);
    if s_len > 0 && t_len == 0 {
        return vec![RawPair {
            status: PairStatus::Untranslated,
            hard_cut: false,
            s_start,
            s_end,
            t_start,
            t_end,
        }];
    }
    if t_len > 0 && s_len == 0 {
        return vec![RawPair {
            status: PairStatus::UnpairedTarget,
            hard_cut: false,
            s_start,
            s_end,
            t_start,
            t_end,
        }];
    }
    if s_len <= limit && t_len <= limit {
        return vec![RawPair {
            status: PairStatus::Ready,
            hard_cut: false,
            s_start,
            s_end,
            t_start,
            t_end,
        }];
    }
    if !aligned {
        return vec![RawPair {
            status: PairStatus::NeedsManualSplit,
            hard_cut: false,
            s_start,
            s_end,
            t_start,
            t_end,
        }];
    }
    let s_sents = sentence_pieces(src_chars, s_start, s_end);
    let t_sents = sentence_pieces(tgt_chars, t_start, t_end);
    if s_sents.len() != t_sents.len() {
        return vec![RawPair {
            status: PairStatus::NeedsManualSplit,
            hard_cut: false,
            s_start,
            s_end,
            t_start,
            t_end,
        }];
    }
    let mut pairs = Vec::with_capacity(s_sents.len());
    for (s_piece, t_piece) in s_sents.into_iter().zip(t_sents.into_iter()) {
        if core_len(src_chars, s_piece.0, s_piece.1) <= limit
            && core_len(tgt_chars, t_piece.0, t_piece.1) <= limit
        {
            pairs.push(RawPair {
                status: PairStatus::Ready,
                hard_cut: false,
                s_start: s_piece.0,
                s_end: s_piece.1,
                t_start: t_piece.0,
                t_end: t_piece.1,
            });
        } else {
            pairs.extend(fallback_pairs(src_chars, tgt_chars, s_piece, t_piece, limit));
        }
    }
    pairs
}

fn validate_pairs(
    src_len: usize,
    tgt_len: usize,
    pairs: &[SegmentPair],
    limit: usize,
) {
    for pair in pairs {
        if pair.status != PairStatus::Ready {
            continue;
        }
        for region in [pair.source, pair.target] {
            let len = region.core_end - region.core_start;
            assert!(
                len > 0 && len <= limit,
                "ready pair core is empty or over the limit"
            );
        }
    }
    let s_bounds: Vec<(usize, usize)> = pairs.iter().map(|p| (p.source.start, p.source.end)).collect();
    let t_bounds: Vec<(usize, usize)> = pairs.iter().map(|p| (p.target.start, p.target.end)).collect();
    check_partition(&s_bounds, src_len, "source");
    check_partition(&t_bounds, tgt_len, "target");
}

pub fn pair_segments(
    source: &str,
    target: &str,
    limit: usize,
    alignments: Option<&[AlignmentItem]>,
) -> Result<Vec<SegmentPair>, SegError> {
    let src_chars: Vec<char> = source.chars().collect();
    let tgt_chars: Vec<char> = target.chars().collect();
    pair_chars(&src_chars, &tgt_chars, limit, alignments)
}

pub fn pair_chars(
    src_chars: &[char],
    tgt_chars: &[char],
    limit: usize,
    alignments: Option<&[AlignmentItem]>,
) -> Result<Vec<SegmentPair>, SegError> {
    let non_empty_alignments = alignments.filter(|a| !a.is_empty());

    if !has_non_ws(src_chars, 0, src_chars.len())
        && !has_non_ws(tgt_chars, 0, tgt_chars.len())
    {
        if let Some(a) = non_empty_alignments {
            let _ = alignment_region_pairs(src_chars, tgt_chars, a)?;
        }
        return Ok(Vec::new());
    }

    let whole = (0, src_chars.len(), 0, tgt_chars.len());
    let mut pairs: Vec<RawPair> = Vec::new();
    if let Some(a) = non_empty_alignments {
        match alignment_region_pairs(src_chars, tgt_chars, a)? {
            None => {
                pairs.push(RawPair {
                    status: PairStatus::NeedsManualSplit,
                    hard_cut: false,
                    s_start: whole.0,
                    s_end: whole.1,
                    t_start: whole.2,
                    t_end: whole.3,
                });
            }
            Some(regions) => {
                for r in drop_empty_pairs(src_chars, tgt_chars, &regions) {
                    pairs.extend(classify_region_pair(src_chars, tgt_chars, r, limit, true));
                }
            }
        }
    } else {
        let s_paras = paragraph_ranges_chars(src_chars);
        let t_paras = paragraph_ranges_chars(tgt_chars);
        if !s_paras.is_empty() && s_paras.len() == t_paras.len() {
            for ((a, b), (c, d)) in s_paras.into_iter().zip(t_paras.into_iter()) {
                pairs.extend(classify_region_pair(
                    src_chars,
                    tgt_chars,
                    (a, b, c, d),
                    limit,
                    true,
                ));
            }
        } else {
            pairs = classify_region_pair(src_chars, tgt_chars, whole, limit, false);
        }
    }

    let mut merged: Vec<RawPair> = Vec::with_capacity(pairs.len());
    for pair in pairs {
        if let Some(prev) = merged.last_mut()
            && prev.status == PairStatus::Ready
            && pair.status == PairStatus::Ready
            && !prev.hard_cut
            && !pair.hard_cut
        {
            let (s_a, _) = segment_core_chars(src_chars, prev.s_start, prev.s_end);
            let (_, s_b) = segment_core_chars(src_chars, pair.s_start, pair.s_end);
            let (t_a, _) = segment_core_chars(tgt_chars, prev.t_start, prev.t_end);
            let (_, t_b) = segment_core_chars(tgt_chars, pair.t_start, pair.t_end);
            if s_b - s_a <= limit && t_b - t_a <= limit {
                prev.s_end = pair.s_end;
                prev.t_end = pair.t_end;
                continue;
            }
        }
        merged.push(pair);
    }

    let result: Vec<SegmentPair> = merged
        .into_iter()
        .enumerate()
        .map(|(index, p)| {
            let (s_cs, s_ce) = segment_core_chars(src_chars, p.s_start, p.s_end);
            let (t_cs, t_ce) = segment_core_chars(tgt_chars, p.t_start, p.t_end);
            SegmentPair {
                index,
                status: p.status,
                hard_cut: p.hard_cut,
                source: Region {
                    start: p.s_start,
                    end: p.s_end,
                    core_start: s_cs,
                    core_end: s_ce,
                },
                target: Region {
                    start: p.t_start,
                    end: p.t_end,
                    core_start: t_cs,
                    core_end: t_ce,
                },
            }
        })
        .collect();

    validate_pairs(src_chars.len(), tgt_chars.len(), &result, limit);
    Ok(result)
}
