"""scripts/eval_usability.py: frozen scorer and leakage scan (ADR 0020).

Synthetic strings only, invented for these tests; no corpus text, no network.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "eval_usability.py"
REASON = "zz-reason-zz"
WINDOW = 12


@pytest.fixture(scope="module")
def ev():
    spec = importlib.util.spec_from_file_location("eval_usability", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_usability"] = module  # dataclasses resolve their module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("eval_usability", None)


def call(ev, argv):
    try:
        return ev.main(argv)
    except SystemExit as exc:  # argparse usage errors
        return exc.code


def test_script_imports_only_the_standard_library():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)
    assert not {"wuwaterm", "wuwaterm_api"} & roots


# ---------------------------------------------------------------- unit helpers


@pytest.mark.parametrize(
    ("k", "n", "lo", "hi"),
    [(80, 100, 0.7112, 0.8666), (0, 10, 0.0, 0.2775), (10, 10, 0.7225, 1.0)],
)
def test_wilson_interval(ev, k, n, lo, hi):
    got = ev.wilson(k, n)
    assert got == pytest.approx((lo, hi), abs=5e-4)
    if k == 0:
        assert got[0] == 0.0
    if k == n:
        assert got[1] == 1.0


def test_wilson_without_observations_is_none(ev):
    assert ev.wilson(0, 0) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Version 3.7 is out. Next one!", ["Version 3.7 is out.", " Next one!"]),
        ("A。B！？C", ["A。", "B！？", "C"]),
        ("x.\n\ny", ["x.\n\n", "y"]),
        ("Hello. ... !", ["Hello. ... !"]),
        ("3.7.1 done", ["3.7.1 done"]),
        ("版本３．７来了。", ["版本３．７来了。"]),
        ("v3. 7", ["v3.", " 7"]),
        ("1..", ["1.."]),
        ("。。A", ["A"]),
    ],
)
def test_split_sentences(ev, text, expected):
    assert ev.split_sentences(text) == expected


def test_normalize_sentence_keeps_case_and_drops_space_and_punctuation(ev):
    assert ev.normalize_sentence("Ｔhe Gate, opens… (now)!\n") == "TheGateopensnow"
    assert ev.normalize_sentence("（）。") == ""


@pytest.mark.parametrize(
    ("reference", "translation", "expected"),
    [
        ("Hello。（）。World。", "Hello。World。", (2, 2)),
        ("Yes. Yes.", "Yes.", (2, 1)),
        ("Alpha. Beta.", "alpha. beta.", (2, 0)),
        ("v 3.7 ok. Next.", "v3.7 ok! Next", (2, 2)),
    ],
)
def test_literal_match(ev, reference, translation, expected):
    assert ev.literal_match(reference, translation) == expected


def test_normalize_rendering(ev):
    forms = ["Sigil", "sigil.", "  “Sigil”  ", "ＳＩＧＩＬ"]
    assert len({ev.normalize_rendering(form) for form in forms}) == 1
    assert ev.normalize_rendering("Sigil") != ev.normalize_rendering("Sigils")


# ------------------------------------------------------------ synthetic world


def para(pid, direction, genre, source, reference=None, exclusion=None):
    return {
        "id": pid,
        "doc": pid.split("#")[0],
        "direction": direction,
        "genre": genre,
        "scored": exclusion is None,
        "exclusion": exclusion,
        "source": source,
        "reference": reference,
    }


def occ(occ_id, paragraph, source, surface, name_id):
    start = source.index(surface)
    return {
        "occ": occ_id,
        "paragraph": paragraph,
        "start": start,
        "end": start + len(surface),
        "surface": surface,
        "name_id": name_id,
        "official_forms": ["Official"],
    }


def nm(occ_id, label="official", rendering="Official", misleading=False):
    if label in ("omitted", "undecidable"):
        rendering = None
    return {"occ": occ_id, "label": label, "rendering": rendering, "misleading": misleading}


def err(kind, count=1):
    return {"type": kind, "count": count}


class W:
    """Paragraphs, name occurrences and one item per scored paragraph and arm."""

    def __init__(self, paras, occs=(), arms=("x",)):
        self.paras = list(paras)
        self.occs = list(occs)
        self.items = [
            {
                "item": f"{arm}:{p['id']}",
                "paragraph": p["id"],
                "arm": arm,
                "translation": f"Rendered text for {arm}:{p['id']}.",
                "run_failed": False,
            }
            for arm in arms
            for p in self.paras
            if p["scored"]
        ]

    def item(self, item_id):
        return next(i for i in self.items if i["item"] == item_id)

    def fail(self, item_id):
        self.item(item_id).update(translation=None, run_failed=True)

    def files(self):
        return {
            "sample": {"schema": "usability-sample-v1", "set": "H1", "paragraphs": self.paras},
            "names": {"schema": "usability-names-v1", "occurrences": self.occs},
            "items": {"schema": "usability-items-v1", "items": self.items},
        }

    def texts(self):
        out = [REASON]
        out += [p["source"] for p in self.paras]
        out += [p["reference"] for p in self.paras if p["reference"]]
        out += [i["translation"] for i in self.items if i["translation"]]
        out += [o["surface"] for o in self.occs]
        return out

    def rec(self, item_id, grade="A", naturalness="natural", critical=(), names=None, reason=None):
        pid = self.item(item_id)["paragraph"]
        if names is None:
            names = [nm(o["occ"]) for o in self.occs if o["paragraph"] == pid]
        out = {
            "item": item_id,
            "grade": grade,
            "critical_errors": list(critical),
            "naturalness": naturalness,
            "names": names,
        }
        if reason is not None:
            out["reason"] = reason
        return out

    def jf(self, pass_, over=None, skip=(), only=None):
        over = over or {}
        records = [
            self.rec(i["item"], **over.get(i["item"], {}))
            for i in self.items
            if not i["run_failed"]
            and i["item"] not in skip
            and (only is None or i["item"] in only)
        ]
        return {"schema": "usability-judgment-v1", "pass": pass_, "records": records}

    def agree(self, over=None):
        return [self.jf("j1", over), self.jf("j2", over)]

    def adj(self, over):
        return self.jf("adj", over, only=set(over))


def base_world():
    paras = [
        para("docA#p0", "zh-en", "announcement", "雾港的灯塔亮了。", "The lighthouse of the misty harbor is lit."),
        para("docA#p1", "zh-en", "guide", "旅人收起了地图。"),
        para("docA#p2", "zh-en", "mixed", "那天没有风。"),
        para("docA#p3", "zh-en", "mixed", "他走了很久很久。"),
        para("docA#p4", "zh-en", "guide", "桥下的水很冷。"),
        para("docA#p5", "zh-en", "mixed", "123 456", exclusion="no_letter"),
        para("docB#p0", "en-zh", "story", "The lantern of Brinemoor is lit tonight.", "今晚布林摩尔的灯亮了。"),
        para("docB#p1", "en-zh", "story", "Nobody opened the gate."),
        para("docB#p2", "en-zh", "announcement", "Maintenance ends at noon."),
        para("docB#p3", "en-zh", "mixed", "Quiet again.", exclusion="duplicate"),
    ]
    occs = [occ("o1", "docB#p0", paras[6]["source"], "Brinemoor", "n1")]
    world = W(paras, occs)
    world.fail("x:docA#p4")
    return world


Result = namedtuple("Result", "code report out err paths")


@pytest.fixture()
def score(ev, tmp_path, capsys):
    counter = iter(range(10_000))

    def write(name, obj):
        path = tmp_path / f"{next(counter)}-{name}"
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run(world, judgments, *, samples=None, names=None, items=None, to_stdout=False):
        files = world.files()
        given = {"sample": samples, "names": names, "items": items}
        argv = ["score"]
        for kind in ("sample", "names", "items"):
            for obj in [files[kind]] if given[kind] is None else given[kind]:
                argv += [f"--{kind}", write(f"{kind}.json", obj)]
        paths = [write(f"judgments-{j['pass'] if isinstance(j, dict) else 'raw'}.json", j) for j in judgments]
        argv += ["--judgments", *paths]
        out_path = tmp_path / "report.json"
        if not to_stdout:
            argv += ["--out", str(out_path)]
        code = call(ev, argv)
        captured = capsys.readouterr()
        text = captured.out if to_stdout else (out_path.read_text(encoding="utf-8") if out_path.exists() else "")
        return Result(code, json.loads(text) if text.strip() else None, captured.out, captured.err, paths)

    return run


def cell(res, arm="x", direction="zh-en"):
    return res.report["arms"][arm][direction]


# --------------------------------------------------------------- usable rate


def score_wilson(k, n):
    # independent restatement of the Wilson score interval, z = 1.959963984540054
    z = 1.959963984540054
    p = k / n
    centre = p + z * z / (2 * n)
    half = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    denom = 1 + z * z / n
    return ((centre - half) / denom, (centre + half) / denom)


def test_usable_rate_counts_undecidable_failed_and_c_as_not_usable(score):
    world = base_world()
    over = {
        "x:docA#p1": {"grade": "B"},
        "x:docA#p2": {"grade": "C", "reason": REASON},
        "x:docA#p3": {"grade": "undecidable", "reason": REASON},
    }
    res = score(world, world.agree(over))
    assert res.code == 0 and res.out == ""
    assert res.report["schema"] == "usability-report-v1"
    assert res.report["state"] == "complete" and res.report["pending_items"] == 0
    zh = cell(res)
    assert zh["usable"]["n"] == 5 and zh["usable"]["usable"] == 2
    assert zh["usable"]["rate"] == pytest.approx(0.4)
    assert zh["usable"]["wilson95"] == pytest.approx(score_wilson(2, 5))
    assert zh["usable"]["grades"] == {"A": 1, "B": 1, "C": 1, "undecidable": 1}
    assert zh["usable"]["run_failed"] == 1 and zh["usable"]["pending"] == 0
    assert zh["excluded_from_metrics_2_5"] == 1
    assert zh["undecidable_items"] == ["x:docA#p3"]
    assert zh["genres"]["announcement"] == {"n": 1, "usable": 1, "rate": 1.0}
    assert zh["genres"]["guide"] == {"n": 2, "usable": 1, "rate": 0.5}
    assert zh["genres"]["mixed"] == {"n": 2, "usable": 0, "rate": 0.0}
    assert zh["genres"]["story"] == {"n": 0, "usable": 0, "rate": None}
    en = cell(res, direction="en-zh")
    assert en["usable"]["n"] == 3 and en["usable"]["rate"] == 1.0
    for direction in ("zh-en", "en-zh"):
        u = cell(res, direction=direction)["usable"]
        assert sum(u["grades"].values()) + u["run_failed"] + u["pending"] == u["n"]


def test_direction_without_scored_paragraphs_has_null_rate(score):
    world = W([para("d#p0", "zh-en", "guide", "雨停了。")])
    res = score(world, world.agree())
    assert res.code == 0
    empty = cell(res, direction="en-zh")
    assert empty["usable"]["n"] == 0 and empty["usable"]["rate"] is None
    assert empty["usable"]["wilson95"] is None


def test_two_arms_are_reported_separately(score):
    world = W(base_world().paras, arms=("x", "y"))
    over = {"y:docA#p0": {"grade": "C", "reason": REASON}}
    res = score(world, world.agree(over))
    assert res.code == 0
    assert cell(res, "x")["usable"]["usable"] == 5 and cell(res, "y")["usable"]["usable"] == 4
    assert sorted(res.report["arms"]) == ["x", "y"]


def test_multiple_files_per_option_are_merged(score):
    world = base_world()
    files = world.files()
    half = len(world.paras) // 2
    samples = [
        {**files["sample"], "paragraphs": world.paras[:half]},
        {**files["sample"], "paragraphs": world.paras[half:]},
    ]
    items = [
        {**files["items"], "items": [i for i in world.items if i["paragraph"].startswith("docA")]},
        {**files["items"], "items": [i for i in world.items if i["paragraph"].startswith("docB")]},
    ]
    j1 = world.jf("j1")
    j1a = {**j1, "records": [r for r in j1["records"] if "docA" in r["item"]]}
    j1b = {**j1, "records": [r for r in j1["records"] if "docB" in r["item"]]}
    res = score(world, [j1a, j1b, world.jf("j2")], samples=samples, items=items)
    assert res.code == 0 and cell(res)["usable"]["n"] == 5


# ------------------------------------------------------- agreement and adjudication


def test_report_goes_to_stdout_without_out(score):
    world = base_world()
    res = score(world, world.agree(), to_stdout=True)
    assert res.code == 0 and res.report["state"] == "complete"


def test_disagreement_without_adjudication_is_pending(score):
    world = base_world()
    j1 = world.jf("j1")
    j2 = world.jf("j2", {"x:docA#p0": {"grade": "B"}})
    for to_stdout in (False, True):
        res = score(world, [j1, j2], to_stdout=to_stdout)
        assert res.code == 3
        assert res.report["state"] == "pending" and res.report["pending_items"] == 1
        zh = cell(res)
        assert zh["usable"]["pending"] == 1 and zh["usable"]["n"] == 5
        assert zh["usable"]["grades"]["A"] == 3 and zh["usable"]["usable"] == 3
        assert zh["usable"]["rate"] is None and zh["usable"]["wilson95"] is None
        assert zh["critical"]["share"] is None and zh["names"]["accuracy"] is None
        assert cell(res, direction="en-zh")["usable"]["rate"] == 1.0


def test_missing_second_judgment_is_pending(score):
    world = base_world()
    res = score(world, [world.jf("j1"), world.jf("j2", skip={"x:docB#p1"})])
    assert res.code == 3 and res.report["pending_items"] == 1
    assert cell(res, direction="en-zh")["usable"]["pending"] == 1


def test_adjudication_resolves_a_disagreement(score):
    world = base_world()
    j1 = world.jf("j1")
    j2 = world.jf("j2", {"x:docA#p0": {"grade": "B"}})
    adj = world.adj({"x:docA#p0": {"grade": "C", "reason": REASON}})
    res = score(world, [j1, j2, adj])
    assert res.code == 0 and res.report["state"] == "complete"
    assert cell(res)["usable"]["grades"]["C"] == 1 and cell(res)["usable"]["usable"] == 3


def test_adjudication_where_judges_agree_is_rejected(score):
    world = base_world()
    adj = world.adj({"x:docA#p0": {"reason": REASON}})
    res = score(world, [*world.agree(), adj])
    assert res.code == 2 and res.report is None


def test_adjudication_without_both_judgments_is_rejected(score):
    world = base_world()
    adj = world.adj({"x:docA#p0": {"reason": REASON}})
    res = score(world, [world.jf("j1"), world.jf("j2", skip={"x:docA#p0"}), adj])
    assert res.code == 2


def test_adjudication_record_needs_a_reason(score):
    world = base_world()
    j2 = world.jf("j2", {"x:docA#p0": {"grade": "B"}})
    res = score(world, [world.jf("j1"), j2, world.adj({"x:docA#p0": {"grade": "B"}})])
    assert res.code == 2


SIGIL = [nm("o1", "official", "Sigil")]


@pytest.mark.parametrize(
    ("first", "second", "agree"),
    [
        ({}, {}, True),
        (
            {"grade": "C", "critical": [err("omission"), err("omission")], "reason": REASON},
            {"grade": "C", "critical": [err("omission", 2)], "reason": REASON},
            True,
        ),
        ({"grade": "C", "reason": "first"}, {"grade": "C", "reason": "second"}, True),
        ({"names": SIGIL}, {"names": [nm("o1", "official", "sigil.")]}, True),
        ({"names": SIGIL}, {"names": [nm("o1", "official", " ＳＩＧＩＬ ")]}, True),
        ({}, {"grade": "B"}, False),
        ({}, {"naturalness": "stiff"}, False),
        (
            {"grade": "C", "critical": [err("omission")], "reason": REASON},
            {"grade": "C", "critical": [err("addition")], "reason": REASON},
            False,
        ),
        (
            {"grade": "C", "critical": [err("omission")], "reason": REASON},
            {"grade": "C", "critical": [err("omission", 2)], "reason": REASON},
            False,
        ),
        ({"names": SIGIL}, {"names": [nm("o1", "not_official", "Sigil")]}, False),
        ({"names": SIGIL}, {"names": [nm("o1", "official", "Sigils")]}, False),
        (
            {"grade": "C", "reason": REASON, "names": [nm("o1", "not_official", "Sigil")]},
            {"grade": "C", "reason": REASON, "names": [nm("o1", "not_official", "Sigil", True)]},
            False,
        ),
    ],
)
def test_what_counts_as_agreement(score, first, second, agree):
    world = base_world()
    item = "x:docB#p0"
    res = score(world, [world.jf("j1", {item: first}), world.jf("j2", {item: second})])
    assert res.code == (0 if agree else 3)
    assert res.report["state"] == ("complete" if agree else "pending")


# ------------------------------------------------------------ names, critical, ...


def names_world():
    sources = {
        "docN#p0": "Aaa Bbb Ccc Ddd.",
        "docN#p1": "Aaa again, Ccc again, Eee.",
        "docN#p2": "Eee Fff Ggg.",
        "docM#p0": "Aaa solo.",
        "docM#p1": "Aaa lost.",
        "docN#p3": "Hhh one.",
        "docN#p4": "Hhh two.",
    }
    paras = [para(pid, "en-zh", "story", text) for pid, text in sources.items()]
    spots = [
        ("o1", "docN#p0", "Aaa", "n1"), ("o2", "docN#p0", "Ccc", "n2"),
        ("o3", "docN#p1", "Aaa", "n1"), ("o4", "docN#p1", "Ccc", "n2"), ("o5", "docN#p1", "Eee", "n3"),
        ("o6", "docN#p2", "Eee", "n3"), ("o7", "docN#p2", "Ggg", "n4"),
        ("o8", "docM#p0", "Aaa", "n1"), ("o9", "docM#p1", "Aaa", "n1"),
        ("o10", "docN#p3", "Hhh", "n5"), ("o11", "docN#p4", "Hhh", "n5"),
    ]  # fmt: skip
    world = W(paras, [occ(o, p, sources[p], s, n) for o, p, s, n in spots])
    world.fail("x:docM#p1")
    return world


def test_name_accuracy_and_in_document_consistency(score):
    world = names_world()
    over = {
        "x:docN#p0": {"names": [nm("o1", "official", "Sigil"), nm("o2", "official", "Beam")]},
        "x:docN#p1": {"names": [nm("o3", "not_official", "sigil."), nm("o4", "omitted"), nm("o5", "official", "Alpha")]},
        "x:docN#p2": {"names": [nm("o6", "official", "Beta"), nm("o7", "undecidable")]},
        "x:docM#p0": {"names": [nm("o8", "official", "Zed")]},
        "x:docN#p3": {"names": [nm("o10", "omitted")]},
        "x:docN#p4": {"names": [nm("o11", "omitted")]},
    }
    res = score(world, world.agree(over))
    assert res.code == 0
    names = cell(res, direction="en-zh")["names"]
    # o9 sits in a run_failed paragraph and is left out; (docM, n1) has one occurrence, so is not counted
    # n5 is omitted twice: equal (empty) renderings do not make it consistent
    assert names["occurrences"] == 10 and names["official"] == 5
    assert names["accuracy"] == pytest.approx(5 / 10)
    assert names["multi_occurrence_names"] == 4 and names["consistent_names"] == 1
    assert names["consistency"] == pytest.approx(1 / 4)


def test_names_metrics_without_occurrences_are_null(score):
    world = base_world()
    res = score(world, world.agree())
    zh = cell(res)["names"]
    assert zh["occurrences"] == 0 and zh["accuracy"] is None
    assert zh["multi_occurrence_names"] == 0 and zh["consistency"] is None


def test_critical_errors_per_1000_source_scalars(score):
    world = base_world()
    over = {
        "x:docA#p0": {"grade": "C", "critical": [err("omission", 2)], "reason": REASON},
        "x:docA#p1": {"grade": "C", "critical": [err("number")], "reason": REASON},
    }
    res = score(world, world.agree(over))
    crit = cell(res)["critical"]
    scalars = sum(len(p["source"]) for p in world.paras[:4])  # docA#p4 is run_failed
    assert crit["paragraphs"] == 4 and crit["with_critical"] == 2 and crit["errors"] == 3
    assert crit["share"] == pytest.approx(0.5) and crit["source_scalars"] == scalars
    assert crit["per_1000_source_scalars"] == pytest.approx(3 / scalars * 1000)
    clean = cell(res, direction="en-zh")["critical"]
    assert clean["with_critical"] == 0 and clean["share"] == 0.0 and clean["per_1000_source_scalars"] == 0.0


def test_naturalness_distribution(score):
    world = base_world()
    over = {
        "x:docA#p1": {"naturalness": "stiff"},
        "x:docA#p2": {"naturalness": "disfluent"},
        "x:docA#p3": {"naturalness": "undecidable"},
    }
    res = score(world, world.agree(over))
    assert cell(res)["naturalness"] == {"natural": 1, "stiff": 1, "disfluent": 1, "undecidable": 1}
    assert cell(res, direction="en-zh")["naturalness"]["natural"] == 3


def test_literal_sentence_match_per_direction(score):
    world = base_world()
    world.paras[0]["reference"] = "The gate opens. Version 3.7 is out! Done."
    world.item("x:docA#p0")["translation"] = "The gate opens . Version 3.7 is out! done."
    world.paras[6]["reference"] = "Yes. Yes."
    world.item("x:docB#p0")["translation"] = "Yes."
    res = score(world, world.agree())
    zh = cell(res)["literal"]
    assert zh == {"paragraphs_with_reference": 1, "reference_sentences": 3, "matched": 2, "rate": pytest.approx(2 / 3)}
    en = cell(res, direction="en-zh")["literal"]
    assert en == {"paragraphs_with_reference": 1, "reference_sentences": 2, "matched": 1, "rate": 0.5}


# ------------------------------------------------------------------ validation

BAD_RECORDS = {
    "critical-error-with-grade-A": lambda r: r.update(critical_errors=[err("omission")]),
    "misleading-name-with-grade-B": lambda r: (
        r.update(grade="B"),
        r["names"][0].update(label="not_official", misleading=True),
    ),
    "official-without-rendering": lambda r: r["names"][0].update(rendering=None),
    "not-official-blank-rendering": lambda r: r["names"][0].update(label="not_official", rendering="  "),
    "omitted-with-rendering": lambda r: r["names"][0].update(label="omitted", rendering="x"),
    "undecidable-label-with-rendering": lambda r: r["names"][0].update(label="undecidable", rendering="x"),
    "misleading-with-official-label": lambda r: (
        r.update(grade="C", reason=REASON),
        r["names"][0].update(misleading=True),
    ),
    "names-missing-an-occurrence": lambda r: r.update(names=[]),
    "names-with-unknown-occurrence": lambda r: r["names"].append(nm("o9")),
    "names-repeat-an-occurrence": lambda r: r["names"].append(dict(r["names"][0])),
    "grade-C-without-reason": lambda r: r.update(grade="C"),
    "grade-C-with-blank-reason": lambda r: r.update(grade="C", reason="  "),
    "undecidable-grade-without-reason": lambda r: r.update(grade="undecidable"),
    "unknown-grade": lambda r: r.update(grade="D"),
    "unknown-naturalness": lambda r: r.update(naturalness="fine"),
    "unknown-label": lambda r: r["names"][0].update(label="maybe"),
    "unknown-error-type": lambda r: r.update(grade="C", reason=REASON, critical_errors=[err("typo")]),
    "error-count-zero": lambda r: r.update(grade="C", reason=REASON, critical_errors=[err("omission", 0)]),
    "unknown-record-key": lambda r: r.update(note="x"),
    "missing-record-key": lambda r: r.pop("naturalness"),
    "unknown-name-key": lambda r: r["names"][0].update(note="x"),
    "unknown-item": lambda r: r.update(item="nope"),
    "record-for-a-run-failed-item": lambda r: r.update(item="x:docA#p4"),
}


@pytest.mark.parametrize("mutate", BAD_RECORDS.values(), ids=list(BAD_RECORDS))
def test_invalid_judgment_record_is_refused(score, mutate):
    world = base_world()
    first = world.jf("j1")
    mutate(next(r for r in first["records"] if r["item"] == "x:docB#p0"))
    res = score(world, [first, world.jf("j2")])
    assert res.code == 2 and res.report is None and res.out == ""
    assert Path(res.paths[0]).name in res.err
    assert not [t for t in world.texts() if t in res.err]


def test_item_judged_twice_in_one_pass_is_refused(score):
    world = base_world()
    first = world.jf("j1")
    second = world.jf("j1", only={"x:docA#p0"})
    assert score(world, [first, second, world.jf("j2")]).code == 2
    first["records"].append(dict(first["records"][0]))
    assert score(world, [first, world.jf("j2")]).code == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda j: j.update(**{"pass": "j3"}),
        lambda j: j.update(schema="usability-judgment-v2"),
        lambda j: j.update(extra=1),
        lambda j: j.pop("records"),
    ],
    ids=["pass", "schema", "extra-key", "no-records"],
)
def test_invalid_judgment_file_header_is_refused(score, mutate):
    world = base_world()
    first = world.jf("j1")
    mutate(first)
    assert score(world, [first, world.jf("j2")]).code == 2


INPUT_CASES = {
    "names-unknown-paragraph": lambda w: w.occs[0].update(paragraph="nope#p9"),
    "names-unscored-paragraph": lambda w: w.occs[0].update(paragraph="docA#p5"),
    "names-surface-mismatch": lambda w: w.occs[0].update(surface="Brinemore"),
    "names-range-past-end": lambda w: w.occs[0].update(end=999),
    "names-tail-surface-with-end-past-source": lambda w: w.occs[0].update(
        surface=w.paras[6]["source"][w.occs[0]["start"] :], end=999
    ),
    "names-empty-range": lambda w: w.occs[0].update(end=w.occs[0]["start"]),
    "names-duplicate-occ": lambda w: w.occs.append(dict(w.occs[0])),
    "names-empty-forms": lambda w: w.occs[0].update(official_forms=[]),
    "items-missing-for-a-paragraph": lambda w: w.items.pop(),
    "items-for-an-unscored-paragraph": lambda w: w.items.append({**w.items[0], "item": "e", "paragraph": "docA#p5"}),
    "items-for-an-unknown-paragraph": lambda w: w.items.append({**w.items[0], "item": "e", "paragraph": "nope#p9"}),
    "items-duplicate-id": lambda w: w.items[1].update(item=w.items[0]["item"]),
    "items-two-for-one-paragraph-and-arm": lambda w: w.items.append({**w.items[0], "item": "e"}),
    "items-run-failed-with-translation": lambda w: w.items[0].update(run_failed=True),
    "items-null-translation-not-failed": lambda w: w.items[0].update(translation=None),
    "sample-scored-with-exclusion": lambda w: w.paras[0].update(exclusion="duplicate"),
    "sample-unscored-without-exclusion": lambda w: w.paras[5].update(exclusion=None),
    "sample-unknown-direction": lambda w: w.paras[0].update(direction="zh-fr"),
    "sample-unknown-genre": lambda w: w.paras[0].update(genre="poem"),
    "sample-unknown-exclusion": lambda w: w.paras[5].update(exclusion="other"),
    "sample-duplicate-paragraph-id": lambda w: w.paras.append(dict(w.paras[0])),
    "sample-reference-not-a-string": lambda w: w.paras[0].update(reference=5),
}


@pytest.mark.parametrize("mutate", INPUT_CASES.values(), ids=list(INPUT_CASES))
def test_invalid_input_file_is_refused(score, mutate):
    world = base_world()
    judgments = world.agree()
    texts = world.texts()
    mutate(world)
    res = score(world, judgments)
    assert res.code == 2 and res.report is None and res.out == ""
    assert res.err.strip() and not [t for t in texts if t in res.err]


def test_an_arm_missing_an_item_is_refused(score):
    world = W(base_world().paras, arms=("x", "y"))
    world.items.remove(world.item("y:docA#p1"))
    assert score(world, world.agree()).code == 2


MALFORMED = [
    ("sample", lambda f: "not json {"),
    ("sample", lambda f: '{"schema": "usability-sample-v1", "schema": "usability-sample-v1"}'),
    ("sample", lambda f: [f]),
    ("sample", lambda f: {**f, "set": 3}),
    ("sample", lambda f: {**f, "paragraphs": "oops"}),
    ("sample", lambda f: {**f, "paragraphs": [1]}),
    ("sample", lambda f: {**f, "paragraphs": [{"id": "a"}]}),
    ("sample", lambda f: {**f, "paragraphs": [{**f["paragraphs"][0], "scored": "yes"}]}),
    ("sample", lambda f: {**f, "paragraphs": [{**f["paragraphs"][0], "source": None}]}),
    ("names", lambda f: {**f, "occurrences": {}}),
    ("names", lambda f: {**f, "occurrences": [{**f["occurrences"][0], "start": "0"}]}),
    ("names", lambda f: {**f, "occurrences": [{**f["occurrences"][0], "start": True}]}),
    ("names", lambda f: {**f, "occurrences": [{**f["occurrences"][0], "official_forms": "x"}]}),
    ("items", lambda f: {**f, "items": [None]}),
    ("items", lambda f: {**f, "items": [{**f["items"][0], "run_failed": 0}]}),
    ("items", lambda f: {**f, "items": [{**f["items"][0], "translation": 5}]}),
    ("judgment", lambda f: {**f, "records": "x"}),
    ("judgment", lambda f: {**f, "records": [{**f["records"][0], "names": "x"}]}),
    ("judgment", lambda f: {**f, "records": [{**f["records"][0], "critical_errors": ["x"]}]}),
    ("judgment", lambda f: {**f, "records": [{**f["records"][0], "critical_errors": [err("omission", "1")]}]}),
    ("judgment", lambda f: {**f, "records": [{**f["records"][0], "critical_errors": [err("omission", True)]}]}),
    ("judgment", lambda f: {**f, "records": [{**f["records"][0], "reason": 5}]}),
]


KWARG = {"sample": "samples", "names": "names", "items": "items"}


@pytest.mark.parametrize(("kind", "mutate"), MALFORMED)
def test_wrong_shapes_exit_2_not_a_crash(score, kind, mutate):
    world = base_world()
    files = world.files()
    j1 = world.jf("j1")
    if kind == "judgment":
        j1 = mutate(j1)
        res = score(world, [j1, world.jf("j2")])
    else:
        res = score(world, world.agree(), **{KWARG[kind]: [mutate(files[kind])]})
    assert res.code == 2 and res.out == "" and res.err.strip()


# ------------------------------------------------------------------------ scan

SEALED_SRC = "Quillfeather harbor lanterns glow softly."
SEALED_REF = "Marrowdeep tidal chimes ring slowly."
SEALED_UNSCORED = "Ephemeral ledger entries remain sealed."
SEALED_TEXTS = [SEALED_SRC, SEALED_REF, SEALED_UNSCORED]


def sealed_paras():
    return [
        para("s#p0", "zh-en", "story", SEALED_SRC, SEALED_REF),
        para("s#p1", "en-zh", "mixed", SEALED_UNSCORED, exclusion="duplicate"),
    ]


def sample_doc(set_, paragraphs):
    return {"schema": "usability-sample-v1", "set": set_, "paragraphs": paragraphs}


ScanResult = namedtuple("ScanResult", "code rows out err")


class Scan:
    def __init__(self, ev, tmp_path, capsys):
        self.ev, self.tmp_path, self.capsys = ev, tmp_path, capsys
        self.counter = iter(range(10_000))

    def file(self, name, content):
        path = self.tmp_path / f"{next(self.counter)}-{name}"
        if not isinstance(content, (str, bytes)):
            content = json.dumps(content, ensure_ascii=False)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return str(path)

    def sealed(self, set_="H1", paragraphs=None):
        return ["--sealed", self.file("sealed.json", sample_doc(set_, paragraphs or sealed_paras()))]

    def run(self, *argv, forbidden=SEALED_TEXTS):
        code = call(self.ev, ["scan", *argv])
        captured = self.capsys.readouterr()
        for text in forbidden:  # counts only: no sealed window may reach stdout or stderr
            for start in range(len(text) - WINDOW + 1):
                assert text[start : start + WINDOW] not in captured.out + captured.err
        rows = json.loads(captured.out)["rows"] if code in (0, 1) else None
        return ScanResult(code, rows, captured.out, captured.err)


@pytest.fixture()
def scan(ev, tmp_path, capsys):
    return Scan(ev, tmp_path, capsys)


def test_a_12_scalar_window_hits_and_an_11_scalar_one_does_not(scan):
    hit = scan.file("in.txt", f"zzz {SEALED_SRC[3:15]} zzz")
    miss = scan.file("in.txt", f"zzz {SEALED_SRC[3:14]} zzz")
    res = scan.run(*scan.sealed(), "--input", hit)
    assert res.code == 1
    assert res.rows == [
        {
            "input": hit,
            "sealed": "H1",
            "windows_hit": 1,
            "windows_cleared": 0,
            "windows_uncleared": 1,
            "input_units_with_uncleared_hit": 1,
            "sealed_paragraphs_with_uncleared_hit": 1,
        }
    ]
    res = scan.run(*scan.sealed(), "--input", miss)
    assert res.code == 0 and res.rows[0]["windows_hit"] == 0 and res.rows[0]["windows_uncleared"] == 0


def test_every_overlapping_window_counts_once(scan):
    shared = SEALED_SRC[3:23]  # 20 scalars: 9 windows of 12
    path = scan.file("in.txt", f"{shared} and again {shared}")
    res = scan.run(*scan.sealed(), "--input", path)
    assert res.rows[0]["windows_hit"] == 20 - WINDOW + 1
    assert res.rows[0]["sealed_paragraphs_with_uncleared_hit"] == 1


def test_references_and_unscored_paragraphs_are_sealed_too(scan):
    path = scan.file("in.txt", f"{SEALED_REF[2:14]} | {SEALED_UNSCORED[:12]}")
    res = scan.run(*scan.sealed(), "--input", path)
    assert res.code == 1
    assert res.rows[0]["windows_hit"] == 2 and res.rows[0]["sealed_paragraphs_with_uncleared_hit"] == 2


def test_windows_count_unicode_scalars(scan):
    astral = "𠀀𠀁𠀂𠀃𠀄𠀅𠀆𠀇𠀈𠀉𠀊𠀋𠀌"  # 13 scalars, 26 UTF-16 units
    sealed = scan.sealed(paragraphs=[para("a#p0", "zh-en", "story", astral)])
    assert scan.run(*sealed, "--input", scan.file("in.txt", astral[:12]), forbidden=[astral]).rows[0]["windows_hit"] == 1
    assert scan.run(*sealed, "--input", scan.file("in.txt", astral[:11]), forbidden=[astral]).rows[0]["windows_hit"] == 0


def test_text_is_compared_raw_without_newline_translation(scan):
    text = "first line here\r\nsecond line here"
    sealed = scan.sealed(paragraphs=[para("a#p0", "zh-en", "story", text)])
    res = scan.run(*sealed, "--input", scan.file("in.txt", text.encode("utf-8")), forbidden=[text])
    assert res.rows[0]["windows_hit"] == len(text) - WINDOW + 1
    lf_only = scan.file("in.txt", text.replace("\r\n", "\n"))
    assert scan.run(*sealed, "--input", lf_only, forbidden=[text]).rows[0]["windows_hit"] < len(text) - WINDOW + 1


def test_window_option(scan):
    path = scan.file("in.txt", f"zz {SEALED_SRC[3:8]} zz")
    assert scan.run(*scan.sealed(), "--input", path).code == 0
    assert scan.run(*scan.sealed(), "--input", path, "--window", "5").code == 1


def test_diff_scans_added_lines_only(scan):
    removed = f"diff --git a/f b/f\n--- a/f\n+++ b/{SEALED_SRC}\n@@ -1,3 +1,2 @@\n context {SEALED_SRC}\n-removed {SEALED_SRC}\n+plain added line\n"
    res = scan.run(*scan.sealed(), "--diff", scan.file("a.patch", removed))
    assert res.code == 0 and res.rows[0]["windows_hit"] == 0
    added = removed + f"+kept {SEALED_SRC}\n"
    res = scan.run(*scan.sealed(), "--diff", scan.file("b.patch", added))
    assert res.code == 1 and res.rows[0]["windows_hit"] == len(SEALED_SRC) - WINDOW + 1


def test_diff_added_lines_are_joined_by_newline_without_the_plus(scan):
    text = "alpha beta gamma\ndelta epsilon zeta"
    sealed = scan.sealed(paragraphs=[para("a#p0", "zh-en", "story", text)])
    patch = "--- a/f\n+++ b/f\n@@ -0,0 +1,2 @@\n+alpha beta gamma\n+delta epsilon zeta\n"
    res = scan.run(*sealed, "--diff", scan.file("a.patch", patch), forbidden=[text])
    assert res.rows[0]["windows_hit"] == len(text) - WINDOW + 1


def test_diff_lines_split_on_newline_only(scan):
    text = "one two three\u2028four five six\x0cseven eight nine"
    sealed = scan.sealed(paragraphs=[para("a#p0", "zh-en", "story", text)])
    patch = f"--- a/f\n+++ b/f\n@@ -0,0 +1 @@\n+{text}\n"
    res = scan.run(*sealed, "--diff", scan.file("a.patch", patch), forbidden=[text])
    assert res.rows[0]["windows_hit"] == len(text) - WINDOW + 1


def test_input_sample_units_and_paragraph_ids(scan):
    d_doc = sample_doc(
        "D",
        [
            para("d#p0", "zh-en", "story", f"x {SEALED_SRC[3:15]} x", f"y {SEALED_REF[2:14]} y"),
            para("d#p1", "zh-en", "story", "nothing shared here at all"),
            para("d#p2", "en-zh", "mixed", f"{SEALED_SRC[20:32]}", exclusion="duplicate"),
        ],
    )
    res = scan.run(*scan.sealed(), "--input-sample", scan.file("d.json", d_doc))
    assert res.code == 1
    (row,) = res.rows
    assert row["input"] == "D" and row["sealed"] == "H1"
    assert row["windows_hit"] == 3 and row["input_units_with_uncleared_hit"] == 3
    assert row["uncleared_input_paragraph_ids"] == ["d#p0", "d#p2"]
    assert row["sealed_paragraphs_with_uncleared_hit"] == 1


def test_one_row_per_input_and_sealed_set(scan):
    h2 = scan.sealed("H2", [para("t#p0", "en-zh", "guide", "Bramble cartographers whisper secrets.")])
    path = scan.file("in.txt", f"{SEALED_SRC[3:15]} // Bramble cart")
    res = scan.run(*scan.sealed(), *h2, "--input", path, forbidden=[*SEALED_TEXTS, "Bramble cartographers whisper secrets."])
    assert [(r["sealed"], r["windows_hit"]) for r in res.rows] == [("H1", 1), ("H2", 1)]


def test_clear_file_clears_only_the_windows_it_contains(scan):
    shared = SEALED_SRC[3:23]  # 9 windows
    path = scan.file("in.txt", shared)
    whole = scan.file("clear.txt", f"zz {shared} zz")
    res = scan.run(*scan.sealed(), "--input", path, "--clear", whole)
    assert res.code == 0
    assert (res.rows[0]["windows_hit"], res.rows[0]["windows_cleared"], res.rows[0]["windows_uncleared"]) == (9, 9, 0)
    assert res.rows[0]["input_units_with_uncleared_hit"] == 0
    assert res.rows[0]["sealed_paragraphs_with_uncleared_hit"] == 0
    part = scan.file("clear.txt", shared[:14])  # 3 windows
    res = scan.run(*scan.sealed(), "--input", path, "--clear", part)
    assert res.code == 1
    assert (res.rows[0]["windows_cleared"], res.rows[0]["windows_uncleared"]) == (3, 6)
    assert res.rows[0]["input_units_with_uncleared_hit"] == 1


def git(repo, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    cmd = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    done = subprocess.run([*cmd, *args], check=True, env=env, capture_output=True, text=True)
    return done.stdout.strip()


def test_clear_git_uses_the_tree_of_the_named_commit(scan, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    shared = SEALED_SRC[3:23]
    (repo / "docs").mkdir()
    (repo / "docs" / "note one.txt").write_text(f"see {shared} here", encoding="utf-8")
    (repo / "blob.bin").write_bytes(b"\xff\xfe" + shared.encode("utf-8"))  # undecodable: skipped
    (repo / "nul.bin").write_bytes(b"\0" + shared.encode("utf-8"))  # binary: skipped
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "one")
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "rm", "-q", "docs/note one.txt")
    git(repo, "commit", "-q", "-m", "two")
    second = git(repo, "rev-parse", "HEAD")
    path = scan.file("in.txt", shared)
    res = scan.run(*scan.sealed(), "--input", path, "--clear-git", f"{repo}:{first}")
    assert res.code == 0 and res.rows[0]["windows_cleared"] == 9
    res = scan.run(*scan.sealed(), "--input", path, "--clear-git", f"{repo}:{second}")
    assert res.code == 1 and res.rows[0]["windows_cleared"] == 0 and res.rows[0]["windows_uncleared"] == 9


@pytest.mark.parametrize("spec", ["no-colon", "{repo}:--output=x", "{repo}:no-such-commit", "{missing}:HEAD"])
def test_bad_clear_git_spec_exits_2(scan, tmp_path, spec):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    spec = spec.format(repo=repo, missing=tmp_path / "missing")
    path = scan.file("in.txt", SEALED_SRC[3:15])
    res = scan.run(*scan.sealed(), "--input", path, "--clear-git", spec)
    assert res.code == 2 and res.out == "" and res.err.strip()


def test_scan_exit_2_on_invalid_input(scan, tmp_path):
    ok = scan.file("in.txt", "text")
    assert scan.run(*scan.sealed(), "--input", str(tmp_path / "missing.txt")).code == 2
    assert scan.run(*scan.sealed(), "--input", ok, "--clear", str(tmp_path / "missing.txt")).code == 2
    assert scan.run(*scan.sealed(), "--input", ok, "--window", "0").code == 2
    assert scan.run(*scan.sealed()).code == 2  # nothing to scan
    assert scan.run("--input", ok).code == 2  # nothing sealed
    assert scan.run(*scan.sealed(), "--input", scan.file("bad.bin", b"\xff\xfe\x00")).code == 2


@pytest.mark.parametrize("mutate", [m for kind, m in MALFORMED if kind == "sample"])
def test_scan_wrong_shapes_exit_2(scan, mutate):
    bad = mutate(sample_doc("H1", sealed_paras()))
    for args in (
        ["--sealed", scan.file("s.json", bad), "--input", scan.file("in.txt", "x")],
        [*scan.sealed(), "--input-sample", scan.file("d.json", bad)],
    ):
        res = scan.run(*args)
        assert res.code == 2 and res.out == "" and res.err.strip()


def test_scan_script_runs_as_a_program(scan):
    sealed = scan.sealed()
    path = scan.file("in.txt", SEALED_SRC)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "scan", *sealed, "--input", path],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 1
    assert json.loads(done.stdout)["rows"][0]["windows_hit"] == len(SEALED_SRC) - WINDOW + 1
    assert SEALED_SRC[:WINDOW] not in done.stdout + done.stderr
