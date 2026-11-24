//! Pure-algorithm Rust text kernel prototype for WuwaTerm.
//!
//! Research prototype only (`research/rust-text-kernel/`): zero external
//! dependencies, offline-only, differential-tested against Python `wuwaterm`.

pub mod aho_corasick;
pub mod segmentation;
pub mod text_spans;

#[cfg(test)]
mod tests {
    use crate::aho_corasick::{
        AhoCorasick, TermPattern, find_all_naive, select_longest_non_overlapping,
    };
    use crate::segmentation::{SEGMENT_LIMIT, segment_text};
    use crate::text_spans::{
        paragraph_ranges, review_sentence_ranges_v2, scalar_to_utf16, sentence_ranges_v2,
        utf16_to_scalar,
    };

    #[test]
    fn scalar_utf16_surrogate_roundtrip() {
        let s = "🌊椿A🚀";
        assert_eq!(scalar_to_utf16(s, 0), 0);
        assert_eq!(scalar_to_utf16(s, 1), 2);
        assert_eq!(scalar_to_utf16(s, 2), 3);
        assert_eq!(scalar_to_utf16(s, 3), 4);
        assert_eq!(scalar_to_utf16(s, 4), 6);
        assert_eq!(utf16_to_scalar(s, 0), 0);
        assert_eq!(utf16_to_scalar(s, 1), 0);
        assert_eq!(utf16_to_scalar(s, 2), 1);
        assert_eq!(utf16_to_scalar(s, 3), 2);
        assert_eq!(utf16_to_scalar(s, 6), 4);
    }

    #[test]
    fn unicode_digits_and_list_markers_match_spec() {
        assert_eq!(sentence_ranges_v2("3.7 和 3²．5。下一句。"), vec![(0, 11), (11, 15)]);
        assert_eq!(sentence_ranges_v2("1. First item."), vec![(0, 2), (2, 14)]);
        assert_eq!(review_sentence_ranges_v2("1. First item."), vec![(0, 14)]);
        assert_eq!(review_sentence_ranges_v2("IV. Next item."), vec![(0, 14)]);
    }

    #[test]
    fn paragraph_boundaries_follow_spec_whitespace() {
        let s = "  甲。\n\u{3000}\n乙。  ";
        assert_eq!(paragraph_ranges(s), vec![(0, 7), (7, 11)]);
        let segs = segment_text(s, SEGMENT_LIMIT);
        assert_eq!(segs.len(), 1);
        assert_eq!((segs[0].start, segs[0].end, segs[0].core_start, segs[0].core_end), (0, 11, 2, 9));
    }

    #[test]
    fn aho_corasick_handles_overlaps_suffixes_and_astral_offsets() {
        let patterns = vec![
            TermPattern::new("声骸".into(), 0),
            TermPattern::new("声骸技能".into(), 1),
            TermPattern::new("技能".into(), 2),
            TermPattern::new("ab".into(), 3),
            TermPattern::new("bab".into(), 4),
        ];
        let ac = AhoCorasick::build(patterns.clone());
        let text = "🌊使用声骸技能ababa✨";
        let got_ac = select_longest_non_overlapping(ac.find_all_matches(text), ac.patterns());
        let got_naive = select_longest_non_overlapping(find_all_naive(&patterns, text), &patterns);
        assert_eq!(got_ac, got_naive);
        let triples: Vec<(usize, usize, usize)> =
            got_ac.iter().map(|m| (m.start, m.end, m.order)).collect();
        assert_eq!(triples, vec![(3, 7, 1), (8, 11, 4)]);
    }
}
