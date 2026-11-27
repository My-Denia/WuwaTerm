//! WebAssembly (`wasm32-unknown-unknown`) linear-memory ABI for WuwaTerm's
//! shared text kernel.
//!
//! Designed for zero-bundler consumption in Browser, Web Worker, and Node.js
//! runtimes via `js/index.mjs`. Every function accepts UTF-8 JSON input in
//! linear memory and writes its UTF-8 JSON output (or error message) to a
//! module-owned result buffer readable via `wasm_result_ptr` / `wasm_result_len`.

use std::alloc::{Layout, alloc, dealloc};
use std::cell::RefCell;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use wuwaterm_text_kernel::KERNEL_VERSION;
use wuwaterm_text_kernel::aho_corasick::{AhoCorasick, RawMatch, TermPattern};
use wuwaterm_text_kernel::segmentation::{
    AlignmentItem, AlignmentSpan, SEGMENT_LIMIT, pair_segments, segment_text,
};
use wuwaterm_text_kernel::text_spans::{
    build_scalar_to_utf16_map, build_utf16_to_scalar_map, paragraph_ranges,
    review_sentence_ranges_v2, scalar_len, scalar_to_utf16, segment_core, sentence_ranges,
    sentence_ranges_v2, utf16_len, utf16_to_scalar,
};

thread_local! {
    static RESULT_BUF: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
    static AUTOMATONS: RefCell<Vec<Option<AhoCorasick>>> = const { RefCell::new(Vec::new()) };
}

fn set_result_bytes(bytes: Vec<u8>) {
    RESULT_BUF.with(|buf| {
        *buf.borrow_mut() = bytes;
    });
}

fn set_error(msg: impl Into<String>) -> u32 {
    set_result_bytes(msg.into().into_bytes());
    1
}

fn set_ok_json<T: Serialize>(value: &T) -> u32 {
    match serde_json::to_vec(value) {
        Ok(bytes) => {
            set_result_bytes(bytes);
            0
        }
        Err(err) => set_error(format!("json serialization failed: {err}")),
    }
}

unsafe fn read_utf8_slice<'a>(ptr: *const u8, len: usize) -> Result<&'a str, String> {
    if len == 0 {
        return Ok("");
    }
    if ptr.is_null() {
        return Err("null input pointer".to_string());
    }
    let slice = unsafe { std::slice::from_raw_parts(ptr, len) };
    std::str::from_utf8(slice).map_err(|err| format!("invalid UTF-8 input: {err}"))
}

// ---------------------------------------------------------------------------
// Memory & Result Buffer Exports
// ---------------------------------------------------------------------------

