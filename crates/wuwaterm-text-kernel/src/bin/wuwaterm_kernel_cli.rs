use std::io::{self, BufWriter, Read, Write};
use std::time::Instant;

use wuwaterm_text_kernel::aho_corasick::{
    AhoCorasick, TermPattern, find_all_naive, select_longest_non_overlapping,
};
use wuwaterm_text_kernel::segmentation::{
    AlignmentItem, AlignmentSpan, SEGMENT_LIMIT, pair_segments, segment_text,
};
use wuwaterm_text_kernel::text_spans::{
    is_py_isdigit, is_py_isspace, paragraph_ranges, review_sentence_ranges_v2, segment_core,
    sentence_ranges, sentence_ranges_v2,
};

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    fn read_u8(&mut self) -> u8 {
        let b = self.buf[self.pos];
        self.pos += 1;
        b
    }

    fn read_u32(&mut self) -> u32 {
        let bytes: [u8; 4] = self.buf[self.pos..self.pos + 4]
            .try_into()
            .expect("truncated u32 input");
        self.pos += 4;
        u32::from_le_bytes(bytes)
    }

    fn read_str(&mut self) -> String {
        let len = self.read_u32() as usize;
        let slice = &self.buf[self.pos..self.pos + len];
        self.pos += len;
        String::from_utf8(slice.to_vec()).expect("invalid UTF-8 input")
    }
}

fn write_u32(out: &mut impl Write, val: u32) -> io::Result<()> {
    out.write_all(&val.to_le_bytes())
}

fn write_u8(out: &mut impl Write, val: u8) -> io::Result<()> {
    out.write_all(&[val])
}

fn write_str(out: &mut impl Write, s: &str) -> io::Result<()> {
    let bytes = s.as_bytes();
    write_u32(out, bytes.len() as u32)?;
    out.write_all(bytes)
}

fn write_pairs(out: &mut impl Write, pairs: &[(usize, usize)]) -> io::Result<()> {
    write_u32(out, pairs.len() as u32)?;
    for &(a, b) in pairs {
        write_u32(out, a as u32)?;
        write_u32(out, b as u32)?;
    }
    Ok(())
}

fn handle_text_spans(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let count = r.read_u32();
    for _ in 0..count {
        let text = r.read_str();
        let len = text.chars().count();
        let core = segment_core(&text, 0, len);
        write_pairs(out, &[core])?;
        write_pairs(out, &paragraph_ranges(&text))?;
        write_pairs(out, &sentence_ranges(&text))?;
        write_pairs(out, &sentence_ranges_v2(&text))?;
        write_pairs(out, &review_sentence_ranges_v2(&text))?;
    }
    Ok(())
}

fn handle_segment(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let count = r.read_u32();
    for _ in 0..count {
        let limit = r.read_u32() as usize;
        let text = r.read_str();
        let segs = segment_text(&text, limit);
        write_u32(out, segs.len() as u32)?;
        for s in segs {
            write_u32(out, s.start as u32)?;
            write_u32(out, s.end as u32)?;
            write_u32(out, s.core_start as u32)?;
            write_u32(out, s.core_end as u32)?;
            write_u8(out, u8::from(s.hard_cut))?;
        }
    }
    Ok(())
}

fn handle_pair(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let count = r.read_u32();
    for _ in 0..count {
        let limit = r.read_u32() as usize;
        let source = r.read_str();
        let target = r.read_str();
        let has_align = r.read_u8() != 0;
        let align = if has_align {
            let acount = r.read_u32() as usize;
            let mut items = Vec::with_capacity(acount);
            for _ in 0..acount {
                let ss = r.read_u32() as usize;
                let se = r.read_u32() as usize;
                let st = r.read_str();
                let has_t = r.read_u8() != 0;
                let tgt = if has_t {
                    let ts = r.read_u32() as usize;
                    let te = r.read_u32() as usize;
                    let tt = r.read_str();
                    Some(AlignmentSpan {
                        start: ts,
                        end: te,
                        text: tt,
                    })
                } else {
                    None
                };
                items.push(AlignmentItem {
                    source: AlignmentSpan {
                        start: ss,
                        end: se,
                        text: st,
                    },
                    target: tgt,
                });
            }
            Some(items)
        } else {
            None
        };

        match pair_segments(&source, &target, limit, align.as_deref()) {
            Ok(pairs) => {
                write_u8(out, 1)?;
                write_u32(out, pairs.len() as u32)?;
                for p in pairs {
                    write_u32(out, p.index as u32)?;
                    write_u8(out, p.status.code())?;
                    write_u8(out, u8::from(p.hard_cut))?;
                    write_u32(out, p.source.start as u32)?;
                    write_u32(out, p.source.end as u32)?;
                    write_u32(out, p.source.core_start as u32)?;
                    write_u32(out, p.source.core_end as u32)?;
                    write_u32(out, p.target.start as u32)?;
                    write_u32(out, p.target.end as u32)?;
                    write_u32(out, p.target.core_start as u32)?;
                    write_u32(out, p.target.core_end as u32)?;
                }
            }
            Err(err) => {
                write_u8(out, 0)?;
                write_str(out, &err.0)?;
            }
        }
    }
    Ok(())
}

fn read_patterns(r: &mut Reader<'_>) -> Vec<TermPattern> {
    let pat_count = r.read_u32() as usize;
    let mut patterns = Vec::with_capacity(pat_count);
    for order in 0..pat_count {
        patterns.push(TermPattern::new(r.read_str(), order));
    }
    patterns
}

