//! PyO3 native extension exposing `wuwaterm-text-kernel` to Python as `_wuwaterm_text_kernel`.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyByteArray, PyBytes, PyInt, PyMapping, PySequence, PyString};
use wuwaterm_text_kernel::KERNEL_VERSION;
use wuwaterm_text_kernel::aho_corasick::{
    AhoCorasick, RawMatch, TermPattern, select_longest_non_overlapping as core_select,
};
use wuwaterm_text_kernel::segmentation::{
    AlignmentItem, AlignmentSpan, HARD_CUT_WINDOW, SEGMENT_LIMIT, STATUS_NEEDS_MANUAL_SPLIT,
    STATUS_READY, STATUS_UNPAIRED_TARGET, STATUS_UNTRANSLATED, pair_segments as core_pair,
    segment_text as core_segment,
};
use wuwaterm_text_kernel::text_spans::{
    build_scalar_to_utf16_map, build_utf16_to_scalar_map, is_py_isdigit as core_is_py_isdigit,
    is_py_isspace as core_is_py_isspace, is_spec_whitespace as core_is_spec_whitespace,
    paragraph_ranges as core_paragraph_ranges,
    review_sentence_ranges_v2 as core_review_sentence_ranges_v2, scalar_to_utf8 as core_s2u8,
    scalar_to_utf16 as core_s2u16, segment_core as core_segment_core,
    sentence_ranges as core_sentence_ranges, sentence_ranges_v2 as core_sentence_ranges_v2,
    utf8_to_scalar as core_u82s, utf16_to_scalar as core_u162s,
};