#[unsafe(no_mangle)]
pub extern "C" fn wasm_alloc(len: usize) -> *mut u8 {
    let size = len.max(1);
    let Ok(layout) = Layout::from_size_align(size, 1) else {
        return std::ptr::null_mut();
    };
    unsafe { alloc(layout) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_dealloc(ptr: *mut u8, len: usize) {
    if ptr.is_null() {
        return;
    }
    let size = len.max(1);
    if let Ok(layout) = Layout::from_size_align(size, 1) {
        unsafe { dealloc(ptr, layout) };
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn wasm_result_ptr() -> *const u8 {
    RESULT_BUF.with(|buf| buf.borrow().as_ptr())
}

#[unsafe(no_mangle)]
pub extern "C" fn wasm_result_len() -> usize {
    RESULT_BUF.with(|buf| buf.borrow().len())
}

#[unsafe(no_mangle)]
pub extern "C" fn wasm_version() -> u32 {
    #[derive(Serialize)]
    struct VersionResp<'a> {
        version: &'a str,
        segment_limit: usize,
    }
    set_ok_json(&VersionResp {
        version: KERNEL_VERSION,
        segment_limit: SEGMENT_LIMIT,
    })
}

// ---------------------------------------------------------------------------
// Core JSON Handlers (testable on native targets too)
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct TextSpansReq {
    text: String,
}

#[derive(Serialize)]
struct TextSpansResp {
    core: (usize, usize),
    paragraphs: Vec<(usize, usize)>,
    sentences: Vec<(usize, usize)>,
    sentences_v2: Vec<(usize, usize)>,
    review_sentences_v2: Vec<(usize, usize)>,
}

pub fn handle_text_spans_json(input: &str) -> Result<String, String> {
    let req: TextSpansReq = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let slen = scalar_len(&req.text);
    let resp = TextSpansResp {
        core: segment_core(&req.text, 0, slen),
        paragraphs: paragraph_ranges(&req.text),
        sentences: sentence_ranges(&req.text),
        sentences_v2: sentence_ranges_v2(&req.text),
        review_sentences_v2: review_sentence_ranges_v2(&req.text),
    };
    serde_json::to_string(&resp).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
struct SegmentTextReq {
    text: String,
    #[serde(default = "default_limit")]
    limit: usize,
    #[serde(default)]
    include_utf16: bool,
}

fn default_limit() -> usize {
    SEGMENT_LIMIT
}

#[derive(Serialize)]
struct SegmentDto {
    start: usize,
    end: usize,
    core_start: usize,
    core_end: usize,
    hard_cut: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_end: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_core_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_core_end: Option<usize>,
}

pub fn handle_segment_text_json(input: &str) -> Result<String, String> {
    let req: SegmentTextReq = serde_json::from_str(input).map_err(|e| e.to_string())?;
    if req.limit == 0 {
        return Err("limit must be positive".to_string());
    }
    let segs = segment_text(&req.text, req.limit);
    let u16_map = req
        .include_utf16
        .then(|| build_scalar_to_utf16_map(&req.text));
    let out: Vec<SegmentDto> = segs
        .into_iter()
        .map(|s| {
            let (u_s, u_e, u_cs, u_ce) = match &u16_map {
                Some(m) => (
                    Some(m[s.start]),
                    Some(m[s.end]),
                    Some(m[s.core_start]),
                    Some(m[s.core_end]),
                ),
                None => (None, None, None, None),
            };
            SegmentDto {
                start: s.start,
                end: s.end,
                core_start: s.core_start,
                core_end: s.core_end,
                hard_cut: s.hard_cut,
                utf16_start: u_s,
                utf16_end: u_e,
                utf16_core_start: u_cs,
                utf16_core_end: u_ce,
            }
        })
        .collect();
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

fn parse_json_exact_int(val: &Value, name: &str) -> Result<i64, String> {
    if val.is_boolean() || !val.is_number() {
        return Err(format!("{name} offsets must be integers"));
    }
    if let Some(i) = val.as_i64() {
        return Ok(i);
    }
    if let Some(u) = val.as_u64() {
        return i64::try_from(u).map_err(|_| format!("{name} span is out of bounds"));
    }
    Err(format!("{name} offsets must be integers"))
}

fn parse_json_span(name: &str, val: &Value) -> Result<AlignmentSpan, String> {
    let Some(obj) = val.as_object() else {
        return Err(format!("{name} span is not valid"));
    };
    if obj.len() != 3
        || !obj.contains_key("start")
        || !obj.contains_key("end")
        || !obj.contains_key("text")
    {
        return Err(format!("{name} span is not valid"));
    }
    let start_i = parse_json_exact_int(&obj["start"], name)?;
    let end_i = parse_json_exact_int(&obj["end"], name)?;
    if start_i < 0 || end_i < 0 {
        return Err(format!("{name} span is out of bounds"));
    }
    let Some(text) = obj["text"].as_str() else {
        return Err(format!("{name} span text does not match the text"));
    };
    Ok(AlignmentSpan {
        start: start_i as usize,
        end: end_i as usize,
        text: text.to_string(),
    })
}

fn parse_json_alignments(val: &Value) -> Result<Option<Vec<AlignmentItem>>, String> {
    if val.is_null() {
        return Ok(None);
    }
    let Some(arr) = val.as_array() else {
        return Err("alignments must be an array".to_string());
    };
    let mut items = Vec::with_capacity(arr.len());
    for entry in arr {
        let Some(obj) = entry.as_object() else {
            return Err("alignment keys are not valid".to_string());
        };
        if obj.len() != 2 || !obj.contains_key("source") || !obj.contains_key("target") {
            return Err("alignment keys are not valid".to_string());
        }
        let source = parse_json_span("source", &obj["source"])?;
        let target = if obj["target"].is_null() {
            None
        } else {
            Some(parse_json_span("target", &obj["target"])?)
        };
        items.push(AlignmentItem { source, target });
    }
    Ok(Some(items))
}

#[derive(Serialize)]
struct RegionDto {
    start: usize,
    end: usize,
    core_start: usize,
    core_end: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_end: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_core_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_core_end: Option<usize>,
}

#[derive(Serialize)]
struct SegmentPairDto {
    index: usize,
    status: &'static str,
    hard_cut: bool,
    source: RegionDto,
    target: RegionDto,
}

pub fn handle_pair_segments_json(input: &str) -> Result<String, String> {
    let val: Value = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let Some(obj) = val.as_object() else {
        return Err("request must be a JSON object".to_string());
    };
    let source = obj
        .get("source")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing string field 'source'".to_string())?;
    let target = obj
        .get("target")
        .and_then(Value::as_str)
        .ok_or_else(|| "missing string field 'target'".to_string())?;
    let limit = match obj.get("limit") {
        None | Some(Value::Null) => SEGMENT_LIMIT,
        Some(v) => {
            let i = parse_json_exact_int(v, "limit")?;
            if i <= 0 {
                return Err("limit must be positive".to_string());
            }
            i as usize
        }
    };
    let include_utf16 = obj
        .get("include_utf16")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let alignments = match obj.get("alignments") {
        None => None,
        Some(v) => parse_json_alignments(v)?,
    };

    let pairs =
        pair_segments(source, target, limit, alignments.as_deref()).map_err(|err| err.0)?;
    let src_u16 = include_utf16.then(|| build_scalar_to_utf16_map(source));
    let tgt_u16 = include_utf16.then(|| build_scalar_to_utf16_map(target));

    let out: Vec<SegmentPairDto> = pairs
        .into_iter()
        .map(|p| {
            let map_reg = |r: wuwaterm_text_kernel::segmentation::Region,
                           m: &Option<Vec<usize>>| RegionDto {
                start: r.start,
                end: r.end,
                core_start: r.core_start,
                core_end: r.core_end,
                utf16_start: m.as_ref().map(|v| v[r.start]),
                utf16_end: m.as_ref().map(|v| v[r.end]),
                utf16_core_start: m.as_ref().map(|v| v[r.core_start]),
                utf16_core_end: m.as_ref().map(|v| v[r.core_end]),
            };
            SegmentPairDto {
                index: p.index,
                status: p.status.as_str(),
                hard_cut: p.hard_cut,
                source: map_reg(p.source, &src_u16),
                target: map_reg(p.target, &tgt_u16),
            }
        })
        .collect();
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
#[serde(untagged)]
enum PatternSpec {
    Surface(String),
    WithOrder { surface: String, order: usize },
}

fn build_automaton_from_specs(specs: Vec<PatternSpec>) -> AhoCorasick {
    let patterns: Vec<TermPattern> = specs
        .into_iter()
        .enumerate()
        .map(|(idx, spec)| match spec {
            PatternSpec::Surface(s) => TermPattern::new(s, idx),
            PatternSpec::WithOrder { surface, order } => TermPattern::new(surface, order),
        })
        .collect();
    AhoCorasick::build(patterns)
}

#[derive(Deserialize)]
struct ScanTermsReq {
    surfaces: Vec<PatternSpec>,
    text: String,
    #[serde(default = "default_true")]
    select: bool,
    #[serde(default)]
    include_utf16: bool,
}

#[derive(Deserialize)]
struct AcScanReq {
    text: String,
    #[serde(default = "default_true")]
    select: bool,
    #[serde(default)]
    include_utf16: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Serialize)]
struct MatchDto {
    start: usize,
    end: usize,
    order: usize,
    pattern_idx: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_start: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    utf16_end: Option<usize>,
}

fn format_matches(matches: Vec<RawMatch>, text: &str, include_utf16: bool) -> Vec<MatchDto> {
    let u16_map = include_utf16.then(|| build_scalar_to_utf16_map(text));
    matches
        .into_iter()
        .map(|m| MatchDto {
            start: m.start,
            end: m.end,
            order: m.order,
            pattern_idx: m.pattern_idx,
            utf16_start: u16_map.as_ref().map(|v| v[m.start]),
            utf16_end: u16_map.as_ref().map(|v| v[m.end]),
        })
        .collect()
}

pub fn handle_scan_terms_json(input: &str) -> Result<String, String> {
    let req: ScanTermsReq = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let ac = build_automaton_from_specs(req.surfaces);
    let matches = if req.select {
        ac.scan_and_select(&req.text)
    } else {
        ac.find_all_matches(&req.text)
    };
    let out = format_matches(matches, &req.text, req.include_utf16);
    serde_json::to_string(&out).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
struct ScalarToUtf16Req {
    text: String,
    scalar: usize,
}

#[derive(Serialize)]
struct ScalarToUtf16Resp {
    utf16: usize,
}

pub fn handle_scalar_to_utf16_json(input: &str) -> Result<String, String> {
    let req: ScalarToUtf16Req = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let resp = ScalarToUtf16Resp {
        utf16: scalar_to_utf16(&req.text, req.scalar),
    };
    serde_json::to_string(&resp).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
struct Utf16ToScalarReq {
    text: String,
    utf16: usize,
}

#[derive(Serialize)]
struct Utf16ToScalarResp {
    scalar: usize,
}

pub fn handle_utf16_to_scalar_json(input: &str) -> Result<String, String> {
    let req: Utf16ToScalarReq = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let resp = Utf16ToScalarResp {
        scalar: utf16_to_scalar(&req.text, req.utf16),
    };
    serde_json::to_string(&resp).map_err(|e| e.to_string())
}

#[derive(Deserialize)]
struct CoordMapsReq {
    text: String,
}

#[derive(Serialize)]
struct CoordMapsResp {
    scalar_len: usize,
    utf16_len: usize,
    scalar_to_utf16: Vec<usize>,
    utf16_to_scalar: Vec<usize>,
}

pub fn handle_coordinate_maps_json(input: &str) -> Result<String, String> {
    let req: CoordMapsReq = serde_json::from_str(input).map_err(|e| e.to_string())?;
    let resp = CoordMapsResp {
        scalar_len: scalar_len(&req.text),
        utf16_len: utf16_len(&req.text),
        scalar_to_utf16: build_scalar_to_utf16_map(&req.text),
        utf16_to_scalar: build_utf16_to_scalar_map(&req.text),
    };
    serde_json::to_string(&resp).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------------------
// Exported WASM Entrypoints
// ---------------------------------------------------------------------------

unsafe fn dispatch_json(
    ptr: *const u8,
    len: usize,
    handler: fn(&str) -> Result<String, String>,
) -> u32 {
    let input = match unsafe { read_utf8_slice(ptr, len) } {
        Ok(s) => s,
        Err(err) => return set_error(err),
    };
    match handler(input) {
        Ok(json) => {
            set_result_bytes(json.into_bytes());
            0
        }
        Err(err) => set_error(err),
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_text_spans(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_text_spans_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_segment_text(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_segment_text_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_pair_segments(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_pair_segments_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_scan_terms(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_scan_terms_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_scalar_to_utf16(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_scalar_to_utf16_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_utf16_to_scalar(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_utf16_to_scalar_json) }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_coordinate_maps(ptr: *const u8, len: usize) -> u32 {
    unsafe { dispatch_json(ptr, len, handle_coordinate_maps_json) }
}

/// Build a reusable Aho–Corasick automaton in WASM memory.
/// Returns a 1-based handle on success, or `0` on error (error text in result buffer).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_ac_create(ptr: *const u8, len: usize) -> u32 {
    let input = match unsafe { read_utf8_slice(ptr, len) } {
        Ok(s) => s,
        Err(err) => {
            set_result_bytes(err.into_bytes());
            return 0;
        }
    };
    let specs: Vec<PatternSpec> = match serde_json::from_str(input) {
        Ok(s) => s,
        Err(err) => {
            set_result_bytes(err.to_string().into_bytes());
            return 0;
        }
    };
    let ac = build_automaton_from_specs(specs);
    let stats = serde_json::json!({
        "pattern_count": ac.patterns().len(),
        "state_count": ac.state_count(),
    });
    let handle = AUTOMATONS.with(|slot| {
        let mut table = slot.borrow_mut();
        for (idx, entry) in table.iter_mut().enumerate() {
            if entry.is_none() {
                *entry = Some(ac);
                return (idx + 1) as u32;
            }
        }
        table.push(Some(ac));
        table.len() as u32
    });
    let _ = set_ok_json(&stats);
    handle
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn wasm_ac_scan_terms(handle: u32, ptr: *const u8, len: usize) -> u32 {
    let input = match unsafe { read_utf8_slice(ptr, len) } {
        Ok(s) => s,
        Err(err) => return set_error(err),
    };
    let req: AcScanReq = match serde_json::from_str(input) {
        Ok(r) => r,
        Err(err) => return set_error(err.to_string()),
    };
    let Some(idx) = handle.checked_sub(1).map(|v| v as usize) else {
        return set_error("invalid automaton handle 0");
    };
    AUTOMATONS.with(|slot| {
        let table = slot.borrow();
        let Some(Some(ac)) = table.get(idx) else {
            return set_error(format!("automaton handle {handle} not found"));
        };
        let matches = if req.select {
            ac.scan_and_select(&req.text)
        } else {
            ac.find_all_matches(&req.text)
        };
        let out = format_matches(matches, &req.text, req.include_utf16);
        set_ok_json(&out)
    })
}

#[unsafe(no_mangle)]
pub extern "C" fn wasm_ac_free(handle: u32) -> u32 {
    let Some(idx) = handle.checked_sub(1).map(|v| v as usize) else {
        return set_error("invalid automaton handle 0");
    };
    AUTOMATONS.with(|slot| {
        let mut table = slot.borrow_mut();
        if let Some(entry) = table.get_mut(idx) {
            if entry.take().is_some() {
                set_result_bytes(Vec::new());
                return 0;
            }
        }
        set_error(format!("automaton handle {handle} not found"))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_segment_and_utf16_coordinates_work() {
        let out = handle_segment_text_json(
            r#"{"text":"🌊甲。\n\n乙。","limit":1800,"include_utf16":true}"#,
        )
        .expect("segment ok");
        let parsed: Value = serde_json::from_str(&out).expect("valid json");
        let arr = parsed.as_array().expect("array");
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["start"], 0);
        assert_eq!(arr[0]["end"], 7);
        assert_eq!(arr[0]["utf16_start"], 0);
        assert_eq!(arr[0]["utf16_end"], 8); // 🌊 takes 2 UTF-16 code units
    }

    #[test]
    fn json_pair_rejects_boolean_and_non_array_alignments() {
        let err = handle_pair_segments_json(r#"{"source":"a","target":"b","alignments":"bad"}"#)
            .unwrap_err();
        assert!(err.contains("array"));

        let err2 = handle_pair_segments_json(
            r#"{"source":"a","target":"b","alignments":[{"source":{"start":false,"end":1,"text":"a"},"target":null}]}"#,
        )
        .unwrap_err();
        assert!(err2.contains("integers"));
    }

    #[test]
    fn json_scan_terms_preserves_overlaps_and_utf16() {
        let out = handle_scan_terms_json(
            r#"{"surfaces":["声骸","声骸技能","技能"],"text":"🌊声骸技能","select":true,"include_utf16":true}"#,
        )
        .expect("scan ok");
        let parsed: Value = serde_json::from_str(&out).expect("valid json");
        let arr = parsed.as_array().expect("array");
        assert_eq!(arr.len(), 1);
        assert_eq!(arr[0]["start"], 1);
        assert_eq!(arr[0]["end"], 5);
        assert_eq!(arr[0]["order"], 1);
        assert_eq!(arr[0]["utf16_start"], 2);
        assert_eq!(arr[0]["utf16_end"], 6);
    }
}
