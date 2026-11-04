"""Portable manuscript files must be safe to replace and readable by Web."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from wuwaterm_client import manuscript


def sample() -> dict:
    source, target = "🌊 glass harbor", "抵达玻璃港。"
    start = source.index("glass harbor")
    end = start + len("glass harbor")
    target_start = target.index("玻璃港")
    source_span = {"start": start, "end": end, "text": "glass harbor"}
    target_span = {"start": target_start, "end": target_start + 3, "text": "玻璃港"}
    alignments = [{"source": {"start": 0, "end": len(source), "text": source},
                   "target": {"start": 0, "end": len(target), "text": target}}]
    revision = "a" * 64
    dictionary = {"schema_version": "1", "source_commit": "b" * 40, "term_count": 1, "revision": revision}
    finding = {"id": f"{start}:{end}:glass harbor", "rule_id": "term", "verdict": "needs_review",
               "source_span": source_span, "target_span": target_span, "candidates": [],
               "candidates_truncated": False}
    report = {"request_id": "example", "source_revision": hashlib.sha256(source.encode()).hexdigest(),
              "target_revision": hashlib.sha256(target.encode()).hexdigest(), "rule_version": "review-v2",
              "dictionary": dictionary, "coverage": {"evaluated": 1, "not_evaluated": 0, "rules": ["term"]},
              "findings": [finding], "truncated": False, "matcher_revision": revision}
    choice = {"source": source, "direction": "en", "source_span": source_span,
              "scope": json.dumps(alignments[0], ensure_ascii=False, separators=(",", ":")),
              "choice": "not_a_term", "candidate": None,
              "basis": {"rule_version": "review-v2", "dictionary": dictionary, "matcher_revision": revision}}
    history = {"source": source, "target": target, "direction": "en", "alignments": alignments, "report": report,
               "resolutions": [{"mention_id": finding["id"], "choice": "not_a_term"}]}
    return {"format": manuscript.WORKFILE_FORMAT, "source": source, "target": target, "direction": "en",
            "alignments": alignments, "choices": [choice], "history": [history]}


def test_real_file_roundtrip_preserves_user_data_and_history(tmp_path: Path) -> None:
    original = sample()
    path = tmp_path / "draft.json"
    manuscript.save_workfile(path, original)
    loaded = manuscript.load_workfile(path)
    assert loaded == original
    assert not any("trusted" in snapshot or "active" in snapshot for snapshot in loaded["history"])
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


def test_serializing_runtime_fields_never_exports_trust() -> None:
    original = sample()
    original["trusted"] = True
    original["active"] = True
    exported = json.loads(manuscript.serialize_workfile(original))
    assert set(exported) == {"format", "source", "target", "direction", "alignments", "choices", "history"}
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.parse_workfile(json.dumps(original))
    assert error.value.code == "workfile_unsupported_format"


def test_a_long_draft_is_saved_without_applying_review_limit(tmp_path: Path) -> None:
    draft = manuscript.empty_workfile()
    draft["source"] = "🌊" * 3000
    draft["target"] = "字" * 3000
    path = tmp_path / "long.json"
    manuscript.save_workfile(path, draft)
    assert manuscript.load_workfile(path) == draft


def test_failed_replace_preserves_original_and_removes_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "draft.json"
    path.write_text("original", encoding="utf-8")

    def fail_replace(source: str, destination: Path) -> None:
        raise PermissionError("simulated replace failure")

    monkeypatch.setattr(manuscript.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        manuscript.save_workfile(path, sample())
    assert path.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == [path.name]


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(format="wrong"),
    lambda x: x.update(source="\ud800"),
    lambda x: x.update(direction="fr"),
    lambda x: x.update(choices=x["choices"] * 33),
    lambda x: x.update(history=x["history"] * 3),
    lambda x: x.update(alignments=[{"source": {"start": 0, "end": 1, "text": "x"}, "target": None}]),
    lambda x: x["history"][0]["report"]["findings"][0].update(verdict={}),
])
def test_invalid_input_is_rejected_without_replacing_current_draft(mutate) -> None:
    candidate = sample()
    mutate(candidate)
    raw = json.dumps(candidate, ensure_ascii=True)
    with pytest.raises(manuscript.WorkfileError):
        manuscript.parse_workfile(raw)


def test_duplicate_recovered_choices_are_rejected() -> None:
    draft = sample()
    duplicate = json.loads(json.dumps(draft["choices"][0]))
    duplicate["source"] = "new line\n" + draft["source"]
    duplicate["source_span"]["start"] += len("new line\n")
    duplicate["source_span"]["end"] += len("new line\n")
    draft["choices"].append(duplicate)
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.parse_workfile(json.dumps(draft))
    assert error.value.code == "workfile_duplicate_choices"


def test_edit_can_make_distinct_historical_choices_collide_and_user_can_resolve() -> None:
    draft = sample()
    old_source = draft["source"]
    other = json.loads(json.dumps(draft["choices"][0]))
    other["source"] = "another block\n" + old_source
    other["source_span"]["start"] += len("another block\n")
    other["source_span"]["end"] += len("another block\n")
    draft["choices"].append(other)
    # Both old source blocks are ambiguous in this valid original file.
    draft["source"] = old_source + "\n" + old_source
    original = manuscript.parse_workfile(manuscript.serialize_workfile(draft))
    assert manuscript.choice_conflict_groups(original) == []

    # Removing one duplicate source block makes both old choices recover to the
    # same current mention even though their original keys are still distinct.
    original["source"] = old_source
    before = json.loads(json.dumps(original))
    assert manuscript.choice_conflict_groups(original) == [[0, 1]]
    assert original == before
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.serialize_workfile(original)
    assert error.value.code == "workfile_duplicate_choices"

    removed = original["choices"].pop(1)
    saved = manuscript.parse_workfile(manuscript.serialize_workfile(original))
    assert removed == before["choices"][1]
    assert saved["choices"] == [before["choices"][0]]
    assert saved["history"] == before["history"]
    assert saved["alignments"] == before["alignments"]


def test_conflict_group_indices_and_groups_are_stably_ordered() -> None:
    source = "alpha beta"
    first = {"source": source, "source_span": {"start": 0, "end": 5, "text": "alpha"}}
    second = {"source": source, "source_span": {"start": 6, "end": 10, "text": "beta"}}
    absent = {"source": "gamma", "source_span": {"start": 0, "end": 5, "text": "gamma"}}
    payload = {"source": source, "choices": [first, second, first, absent, second, first]}
    assert manuscript.choice_conflict_groups(payload) == [[0, 2, 5], [1, 4]]


def test_file_and_json_limits_are_checked_before_reading_the_file(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (manuscript.MAX_WORKFILE_BYTES + 1))
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.load_workfile(path)
    assert error.value.code == "workfile_too_large"
    for raw in ('{"source": NaN}', '{"source": Infinity}', "\ufeff{}"):
        with pytest.raises(manuscript.WorkfileError) as error:
            manuscript.parse_workfile(raw)
        assert error.value.code == "workfile_not_json"


def test_invalid_huge_numbers_do_not_escape_as_python_exceptions() -> None:
    raw = json.dumps(sample()).replace('"term_count": 1', '"term_count": 1e999')
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.parse_workfile(raw)
    assert error.value.code == "workfile_invalid_content"


@pytest.mark.skipif(not os.environ.get("WUWATERM_WEB_ROOT") or not shutil.which("node"),
                    reason="requires the external Web checkout and Node")
def test_actual_web_and_client_read_each_others_files() -> None:
    """Exercise the real Web serializer and parser in both directions."""
    web_root = Path(os.environ["WUWATERM_WEB_ROOT"])
    program = '''
      import { readFileSync } from 'node:fs';
      import { parseWorkfile, serializeWorkfile } from './lib/manuscript.js';
      const input = JSON.parse(readFileSync(0, 'utf8'));
      const parsed = parseWorkfile(input.pythonFile);
      process.stdout.write(JSON.stringify({parsed, webFile: serializeWorkfile(input.payload),
        webConflictFile: serializeWorkfile(input.conflictingPayload)}));
    '''
    payload = sample()
    conflicting_payload = sample()
    other = json.loads(json.dumps(conflicting_payload["choices"][0]))
    other["source"] = "another block\n" + payload["source"]
    other["source_span"]["start"] += len("another block\n")
    other["source_span"]["end"] += len("another block\n")
    conflicting_payload["choices"].append(other)
    conflicting_payload["source"] = payload["source"] + "\n" + payload["source"]
    result = subprocess.run(["node", "--input-type=module", "-e", program], cwd=web_root,
                            input=json.dumps({"pythonFile": manuscript.serialize_workfile(payload), "payload": payload,
                                              "conflictingPayload": conflicting_payload}),
                            text=True, capture_output=True, check=True, timeout=30)
    response = json.loads(result.stdout)
    assert response["parsed"] == payload
    assert manuscript.parse_workfile(response["webFile"]) == payload
    web_draft = manuscript.parse_workfile(response["webConflictFile"])
    assert manuscript.choice_conflict_groups(web_draft) == []
    web_draft["source"] = payload["source"]
    assert manuscript.choice_conflict_groups(web_draft) == [[0, 1]]
    with pytest.raises(manuscript.WorkfileError) as error:
        manuscript.serialize_workfile(web_draft)
    assert error.value.code == "workfile_duplicate_choices"
    web_draft["choices"].pop(1)
    recovered = manuscript.parse_workfile(manuscript.serialize_workfile(web_draft))
    assert recovered["choices"] == [payload["choices"][0]]
    assert recovered["history"] == payload["history"]