type SegmentTuple = (usize, usize, usize, usize, bool);
type RegionTuple = (usize, usize, usize, usize);
type SegmentPairTuple = (usize, &'static str, bool, RegionTuple, RegionTuple);

fn extract_single_char(ch: &str) -> PyResult<char> {
    let mut chars = ch.chars();
    match (chars.next(), chars.next()) {
        (Some(c), None) => Ok(c),
        _ => Err(PyValueError::new_err("expected a single Unicode scalar")),
    }
}

fn mapping_keys_match(mapping: &Bound<'_, PyMapping>, expected: &[&str]) -> PyResult<bool> {
    let keys = mapping.keys()?;
    if keys.len() != expected.len() {
        return Ok(false);
    }
    for key in keys.iter() {
        let Ok(s) = key.extract::<String>() else {
            return Ok(false);
        };
        if !expected.contains(&s.as_str()) {
            return Ok(false);
        }
    }
    Ok(true)
}

fn extract_exact_int(obj: &Bound<'_, PyAny>, name: &str) -> PyResult<isize> {
    if obj.is_instance_of::<PyBool>() || !obj.is_exact_instance_of::<PyInt>() {
        return Err(PyValueError::new_err(format!(
            "{name} offsets must be integers"
        )));
    }
    obj.extract::<isize>()
        .map_err(|_| PyValueError::new_err(format!("{name} span is out of bounds")))
}

fn parse_span(name: &str, item: &Bound<'_, PyAny>) -> PyResult<AlignmentSpan> {
    let Ok(mapping) = item.downcast::<PyMapping>() else {
        return Err(PyValueError::new_err(format!("{name} span is not valid")));
    };
    if !mapping_keys_match(mapping, &["start", "end", "text"])? {
        return Err(PyValueError::new_err(format!("{name} span is not valid")));
    }
    let start_obj = mapping.get_item("start")?;
    let end_obj = mapping.get_item("end")?;
    let text_obj = mapping.get_item("text")?;

    let start_i = extract_exact_int(&start_obj, name)?;
    let end_i = extract_exact_int(&end_obj, name)?;
    if start_i < 0 || end_i < 0 {
        return Err(PyValueError::new_err(format!(
            "{name} span is out of bounds"
        )));
    }
    if !text_obj.is_instance_of::<PyString>() {
        return Err(PyValueError::new_err(format!(
            "{name} span text does not match the text"
        )));
    }
    let text: String = text_obj.extract()?;
    Ok(AlignmentSpan {
        start: start_i as usize,
        end: end_i as usize,
        text,
    })
}

fn parse_alignments(
    py: Python<'_>,
    alignments: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<Vec<AlignmentItem>>> {
    let Some(obj) = alignments else {
        return Ok(None);
    };
    if obj.is_none() {
        return Ok(None);
    }
    if obj.is_instance_of::<PyString>()
        || obj.is_instance_of::<PyBytes>()
        || obj.is_instance_of::<PyByteArray>()
    {
        return Err(PyValueError::new_err("alignments must be an array"));
    }
    let collections_abc = py.import("collections.abc")?;
    let seq_abc = collections_abc.getattr("Sequence")?;
    if !obj.is_instance(&seq_abc)? {
        return Err(PyValueError::new_err("alignments must be an array"));
    }
    let seq = obj
        .downcast::<PySequence>()
        .map_err(|_| PyValueError::new_err("alignments must be an array"))?;
    let len = seq.len()?;
    let mut items = Vec::with_capacity(len);
    for i in 0..len {
        let entry = seq.get_item(i)?;
        let Ok(mapping) = entry.downcast::<PyMapping>() else {
            return Err(PyValueError::new_err("alignment keys are not valid"));
        };
        if !mapping_keys_match(mapping, &["source", "target"])? {
            return Err(PyValueError::new_err("alignment keys are not valid"));
        }
        let src_obj = mapping.get_item("source")?;
        let tgt_obj = mapping.get_item("target")?;
        let source = parse_span("source", &src_obj)?;
        let target = if tgt_obj.is_none() {
            None
        } else {
            Some(parse_span("target", &tgt_obj)?)
        };
        items.push(AlignmentItem { source, target });
    }
    Ok(Some(items))
}

#[pyfunction]
fn is_spec_whitespace(ch: &str) -> PyResult<bool> {
    Ok(core_is_spec_whitespace(extract_single_char(ch)?))
}

#[pyfunction]
fn is_py_isspace(ch: &str) -> PyResult<bool> {
    Ok(core_is_py_isspace(extract_single_char(ch)?))
}

#[pyfunction]
fn is_py_isdigit(ch: &str) -> PyResult<bool> {
    Ok(core_is_py_isdigit(extract_single_char(ch)?))
}

#[pyfunction]
fn scalar_to_utf16(text: &str, scalar: usize) -> usize {
    core_s2u16(text, scalar)
}

#[pyfunction]
fn utf16_to_scalar(text: &str, utf16: usize) -> usize {
    core_u162s(text, utf16)
}

#[pyfunction]
fn scalar_to_utf8(text: &str, scalar: usize) -> usize {
    core_s2u8(text, scalar)
}

#[pyfunction]
fn utf8_to_scalar(text: &str, byte_offset: usize) -> usize {
    core_u82s(text, byte_offset)
}

#[pyfunction]
fn scalar_to_utf16_map(text: &str) -> Vec<usize> {
    build_scalar_to_utf16_map(text)
}

#[pyfunction]
fn utf16_to_scalar_map(text: &str) -> Vec<usize> {
    build_utf16_to_scalar_map(text)
}

#[pyfunction]
fn segment_core(text: &str, start: usize, end: usize) -> (usize, usize) {
    core_segment_core(text, start, end)
}

#[pyfunction]
fn paragraph_ranges(text: &str) -> Vec<(usize, usize)> {
    core_paragraph_ranges(text)
}

#[pyfunction]
fn sentence_ranges(text: &str) -> Vec<(usize, usize)> {
    core_sentence_ranges(text)
}

#[pyfunction]
fn sentence_ranges_v2(text: &str) -> Vec<(usize, usize)> {
    core_sentence_ranges_v2(text)
}

#[pyfunction]
fn review_sentence_ranges_v2(text: &str) -> Vec<(usize, usize)> {
    core_review_sentence_ranges_v2(text)
}

#[pyfunction]
#[pyo3(signature = (text, limit = SEGMENT_LIMIT))]
fn segment_text(py: Python<'_>, text: &str, limit: isize) -> PyResult<Vec<SegmentTuple>> {
    if limit <= 0 {
        return Err(PyValueError::new_err("limit must be positive"));
    }
    let lim = limit as usize;
    let segs = py.allow_threads(|| core_segment(text, lim));
    Ok(segs
        .into_iter()
        .map(|s| (s.start, s.end, s.core_start, s.core_end, s.hard_cut))
        .collect())
}

#[pyfunction]
#[pyo3(signature = (source, target, limit = SEGMENT_LIMIT, alignments = None))]
fn pair_segments(
    py: Python<'_>,
    source: &str,
    target: &str,
    limit: isize,
    alignments: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<SegmentPairTuple>> {
    if limit <= 0 {
        return Err(PyValueError::new_err("limit must be positive"));
    }
    let parsed_alignments = parse_alignments(py, alignments)?;
    let lim = limit as usize;
    let pairs = py
        .allow_threads(|| core_pair(source, target, lim, parsed_alignments.as_deref()))
        .map_err(|err| PyValueError::new_err(err.0))?;
    Ok(pairs
        .into_iter()
        .map(|p| {
            (
                p.index,
                p.status.as_str(),
                p.hard_cut,
                (
                    p.source.start,
                    p.source.end,
                    p.source.core_start,
                    p.source.core_end,
                ),
                (
                    p.target.start,
                    p.target.end,
                    p.target.core_start,
                    p.target.core_end,
                ),
            )
        })
        .collect())
}

#[pyfunction]
fn select_longest_non_overlapping(
    py: Python<'_>,
    matches: Vec<(usize, usize, usize, usize)>,
    surfaces: Vec<String>,
) -> PyResult<Vec<(usize, usize, usize)>> {
    for &(_, _, _, pat_idx) in &matches {
        if pat_idx >= surfaces.len() {
            return Err(PyValueError::new_err(format!(
                "pattern_idx {pat_idx} out of bounds for {} surfaces",
                surfaces.len()
            )));
        }
    }
    let selected = py.allow_threads(|| {
        let patterns: Vec<TermPattern> = surfaces
            .into_iter()
            .enumerate()
            .map(|(order, surface)| TermPattern::new(surface, order))
            .collect();
        let raw: Vec<RawMatch> = matches
            .into_iter()
            .map(|(start, end, order, pattern_idx)| RawMatch {
                start,
                end,
                order,
                pattern_idx,
            })
            .collect();
        core_select(raw, &patterns)
    });
    Ok(selected
        .into_iter()
        .map(|m| (m.start, m.end, m.order))
        .collect())
}

#[pyclass(frozen)]
struct TermAutomaton {
    inner: AhoCorasick,
}

#[pymethods]
impl TermAutomaton {
    #[new]
    fn new(py: Python<'_>, surfaces: Vec<String>) -> Self {
        let inner = py.allow_threads(|| AhoCorasick::from_surfaces(surfaces));
        Self { inner }
    }

    #[staticmethod]
    fn from_patterns(py: Python<'_>, patterns: Vec<(String, usize)>) -> Self {
        let inner = py.allow_threads(|| {
            let built = patterns
                .into_iter()
                .map(|(surface, order)| TermPattern::new(surface, order))
                .collect();
            AhoCorasick::build(built)
        });
        Self { inner }
    }

    #[getter]
    fn pattern_count(&self) -> usize {
        self.inner.patterns().len()
    }

    #[getter]
    fn state_count(&self) -> usize {
        self.inner.state_count()
    }

    fn find_all(&self, py: Python<'_>, text: &str) -> Vec<(usize, usize, usize)> {
        let raw = py.allow_threads(|| self.inner.find_all_matches(text));
        raw.into_iter()
            .map(|m| (m.start, m.end, m.order))
            .collect()
    }

    fn find_all_with_pattern_idx(
        &self,
        py: Python<'_>,
        text: &str,
    ) -> Vec<(usize, usize, usize, usize)> {
        let raw = py.allow_threads(|| self.inner.find_all_matches(text));
        raw.into_iter()
            .map(|m| (m.start, m.end, m.order, m.pattern_idx))
            .collect()
    }

    fn scan_and_select(&self, py: Python<'_>, text: &str) -> Vec<(usize, usize, usize)> {
        let selected = py.allow_threads(|| self.inner.scan_and_select(text));
        selected
            .into_iter()
            .map(|m| (m.start, m.end, m.order))
            .collect()
    }
}

#[pymodule]
fn _wuwaterm_text_kernel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("KERNEL_VERSION", KERNEL_VERSION)?;
    m.add("SEGMENT_LIMIT", SEGMENT_LIMIT)?;
    m.add("HARD_CUT_WINDOW", HARD_CUT_WINDOW)?;
    m.add("STATUS_READY", STATUS_READY)?;
    m.add("STATUS_NEEDS_MANUAL_SPLIT", STATUS_NEEDS_MANUAL_SPLIT)?;
    m.add("STATUS_UNTRANSLATED", STATUS_UNTRANSLATED)?;
    m.add("STATUS_UNPAIRED_TARGET", STATUS_UNPAIRED_TARGET)?;

    m.add_function(wrap_pyfunction!(is_spec_whitespace, m)?)?;
    m.add_function(wrap_pyfunction!(is_py_isspace, m)?)?;
    m.add_function(wrap_pyfunction!(is_py_isdigit, m)?)?;
    m.add_function(wrap_pyfunction!(scalar_to_utf16, m)?)?;
    m.add_function(wrap_pyfunction!(utf16_to_scalar, m)?)?;
    m.add_function(wrap_pyfunction!(scalar_to_utf8, m)?)?;
    m.add_function(wrap_pyfunction!(utf8_to_scalar, m)?)?;
    m.add_function(wrap_pyfunction!(scalar_to_utf16_map, m)?)?;
    m.add_function(wrap_pyfunction!(utf16_to_scalar_map, m)?)?;
    m.add_function(wrap_pyfunction!(segment_core, m)?)?;
    m.add_function(wrap_pyfunction!(paragraph_ranges, m)?)?;
    m.add_function(wrap_pyfunction!(sentence_ranges, m)?)?;
    m.add_function(wrap_pyfunction!(sentence_ranges_v2, m)?)?;
    m.add_function(wrap_pyfunction!(review_sentence_ranges_v2, m)?)?;
    m.add_function(wrap_pyfunction!(segment_text, m)?)?;
    m.add_function(wrap_pyfunction!(pair_segments, m)?)?;
    m.add_function(wrap_pyfunction!(select_longest_non_overlapping, m)?)?;
    m.add_class::<TermAutomaton>()?;
    Ok(())
}
