//! Pure-algorithm shared text kernel for WuwaTerm.
//!
//! Zero external runtime dependencies (`std` only): deterministic Unicode
//! coordinate mapping, frozen whitespace/digit predicates, `segmentation-v1`
//! (`segment_text`, `pair_segments`, `paragraph_ranges`, `sentence_ranges`,
//! `sentence_ranges_v2`, `review_sentence_ranges_v2`), and UTF-8 Aho–Corasick
//! multi-pattern term scanning with WuwaTerm exact overlap/priority semantics.
//!
//! Shared across Core (PyO3 via `wuwaterm-text-kernel-py`) and Web/Workers
//! (WASM via `wuwaterm-text-kernel-wasm`).

pub mod aho_corasick;
pub mod segmentation;
pub mod text_spans;

pub const KERNEL_VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    use crate::aho_corasick::{
        AhoCorasick, TermPattern, find_all_naive, select_longest_non_overlapping,
    };
    use crate::segmentation::{
        AlignmentItem, AlignmentSpan, PairStatus, SEGMENT_LIMIT, pair_segments, segment_text,
    };
    use crate::text_spans::{
        build_byte_to_scalar_map, build_scalar_to_utf16_map, build_utf16_to_scalar_map,
        is_py_isdigit, is_py_isspace, is_spec_whitespace, paragraph_ranges,
        review_sentence_ranges_v2, scalar_len, scalar_to_utf8, scalar_to_utf16, segment_core,
        sentence_ranges, sentence_ranges_v2, utf8_to_scalar, utf16_len, utf16_to_scalar,
    };

    #[test]
    fn scalar_utf16_and_utf8_coordinate_roundtrips() {
        let s = "🌊椿A🚀";
        assert_eq!(scalar_len(s), 4);
        assert_eq!(utf16_len(s), 6);

        // Scalar -> UTF-16
        assert_eq!(scalar_to_utf16(s, 0), 0);
        assert_eq!(scalar_to_utf16(s, 1), 2);
        assert_eq!(scalar_to_utf16(s, 2), 3);
        assert_eq!(scalar_to_utf16(s, 3), 4);
        assert_eq!(scalar_to_utf16(s, 4), 6);
        assert_eq!(scalar_to_utf16(s, 99), 6);

        // UTF-16 -> Scalar (mid-surrogate clamps down to owning scalar)
        assert_eq!(utf16_to_scalar(s, 0), 0);
        assert_eq!(utf16_to_scalar(s, 1), 0);
        assert_eq!(utf16_to_scalar(s, 2), 1);
        assert_eq!(utf16_to_scalar(s, 3), 2);
        assert_eq!(utf16_to_scalar(s, 4), 3);
        assert_eq!(utf16_to_scalar(s, 5), 3);
        assert_eq!(utf16_to_scalar(s, 6), 4);
        assert_eq!(utf16_to_scalar(s, 99), 4);

        // Scalar <-> UTF-8 byte offsets ("🌊"=4, "椿"=3, "A"=1, "🚀"=4)
        assert_eq!(scalar_to_utf8(s, 0), 0);
        assert_eq!(scalar_to_utf8(s, 1), 4);
        assert_eq!(scalar_to_utf8(s, 2), 7);
        assert_eq!(scalar_to_utf8(s, 3), 8);
        assert_eq!(scalar_to_utf8(s, 4), 12);
        assert_eq!(utf8_to_scalar(s, 0), 0);
        assert_eq!(utf8_to_scalar(s, 3), 0);
        assert_eq!(utf8_to_scalar(s, 4), 1);
        assert_eq!(utf8_to_scalar(s, 7), 2);
        assert_eq!(utf8_to_scalar(s, 8), 3);
        assert_eq!(utf8_to_scalar(s, 12), 4);

        // Batch maps agree with point lookups
        let s2u16 = build_scalar_to_utf16_map(s);
        assert_eq!(s2u16, vec![0, 2, 3, 4, 6]);
        let u162s = build_utf16_to_scalar_map(s);
        assert_eq!(u162s, vec![0, 0, 1, 2, 3, 3, 4]);
        let b2s = build_byte_to_scalar_map(s);
        assert_eq!(b2s[0], 0);
        assert_eq!(b2s[4], 1);
        assert_eq!(b2s[7], 2);
        assert_eq!(b2s[8], 3);
        assert_eq!(b2s[12], 4);
    }

    #[test]
    fn frozen_unicode_predicates_match_spec() {
        // Dual whitespace predicates: U+001C..=U+001F & U+0085 are py_isspace only
        for ch in ['\u{001C}', '\u{001D}', '\u{001E}', '\u{001F}', '\u{0085}'] {
            assert!(!is_spec_whitespace(ch));
            assert!(is_py_isspace(ch));
        }
        for ch in [' ', '\t', '\n', '\u{00A0}', '\u{1680}', '\u{202F}', '\u{3000}'] {
            assert!(is_spec_whitespace(ch));
            assert!(is_py_isspace(ch));
        }
        // Python 3.12 (Unicode 15.0.0) digit table includes superscripts & Kawi/Nag Mundari, excludes fractions
        assert!(is_py_isdigit('7'));
        assert!(is_py_isdigit('²'));
        assert!(is_py_isdigit('³'));
        assert!(is_py_isdigit('３'));
        assert!(is_py_isdigit('\u{11F50}')); // Kawi digit 0 (Unicode 15.0)
        assert!(is_py_isdigit('\u{1E4F9}')); // Nag Mundari digit 9 (Unicode 15.0)
        assert!(!is_py_isdigit('½'));
        assert!(!is_py_isdigit('A'));
    }

    #[test]
    fn unicode_digits_and_list_markers_match_spec() {
        assert_eq!(sentence_ranges("3.7。下一句。"), vec![(0, 2), (2, 4), (4, 8)]);
        assert_eq!(
            sentence_ranges_v2("3.7 和 3²．5。下一句。"),
            vec![(0, 11), (11, 15)]
        );
        assert_eq!(sentence_ranges_v2("1. First item."), vec![(0, 2), (2, 14)]);
        assert_eq!(review_sentence_ranges_v2("1. First item."), vec![(0, 14)]);
        assert_eq!(review_sentence_ranges_v2("IV. Next item."), vec![(0, 14)]);
    }

    #[test]
    fn paragraph_boundaries_and_segment_core_follow_spec_whitespace() {
        let s = "  甲。\n\u{3000}\n乙。  ";
        assert_eq!(segment_core(s, 0, s.chars().count()), (2, 9));
        assert_eq!(paragraph_ranges(s), vec![(0, 7), (7, 11)]);
        let segs = segment_text(s, SEGMENT_LIMIT);
        assert_eq!(segs.len(), 1);
        assert_eq!(
            (segs[0].start, segs[0].end, segs[0].core_start, segs[0].core_end),
            (0, 11, 2, 9)
        );
    }

    #[test]
    fn pair_segments_alignments_and_error_paths() {
        let src = "甲。\n\n乙。";
        let tgt = "A.\n\nB.";
        let pairs = pair_segments(src, tgt, SEGMENT_LIMIT, None).expect("valid pair");
        assert_eq!(pairs.len(), 1);
        assert_eq!(pairs[0].status, PairStatus::Ready);

        // Alignment error when text mismatches
        let bad = vec![AlignmentItem {
            source: AlignmentSpan {
                start: 0,
                end: 2,
                text: "错。".to_string(),
            },
            target: None,
        }];
        let err = pair_segments(src, tgt, SEGMENT_LIMIT, Some(&bad)).unwrap_err();
        assert!(err.0.contains("does not match"));
    }

    #[test]
    fn aho_corasick_handles_overlaps_suffixes_duplicates_and_astral_offsets() {
        let patterns = vec![
            TermPattern::new("声骸".into(), 0),
            TermPattern::new("声骸技能".into(), 1),
            TermPattern::new("技能".into(), 2),
            TermPattern::new("ab".into(), 3),
            TermPattern::new("bab".into(), 4),
            TermPattern::new("ab".into(), 5),
            TermPattern::new("".into(), 6),
        ];
        let ac = AhoCorasick::build(patterns.clone());
        let text = "🌊使用声骸技能ababa✨";
        let raw_ac = ac.find_all_matches(text);
        let raw_naive = find_all_naive(&patterns, text);
        // Raw match sets must be identical regardless of emission order
        let mut sorted_ac: Vec<(usize, usize, usize, usize)> = raw_ac
            .iter()
            .map(|m| (m.start, m.end, m.order, m.pattern_idx))
            .collect();
        let mut sorted_naive: Vec<(usize, usize, usize, usize)> = raw_naive
            .iter()
            .map(|m| (m.start, m.end, m.order, m.pattern_idx))
            .collect();
        sorted_ac.sort();
        sorted_naive.sort();
        assert_eq!(sorted_ac, sorted_naive);

        let got_ac = ac.scan_and_select(text);
        let got_naive = select_longest_non_overlapping(raw_naive, &patterns);
        assert_eq!(got_ac, got_naive);
        let triples: Vec<(usize, usize, usize)> =
            got_ac.iter().map(|m| (m.start, m.end, m.order)).collect();
        assert_eq!(triples, vec![(3, 7, 1), (8, 11, 4)]);
    }

    #[test]
    fn aho_corasick_ascii_fast_path_matches_naive() {
        let patterns = vec![
            TermPattern::new("Echo".into(), 0),
            TermPattern::new("Echo Skill".into(), 1),
            TermPattern::new("Skill".into(), 2),
            TermPattern::new("Resonator".into(), 3),
        ];
        let ac = AhoCorasick::build(patterns.clone());
        let text = "Resonator activates Echo Skill and Echo.";
        let got_ac = ac.scan_and_select(text);
        let got_naive = select_longest_non_overlapping(find_all_naive(&patterns, text), &patterns);
        assert_eq!(got_ac, got_naive);
        let triples: Vec<(usize, usize, usize)> =
            got_ac.iter().map(|m| (m.start, m.end, m.order)).collect();
        assert_eq!(triples, vec![(0, 9, 3), (20, 30, 1), (35, 39, 0)]);
    }
}
