//! Pure Unicode-scalar sentence, core, and paragraph span primitives.
//!
//! Direct port of `wuwaterm.text_spans` (`docs/segmentation-v1.md`) using
//! zero external crates.

/// Exact 24 Unicode scalar values defined by `docs/segmentation-v1.md` and
/// `wuwaterm.text_spans.WHITESPACE`.
#[inline]
pub fn is_spec_whitespace(c: char) -> bool {
    matches!(
        c,
        '\u{0009}'..='\u{000D}'
            | '\u{0020}'
            | '\u{00A0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200A}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202F}'
            | '\u{205F}'
            | '\u{3000}'
    )
}

/// Python `str.isspace()` for separator-only sentence checks
/// (`WHITESPACE` plus `U+001C..=U+001F` and `U+0085`).
#[inline]
pub fn is_py_isspace(c: char) -> bool {
    is_spec_whitespace(c) || matches!(c, '\u{001C}'..='\u{001F}' | '\u{0085}')
}

/// Exact Python 3.12 `str.isdigit()` closed inclusive codepoint ranges.
const PY_DIGIT_RANGES: &[(u32, u32)] = &[
    (0x0030, 0x0039), (0x00B2, 0x00B3), (0x00B9, 0x00B9), (0x0660, 0x0669),
    (0x06F0, 0x06F9), (0x07C0, 0x07C9), (0x0966, 0x096F), (0x09E6, 0x09EF),
    (0x0A66, 0x0A6F), (0x0AE6, 0x0AEF), (0x0B66, 0x0B6F), (0x0BE6, 0x0BEF),
    (0x0C66, 0x0C6F), (0x0CE6, 0x0CEF), (0x0D66, 0x0D6F), (0x0DE6, 0x0DEF),
    (0x0E50, 0x0E59), (0x0ED0, 0x0ED9), (0x0F20, 0x0F29), (0x1040, 0x1049),
    (0x1090, 0x1099), (0x1369, 0x1371), (0x17E0, 0x17E9), (0x1810, 0x1819),
    (0x1946, 0x194F), (0x19D0, 0x19DA), (0x1A80, 0x1A89), (0x1A90, 0x1A99),
    (0x1B50, 0x1B59), (0x1BB0, 0x1BB9), (0x1C40, 0x1C49), (0x1C50, 0x1C59),
    (0x2070, 0x2070), (0x2074, 0x2079), (0x2080, 0x2089), (0x2460, 0x2468),
    (0x2474, 0x247C), (0x2488, 0x2490), (0x24EA, 0x24EA), (0x24F5, 0x24FD),
    (0x24FF, 0x24FF), (0x2776, 0x277E), (0x2780, 0x2788), (0x278A, 0x2792),
    (0xA620, 0xA629), (0xA8D0, 0xA8D9), (0xA900, 0xA909), (0xA9D0, 0xA9D9),
    (0xA9F0, 0xA9F9), (0xAA50, 0xAA59), (0xABF0, 0xABF9), (0xFF10, 0xFF19),
    (0x104A0, 0x104A9), (0x10A40, 0x10A43), (0x10D30, 0x10D39), (0x10E60, 0x10E68),
    (0x11052, 0x1105A), (0x11066, 0x1106F), (0x110F0, 0x110F9), (0x11136, 0x1113F),
    (0x111D0, 0x111D9), (0x112F0, 0x112F9), (0x11450, 0x11459), (0x114D0, 0x114D9),
    (0x11650, 0x11659), (0x116C0, 0x116C9), (0x11730, 0x11739), (0x118E0, 0x118E9),
    (0x11950, 0x11959), (0x11C50, 0x11C59), (0x11D50, 0x11D59), (0x11DA0, 0x11DA9),
    (0x11F50, 0x11F59), (0x16A60, 0x16A69), (0x16AC0, 0x16AC9), (0x16B50, 0x16B59),
    (0x1D7CE, 0x1D7FF), (0x1E140, 0x1E149), (0x1E2F0, 0x1E2F9), (0x1E4F0, 0x1E4F9),
    (0x1E950, 0x1E959), (0x1F100, 0x1F10A), (0x1FBF0, 0x1FBF9),
];

