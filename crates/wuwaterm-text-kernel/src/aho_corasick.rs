//! Pure `std`-only Aho–Corasick multi-pattern term scanner and longest
//! non-overlapping interval selector.
//!
//! Operates in one linear pass over UTF-8 bytes and projects byte spans back to
//! exact Unicode scalar `[start, end)` half-open intervals matching
//! `wuwaterm.term_matching`.

use std::cmp::Reverse;
use std::collections::VecDeque;

use crate::text_spans::build_byte_to_scalar_map;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TermPattern {
    pub surface: String,
    pub order: usize,
    pub byte_len: usize,
    pub scalar_len: usize,
}

impl TermPattern {
    pub fn new(surface: String, order: usize) -> Self {
        let byte_len = surface.len();
        let scalar_len = surface.chars().count();
        Self {
            surface,
            order,
            byte_len,
            scalar_len,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RawMatch {
    pub start: usize,
    pub end: usize,
    pub order: usize,
    pub pattern_idx: usize,
}

#[derive(Debug, Clone, Default)]
struct TrieNode {
    children: Vec<(u8, u32)>,
    outputs: Vec<usize>,
    fail: u32,
    dict_link: u32,
}

#[inline]
fn find_child(children: &[(u8, u32)], b: u8) -> Option<u32> {
    children
        .binary_search_by_key(&b, |&(ch, _)| ch)
        .ok()
        .map(|idx| children[idx].1)
}

#[derive(Debug, Clone)]
pub struct AhoCorasick {
    patterns: Vec<TermPattern>,
    nodes: Vec<TrieNode>,
    root_next: [u32; 256],
}

impl AhoCorasick {
    pub fn build(patterns: Vec<TermPattern>) -> Self {
        let mut nodes = vec![TrieNode::default()];
        for (pat_idx, pat) in patterns.iter().enumerate() {
            if pat.surface.is_empty() {
                continue;
            }
            let mut cur = 0usize;
            for &b in pat.surface.as_bytes() {
                let next = match nodes[cur].children.iter().find(|&&(ch, _)| ch == b) {
                    Some(&(_, child)) => child,
                    None => {
                        let idx = nodes.len() as u32;
                        nodes[cur].children.push((b, idx));
                        nodes.push(TrieNode::default());
                        idx
                    }
                };
                cur = next as usize;
            }
            nodes[cur].outputs.push(pat_idx);
        }

        for node in &mut nodes {
            node.children.sort_unstable_by_key(|&(b, _)| b);
        }

        let mut root_next = [0u32; 256];
        let mut queue = VecDeque::new();
        for idx in 0..nodes[0].children.len() {
            let (b, child) = nodes[0].children[idx];
            root_next[b as usize] = child;
            nodes[child as usize].fail = 0;
            queue.push_back(child);
        }

        while let Some(state) = queue.pop_front() {
            let children_len = nodes[state as usize].children.len();
            for i in 0..children_len {
                let (b, child) = nodes[state as usize].children[i];
                let mut f = nodes[state as usize].fail;
                let mut found_next = None;
                while f != 0 {
                    if let Some(nxt) = find_child(&nodes[f as usize].children, b) {
                        found_next = Some(nxt);
                        break;
                    }
                    f = nodes[f as usize].fail;
                }
                let target = found_next.unwrap_or_else(|| root_next[b as usize]);
                nodes[child as usize].fail = target;
                let dict_link = if !nodes[target as usize].outputs.is_empty() {
                    target
                } else {
                    nodes[target as usize].dict_link
                };
                nodes[child as usize].dict_link = dict_link;
                queue.push_back(child);
            }
        }

        Self {
            patterns,
            nodes,
            root_next,
        }
    }

    pub fn from_surfaces<I, S>(surfaces: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        let patterns = surfaces
            .into_iter()
            .enumerate()
            .map(|(order, surface)| TermPattern::new(surface.into(), order))
            .collect();
        Self::build(patterns)
    }

    pub fn patterns(&self) -> &[TermPattern] {
        &self.patterns
    }

    pub fn state_count(&self) -> usize {
        self.nodes.len()
    }

    /// Find every exact occurrence in `text` (including all overlaps and suffixes),
    /// returning Unicode scalar `[start, end)` spans.
    pub fn find_all_matches(&self, text: &str) -> Vec<RawMatch> {
        if text.is_empty() || self.patterns.is_empty() {
            return Vec::new();
        }
        let is_ascii = text.is_ascii();
        let byte_to_scalar = if is_ascii {
            Vec::new()
        } else {
            build_byte_to_scalar_map(text)
        };
        let mut matches = Vec::new();
        let mut state = 0u32;

        for (byte_pos, &b) in text.as_bytes().iter().enumerate() {
            while state != 0 {
                if let Some(nxt) = find_child(&self.nodes[state as usize].children, b) {
                    state = nxt;
                    break;
                }
                state = self.nodes[state as usize].fail;
            }
            if state == 0 {
                state = self.root_next[b as usize];
            }

            let end_byte = byte_pos + 1;
            let node = &self.nodes[state as usize];
            if !node.outputs.is_empty() {
                let end_scalar = if is_ascii {
                    end_byte
                } else {
                    byte_to_scalar[end_byte]
                };
                for &pat_idx in &node.outputs {
                    let pat = &self.patterns[pat_idx];
                    let start_byte = end_byte - pat.byte_len;
                    let start_scalar = if is_ascii {
                        start_byte
                    } else {
                        byte_to_scalar[start_byte]
                    };
                    matches.push(RawMatch {
                        start: start_scalar,
                        end: end_scalar,
                        order: pat.order,
                        pattern_idx: pat_idx,
                    });
                }
            }
            let mut d = node.dict_link;
            while d != 0 {
                let dnode = &self.nodes[d as usize];
                let end_scalar = if is_ascii {
                    end_byte
                } else {
                    byte_to_scalar[end_byte]
                };
                for &pat_idx in &dnode.outputs {
                    let pat = &self.patterns[pat_idx];
                    let start_byte = end_byte - pat.byte_len;
                    let start_scalar = if is_ascii {
                        start_byte
                    } else {
                        byte_to_scalar[start_byte]
                    };
                    matches.push(RawMatch {
                        start: start_scalar,
                        end: end_scalar,
                        order: pat.order,
                        pattern_idx: pat_idx,
                    });
                }
                d = dnode.dict_link;
            }
        }
        matches
    }

    /// Scan `text` for all exact occurrences and apply WuwaTerm global longest
    /// non-overlapping interval selection.
    pub fn scan_and_select(&self, text: &str) -> Vec<RawMatch> {
        select_longest_non_overlapping(self.find_all_matches(text), &self.patterns)
    }
}

/// Direct `str::find` per-pattern scan equivalent to `term_matching.py:321-337`.
pub fn find_all_naive(patterns: &[TermPattern], text: &str) -> Vec<RawMatch> {
    let byte_to_scalar = build_byte_to_scalar_map(text);
    let mut matches = Vec::new();
    for (pat_idx, pat) in patterns.iter().enumerate() {
        if pat.surface.is_empty() {
            continue;
        }
        let mut pos = 0usize;
        while let Some(rel) = text[pos..].find(&pat.surface) {
            let start_byte = pos + rel;
            let end_byte = start_byte + pat.byte_len;
            matches.push(RawMatch {
                start: byte_to_scalar[start_byte],
                end: byte_to_scalar[end_byte],
                order: pat.order,
                pattern_idx: pat_idx,
            });
            match text[start_byte..].chars().next() {
                Some(ch) => pos = start_byte + ch.len_utf8(),
                None => break,
            }
        }
    }
    matches
}

/// Global longest non-overlapping interval selection matching `term_matching.py:435-452`.
pub fn select_longest_non_overlapping(
    mut matches: Vec<RawMatch>,
    patterns: &[TermPattern],
) -> Vec<RawMatch> {
    matches.sort_by(|a, b| {
        let len_a = a.end - a.start;
        let len_b = b.end - b.start;
        Reverse(len_a)
            .cmp(&Reverse(len_b))
            .then_with(|| a.order.cmp(&b.order))
            .then_with(|| a.start.cmp(&b.start))
            .then_with(|| {
                patterns[a.pattern_idx]
                    .surface
                    .cmp(&patterns[b.pattern_idx].surface)
            })
    });

    let mut selected: Vec<RawMatch> = Vec::new();
    let mut occupied: Vec<(usize, usize)> = Vec::new();
    for m in matches {
        if occupied.iter().any(|&(os, oe)| m.start < oe && os < m.end) {
            continue;
        }
        occupied.push((m.start, m.end));
        selected.push(m);
    }
    selected.sort_by_key(|m| m.start);
    selected
}