fn handle_scan_select(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let patterns = read_patterns(r);
    let ac = AhoCorasick::build(patterns);
    let doc_count = r.read_u32();
    for _ in 0..doc_count {
        let text = r.read_str();
        let selected = ac.scan_and_select(&text);
        write_u32(out, selected.len() as u32)?;
        for m in selected {
            write_u32(out, m.start as u32)?;
            write_u32(out, m.end as u32)?;
            write_u32(out, m.order as u32)?;
        }
    }
    Ok(())
}

fn measure<F: FnMut() -> usize>(warmup: usize, iters: usize, mut f: F) -> (f64, f64, usize) {
    let mut last_hits = 0usize;
    for _ in 0..warmup {
        last_hits = std::hint::black_box(f());
    }
    let mut samples = Vec::with_capacity(iters);
    for _ in 0..iters {
        let t0 = Instant::now();
        last_hits = std::hint::black_box(f());
        samples.push(t0.elapsed().as_secs_f64() * 1000.0);
    }
    samples.sort_by(f64::total_cmp);
    let median = samples[samples.len() / 2];
    let p95_idx = ((samples.len() as f64 * 0.95).round() as usize)
        .saturating_sub(1)
        .min(samples.len() - 1);
    (median, samples[p95_idx], last_hits)
}

fn handle_bench(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let patterns = read_patterns(r);
    let doc_count = r.read_u32() as usize;
    let mut docs = Vec::with_capacity(doc_count);
    for _ in 0..doc_count {
        docs.push((r.read_str(), r.read_str()));
    }
    let warmup = r.read_u32() as usize;
    let iters = r.read_u32() as usize;
    let a3_zh = r.read_str();
    let a3_en = r.read_str();

    let (build_med, build_p95, states) = measure(1, 5, || {
        let ac = AhoCorasick::build(patterns.clone());
        ac.state_count()
    });
    writeln!(
        out,
        "rust_ac_build\tall_patterns\t{build_med:.3}\t{build_p95:.3}\t{states}\tpatterns={}",
        patterns.len()
    )?;

    let ac = AhoCorasick::build(patterns.clone());
    for (name, text) in &docs {
        let (med, p95, hits) = measure(warmup, iters, || ac.scan_and_select(text).len());
        writeln!(
            out,
            "rust_ac_scan_select\t{name}\t{med:.4}\t{p95:.4}\t{hits}\tchars={}",
            text.chars().count()
        )?;

        let naive_iters = iters.min(10).max(1);
        let (nmed, np95, nhits) = measure(1, naive_iters, || {
            select_longest_non_overlapping(find_all_naive(&patterns, text), &patterns).len()
        });
        writeln!(
            out,
            "rust_naive_scan_select\t{name}\t{nmed:.4}\t{np95:.4}\t{nhits}\teval_iters={naive_iters}"
        )?;
    }

    let (all_med, all_p95, all_hits) = measure(warmup, iters, || {
        docs.iter()
            .map(|(_, t)| ac.scan_and_select(t).len())
            .sum()
    });
    writeln!(
        out,
        "rust_ac_scan_select\tall_docs\t{all_med:.4}\t{all_p95:.4}\t{all_hits}\tdocs={}",
        docs.len()
    )?;

    let (n_all_med, n_all_p95, n_all_hits) = measure(1, iters.min(5).max(1), || {
        docs.iter()
            .map(|(_, t)| {
                select_longest_non_overlapping(find_all_naive(&patterns, t), &patterns).len()
            })
            .sum()
    });
    writeln!(
        out,
        "rust_naive_scan_select\tall_docs\t{n_all_med:.4}\t{n_all_p95:.4}\t{n_all_hits}\tdocs={}",
        docs.len()
    )?;

    let (seg_med, seg_p95, seg_hits) = measure(warmup, iters, || {
        let s = segment_text(&a3_zh, SEGMENT_LIMIT);
        let p = pair_segments(&a3_zh, &a3_en, SEGMENT_LIMIT, None).expect("A3 pair");
        s.len() + p.len()
    });
    writeln!(
        out,
        "rust_segment_and_pair\tA3\t{seg_med:.4}\t{seg_p95:.4}\t{seg_hits}\tunit=segs+pairs"
    )?;
    Ok(())
}

fn handle_unicode_predicates(r: &mut Reader<'_>, out: &mut impl Write) -> io::Result<()> {
    let text = r.read_str();
    let mut flags = Vec::with_capacity(text.chars().count());
    for ch in text.chars() {
        let bit0 = u8::from(is_py_isspace(ch));
        let bit1 = u8::from(is_py_isdigit(ch)) << 1;
        flags.push(bit0 | bit1);
    }
    write_u32(out, flags.len() as u32)?;
    out.write_all(&flags)
}

fn main() -> io::Result<()> {
    let cmd = std::env::args().nth(1).expect(
        "usage: wuwaterm-kernel-cli <text-spans|segment|pair|scan-select|bench|unicode-predicates>",
    );
    let mut input = Vec::new();
    io::stdin().read_to_end(&mut input)?;
    let mut reader = Reader::new(&input);
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    match cmd.as_str() {
        "text-spans" => handle_text_spans(&mut reader, &mut out)?,
        "segment" => handle_segment(&mut reader, &mut out)?,
        "pair" => handle_pair(&mut reader, &mut out)?,
        "scan-select" => handle_scan_select(&mut reader, &mut out)?,
        "bench" => handle_bench(&mut reader, &mut out)?,
        "unicode-predicates" => handle_unicode_predicates(&mut reader, &mut out)?,
        other => panic!("unknown subcommand: {other}"),
    }
    out.flush()
}