/// Match Python 3.12 `str.isdigit()` across all Unicode scalars.
#[inline]
pub fn is_py_isdigit(c: char) -> bool {
    if c.is_ascii_digit() {
        return true;
    }
    let cp = c as u32;
    if cp < 0x00B2 {
        return false;
    }
    PY_DIGIT_RANGES
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

#[inline]
pub fn is_sentence_end_char(c: char) -> bool {
    matches!(c, '。' | '．' | '.' | '！' | '？' | '!' | '?' | '\n')
}

#[inline]
fn separator_only(chars: &[char], start: usize, end: usize) -> bool {
    start < end
        && chars[start..end]
            .iter()
            .all(|&ch| is_sentence_end_char(ch) || is_py_isspace(ch))
}

pub fn sentence_ranges_chars(chars: &[char]) -> Vec<(usize, usize)> {
    let mut ranges = Vec::new();
    let mut start = 0usize;
    let mut index = 0usize;
    let length = chars.len();
    while index < length {
        if is_sentence_end_char(chars[index]) {
            let mut end = index + 1;
            while end < length && is_sentence_end_char(chars[end]) {
                end += 1;
            }
            if start < end && !separator_only(chars, start, end) {
                ranges.push((start, end));
            }
            start = end;
            index = end;
            continue;
        }
        index += 1;
    }
    if start < length && !separator_only(chars, start, length) {
        ranges.push((start, length));
    }
    if ranges.is_empty() && length > 0 && !separator_only(chars, 0, length) {
        ranges.push((0, length));
    }
    ranges
}

pub fn sentence_ranges(text: &str) -> Vec<(usize, usize)> {
    let chars: Vec<char> = text.chars().collect();
    sentence_ranges_chars(&chars)
}

#[inline]
fn is_sentence_end_v2(chars: &[char], index: usize) -> bool {
    let ch = chars[index];
    if !is_sentence_end_char(ch) {
        return false;
    }
    if (ch == '.' || ch == '．')
        && index > 0
        && index + 1 < chars.len()
        && is_py_isdigit(chars[index - 1])
        && is_py_isdigit(chars[index + 1])
    {
        return false;
    }
    true
}

#[inline]
fn is_list_marker_dot(chars: &[char], index: usize) -> bool {
    if index + 1 >= chars.len() || (chars[index + 1] != ' ' && chars[index + 1] != '\t') {
        return false;
    }
    let mut start = index;
    while start > 0 && index - start < 8 {
        let ch = chars[start - 1];
        if !(ch.is_ascii_digit() || matches!(ch, 'I' | 'V' | 'X' | 'L' | 'C' | 'D' | 'M')) {
            break;
        }
        start -= 1;
    }
    let label = &chars[start..index];
    if label.is_empty() {
        return false;
    }
    let all_digits = label.iter().all(|c| c.is_ascii_digit());
    let all_roman = label
        .iter()
        .all(|c| matches!(c, 'I' | 'V' | 'X' | 'L' | 'C' | 'D' | 'M'));
    if !all_digits && !all_roman {
        return false;
    }
    let mut before_end = start;
    while before_end > 0 && (chars[before_end - 1] == ' ' || chars[before_end - 1] == '\t') {
        before_end -= 1;
    }
    if before_end == 0 || chars[before_end - 1] == '\n' {
        return true;
    }
    matches!(
        chars[before_end - 1],
        '。' | '．' | '！' | '？' | '!' | '?' | '.'
    )
}

#[inline]
fn is_review_sentence_end_v2(chars: &[char], index: usize) -> bool {
    if !is_sentence_end_v2(chars, index) {
        return false;
    }
    !(matches!(chars[index], '.' | '．') && is_list_marker_dot(chars, index))
}

fn scan_sentence_ranges_v2<F>(chars: &[char], is_end: F) -> Vec<(usize, usize)>
where
    F: Fn(&[char], usize) -> bool,
{
    let mut ranges = Vec::new();
    let mut start = 0usize;
    let mut index = 0usize;
    let length = chars.len();
    while index < length {
        if is_end(chars, index) {
            let mut end = index + 1;
            while end < length && is_end(chars, end) {
                end += 1;
            }
            if start < end && !separator_only(chars, start, end) {
                ranges.push((start, end));
            }
            start = end;
            index = end;
            continue;
        }
        index += 1;
    }
    if start < length && !separator_only(chars, start, length) {
        ranges.push((start, length));
    }
    if ranges.is_empty() && length > 0 && !separator_only(chars, 0, length) {
        ranges.push((0, length));
    }
    ranges
}

pub fn sentence_ranges_v2_chars(chars: &[char]) -> Vec<(usize, usize)> {
    scan_sentence_ranges_v2(chars, is_sentence_end_v2)
}

pub fn sentence_ranges_v2(text: &str) -> Vec<(usize, usize)> {
    let chars: Vec<char> = text.chars().collect();
    sentence_ranges_v2_chars(&chars)
}

pub fn review_sentence_ranges_v2_chars(chars: &[char]) -> Vec<(usize, usize)> {
    scan_sentence_ranges_v2(chars, is_review_sentence_end_v2)
}

pub fn review_sentence_ranges_v2(text: &str) -> Vec<(usize, usize)> {
    let chars: Vec<char> = text.chars().collect();
    review_sentence_ranges_v2_chars(&chars)
}

pub fn segment_core_chars(chars: &[char], start: usize, end: usize) -> (usize, usize) {
    let mut a = start;
    while a < end && is_spec_whitespace(chars[a]) {
        a += 1;
    }
    if a == end {
        return (start, start);
    }
    let mut b = end;
    while is_spec_whitespace(chars[b - 1]) {
        b -= 1;
    }
    (a, b)
}

pub fn segment_core(text: &str, start: usize, end: usize) -> (usize, usize) {
    let chars: Vec<char> = text.chars().collect();
    segment_core_chars(&chars, start, end)
}

pub fn paragraph_ranges_chars(chars: &[char]) -> Vec<(usize, usize)> {
    let length = chars.len();
    let mut cuts = Vec::new();
    let mut i = 0usize;
    while i < length {
        if !is_spec_whitespace(chars[i]) {
            i += 1;
            continue;
        }
        let mut j = i;
        let mut newlines = 0usize;
        while j < length && is_spec_whitespace(chars[j]) {
            if chars[j] == '\n' {
                newlines += 1;
            }
            j += 1;
        }
        if newlines >= 2 && i > 0 && j < length {
            cuts.push(j);
        }
        i = j;
    }
    let (ca, cb) = segment_core_chars(chars, 0, length);
    if ca == cb {
        return Vec::new();
    }
    let mut ranges = Vec::with_capacity(cuts.len() + 1);
    let mut prev = 0usize;
    for cut in cuts {
        ranges.push((prev, cut));
        prev = cut;
    }
    ranges.push((prev, length));
    ranges
}

pub fn paragraph_ranges(text: &str) -> Vec<(usize, usize)> {
    let chars: Vec<char> = text.chars().collect();
    paragraph_ranges_chars(&chars)
}

/// Convert a Unicode scalar offset in `text` to a UTF-16 code-unit offset.
pub fn scalar_to_utf16(text: &str, scalar: usize) -> usize {
    text.chars().take(scalar).map(char::len_utf16).sum()
}

/// Convert a UTF-16 code-unit offset in `text` to the scalar offset at or before it.
pub fn utf16_to_scalar(text: &str, utf16: usize) -> usize {
    let mut u16_pos = 0usize;
    let mut scalar_pos = 0usize;
    for ch in text.chars() {
        let width = ch.len_utf16();
        if u16_pos + width > utf16 {
            break;
        }
        u16_pos += width;
        scalar_pos += 1;
    }
    scalar_pos
}

/// Map every UTF-8 byte boundary index `0..=text.len()` to its Unicode scalar offset.
pub fn build_byte_to_scalar_map(text: &str) -> Vec<usize> {
    let mut map = vec![0usize; text.len() + 1];
    let mut scalar_idx = 0usize;
    for (byte_idx, _) in text.char_indices() {
        map[byte_idx] = scalar_idx;
        scalar_idx += 1;
    }
    map[text.len()] = scalar_idx;
    map
}
