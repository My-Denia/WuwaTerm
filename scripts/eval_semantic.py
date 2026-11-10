#!/usr/bin/env python3
"""Run and score the semantic-review / translation-context evaluation.

The evaluation corpus and its gold labels live outside every repository, in a
run directory frozen before the first model call (ADR 0018, ADR 0019). This
script never copies corpus text anywhere but that run directory and the output
directory the caller names.

Subcommands
-----------

``verify``   check every frozen file and corpus input against the SHA-256 in
             the run directory's ``ledger.json`` (``wave0_freeze``).
``run``      verify, then run layers ``P``, ``E``, ``M``, ``Mprime`` in process
             through the real application layer and model gateway. Every HTTP
             attempt to the gateway is counted in the ledger *before* it is
             sent; evaluation stops at 420 evaluation calls and the ledger
             refuses anything past 450 in total.
``score``    score a finished round (plus any adjudication files) and write
             ``receipt.json``.
``ledger``   ``show``; ``reserve`` calls for a run outside this script
             (``w2`` end-to-end check, ``deploy-probe``); ``settle`` it with the
             actual count read from the API log (``llm call purpose=...``).

Credentials come from the environment (``WUWATERM_OPENAI_BASE_URL``,
``WUWATERM_OPENAI_API_KEY``, ``WUWATERM_OPENAI_MODEL``) or from
``--env-file``, which is read into this process only. Nothing here prints or
writes a credential.

``--fake-transport`` runs the same code against an in-process fake gateway. It
never opens the real ledger for writing: it works on a copy in the output
directory and writes ``receipt.dry-run.json`` there.

Exit codes: 0 done; 1 refused (verification failed, budget, bad input);
2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from wuwaterm import application as app_layer  # noqa: E402
from wuwaterm import llm_transport  # noqa: E402
from wuwaterm.review import VERDICT_VERIFIED, review_pair  # noqa: E402
from wuwaterm.segmentation import WHITESPACE, segment_text  # noqa: E402
from wuwaterm.semantic_review import (  # noqa: E402
    Precedent,
    SemanticReviewJob,
)
from wuwaterm.semantic_review_contract import SEMANTIC_RESPONSE_BYTE_BUDGET  # noqa: E402
from wuwaterm.sentence import SentenceTranslator  # noqa: E402

EVAL_SELF_LIMIT = 420
STOP_LINE_TOTAL = 450
OWNER_APPROVED_MAX = 500
P_FP_PER_1000_ZH = 1.0
LATENCY_P95_LIMIT_S = 30.0
DROPPED_RATIO_LIMIT = 0.15
M_PRECISION_MIN = 0.60
M_PRECISION_MIN_SAMPLE = 20
P_REAL_ERROR_MIN_SAMPLE = 10
P_REAL_ERROR_RECALL_MIN = 0.60
E_RECALL_MIN = {
    "negation_change": 0.80,
    "quantity_change": 0.80,
    "omission": 0.80,
    "reference_inconsistency": 0.60,
    "meaning_shift": 0.60,
    "term_sense": 0.60,
}
E_MIN_PER_CATEGORY = 10
E_MIN_GROUPS = 3
M_MIN_OFFICIAL = 30
M_MIN_ORDINARY = 10
MPRIME_MIN_SAME = 10
MPRIME_MIN_DIFF = 5
LAYERS = ("P", "E", "M", "Mprime")
NOT_REVIEWED_STREAK_STOP = 3
TIMEOUT_STREAK_STOP = 3
B1_SANITIZED = "work/B1-zh.sanitized.txt"
SNAPSHOT_DB = "work/terms-3.7.snapshot.db"
ENV_KEYS = re.compile(r"^(WUWATERM_OPENAI_|OPENAI_|WUWATERM_SEMANTIC_REVIEW_)[A-Z_]+$")

MET = "met"
NOT_MET = "not_met"
UNDECIDABLE = "undecidable"
PENDING = "pending_adjudication"


class Refused(RuntimeError):
    """A precondition failed; nothing further is run."""


def config_fingerprint() -> dict[str, Any]:
    """SHA-256 of everything that shapes a model request in this evaluation.

    Prompt texts, the code that assembles them, and the request options as
    this process's environment resolves them. Model name and gateway host are
    recorded as plain values (no credential is part of them).
    """
    import inspect

    from wuwaterm import semantic_review, sentence
    from wuwaterm.semantic_review_contract import PRECEDENT_WORDING

    def h(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    thinking = (
        os.getenv(semantic_review.SEMANTIC_THINKING_ENV, "").strip()
        or semantic_review.SEMANTIC_THINKING_DEFAULT
    )
    max_tokens, extra_body = semantic_review.thinking_request_options(thinking)
    json_mode = os.getenv("WUWATERM_SEMANTIC_REVIEW_JSON_MODE", "1").strip() != "0"
    base_url, _key, model = llm_transport.resolve_llm_config(semantic_review.SEMANTIC_MODEL_ENV)
    parts = {
        "semantic_system_prompt": h(semantic_review.SYSTEM_PROMPT_TEMPLATE),
        "semantic_term_fact_wording": h(semantic_review.TERM_FACT_WORDING),
        "precedent_wording": h(PRECEDENT_WORDING),
        "semantic_message_builder": h(
            inspect.getsource(semantic_review.build_messages)
            + inspect.getsource(semantic_review.term_facts)
        ),
        "translation_context_rules": h(sentence._CONTEXT_RULES_INSTRUCTION),
        "translation_prompt_builder": h(
            inspect.getsource(sentence._call_llm_async_with_client)
            + inspect.getsource(sentence._context_instruction)
        ),
    }
    options = {
        "thinking_mode": thinking,
        "semantic_max_tokens": max_tokens,
        "semantic_extra_body": extra_body,
        "semantic_response_format": "json_object" if json_mode else None,
        "model": model,
        "gateway_host": httpx.URL(base_url).host if base_url else None,
    }
    combined = h(json.dumps({"parts": parts, "options": options}, sort_keys=True))
    return {"config_sha256": combined, "parts": parts, "options": options}


# --------------------------------------------------------------------------
# Files, hashes, ledger
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def verify_frozen(run_dir: Path, corpus_dir: Path, ledger: Mapping[str, Any]) -> list[str]:
    """Return a list of problems; empty means every frozen input matches."""
    problems: list[str] = []
    freeze = ledger.get("wave0_freeze")
    if not isinstance(freeze, Mapping) or not isinstance(freeze.get("files"), Mapping):
        return ["ledger has no wave0_freeze.files"]
    for rel, expected in freeze["files"].items():
        path = run_dir / rel
        if not path.is_file():
            problems.append(f"missing frozen file {rel}")
            continue
        if sha256_file(path) != expected.get("sha256"):
            problems.append(f"sha256 mismatch {rel}")
        elif "bytes" in expected and path.stat().st_size != expected["bytes"]:
            problems.append(f"size mismatch {rel}")
    for rel, expected in (freeze.get("corpus_inputs") or {}).items():
        path = corpus_dir / rel
        if not path.is_file():
            problems.append(f"missing corpus input {rel}")
        elif sha256_file(path) != expected:
            problems.append(f"sha256 mismatch corpus {rel}")
    return problems


class Ledger:
    """The single call ledger. Every write is atomic and happens before a send."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = load_json(path)
        self.data.setdefault("records", [])
        self.data.setdefault("model_calls_total", 0)

    @property
    def total(self) -> int:
        return int(self.data["model_calls_total"])

    def eval_total(self) -> int:
        return sum(
            int(record.get("calls", 0))
            for record in self.data["records"]
            if record.get("purpose") == "eval"
        )

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def open_record(self, **fields: Any) -> dict[str, Any]:
        record = {"time": now_iso(), "calls": 0, **fields}
        self.data["records"].append(record)
        self.save()
        return record

    def charge(self, record: dict[str, Any]) -> None:
        """Count one HTTP attempt before it is sent, or refuse it."""
        if self.total + 1 > STOP_LINE_TOTAL:
            raise llm_transport.LLMTranslationError(
                llm_transport.BUDGET_EXHAUSTED_NOTICE, reason="budget"
            )
        if record.get("purpose") == "eval" and self.eval_total() + 1 > EVAL_SELF_LIMIT:
            raise llm_transport.LLMTranslationError(
                llm_transport.BUDGET_EXHAUSTED_NOTICE, reason="budget"
            )
        cap = record.get("max_calls")
        if cap is not None and int(record.get("calls", 0)) + 1 > int(cap):
            record["cap_reached"] = True
            raise llm_transport.LLMTranslationError(
                llm_transport.BUDGET_EXHAUSTED_NOTICE, reason="budget"
            )
        record["calls"] = int(record.get("calls", 0)) + 1
        self.data["model_calls_total"] = self.total + 1
        self.save()

    def reserve(self, purpose: str, max_calls: int, note: str) -> dict[str, Any]:
        if purpose not in ("w2", "deploy-probe"):
            raise Refused("purpose must be w2 or deploy-probe")
        if max_calls <= 0:
            raise Refused("max-calls must be positive")
        if self.total + max_calls > STOP_LINE_TOTAL:
            raise Refused(
                f"reservation of {max_calls} would take the ledger past {STOP_LINE_TOTAL}"
            )
        rid = f"res-{len(self.data['records']) + 1:03d}"
        record = self.open_record(
            purpose=purpose,
            round=None,
            is_retry=False,
            reused_samples=False,
            reservation_id=rid,
            reserved=max_calls,
            settled=False,
            note=note,
        )
        record["calls"] = max_calls
        self.data["model_calls_total"] = self.total + max_calls
        self.save()
        return record

    def settle(self, rid: str, actual: int) -> dict[str, Any]:
        for record in self.data["records"]:
            if record.get("reservation_id") == rid:
                if record.get("settled"):
                    raise Refused(f"{rid} is already settled")
                if actual < 0:
                    raise Refused("actual must be >= 0")
                delta = actual - int(record["calls"])
                record["calls"] = actual
                record["settled"] = True
                record["settled_at"] = now_iso()
                self.data["model_calls_total"] = self.total + delta
                self.save()
                return record
        raise Refused(f"no reservation {rid}")


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass
class Inputs:
    run_dir: Path
    corpus_dir: Path
    alignment: dict[str, Any]
    freeze: dict[str, Any]
    injections: dict[str, Any]
    variants: dict[str, Any]
    m_gold: dict[str, Any]
    mprime: dict[str, Any]

    def doc_text(self, doc: str) -> str:
        """Monolingual M-layer document. B1 is read only from the sanitized copy."""
        if doc == "B1":
            path = self.run_dir / B1_SANITIZED
        else:
            recorded = Path(self.m_gold["docs"][doc])
            path = self.corpus_dir / recorded.parent.name / recorded.name
            if "B1" in recorded.parent.name:
                raise Refused("B1 may only be read from the sanitized copy")
        return path.read_text(encoding="utf-8")


def load_inputs(run_dir: Path, corpus_dir: Path) -> Inputs:
    return Inputs(
        run_dir=run_dir,
        corpus_dir=corpus_dir,
        alignment=load_json(run_dir / "p-layer-alignment.json"),
        freeze=load_json(run_dir / "p-diff-freeze.json"),
        injections=load_json(run_dir / "e-injections.json"),
        variants=load_json(run_dir / "e-variants.json"),
        m_gold=load_json(run_dir / "m-gold.json"),
        mprime=load_json(run_dir / "mprime-gold.json"),
    )


def load_env_file(path: Path) -> int:
    """Read KEY=VALUE credentials into this process. Returns the count, never values."""
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if ENV_KEYS.match(key):
            os.environ[key] = value
            count += 1
    return count


# --------------------------------------------------------------------------
# Fake gateway for the dry run
# --------------------------------------------------------------------------


def _fake_issue_quote(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if len(line) >= 6:
            return line[: min(12, len(line))]
    return None


def fake_gateway(request: httpx.Request) -> httpx.Response:
    """Deterministic stand-in: one quoted issue per review, echo per translation."""
    body = json.loads(request.content)
    system = body["messages"][0]["content"]
    user = body["messages"][-1]["content"]
    if system.startswith("You review one translated segment"):
        payload = json.loads(user.split("\n", 1)[1])
        quote = _fake_issue_quote(payload["target"])
        issues = []
        if quote is not None and payload["target"].count(quote) == 1:
            issues.append(
                {
                    "category": "meaning_shift",
                    "confidence": "low",
                    "source_quote": None,
                    "target_quote": quote,
                    "rationale": "dry run",
                    # Exercises term_effect on the real snapshot at no cost.
                    "suggestion": {"replacement": quote[::-1]},
                    "alternative": None,
                }
            )
        content = json.dumps({"issues": issues}, ensure_ascii=False)
    else:
        content = f"[dry-run translation of {len(user)} scalars]"
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]}
    )


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


async def legacy_translate_plain(
    translator: SentenceTranslator, prepared: str
) -> list[tuple[str, str]]:
    """The pre-C2 path, for the M-layer baseline: (chunk, translation) pairs.

    Mirrors main before this change: one call up to the 2,000-scalar limit,
    otherwise ``split_plain_text`` chunks; the caller joins with "\\n". No
    context is sent, so the original prompt is used byte for byte.
    """
    if len(prepared) <= app_layer.LLM_INPUT_CHAR_LIMIT:
        chunks = [prepared]
    else:
        chunks = app_layer.split_plain_text(prepared, app_layer.LLM_INPUT_CHAR_LIMIT)
    out = []
    for chunk in chunks:
        out.append(
            (chunk, await translator.translate_async(chunk, to_chinese=False, propagate_errors=True))
        )
    return out


@dataclass
class Runner:
    inputs: Inputs
    out_dir: Path
    ledger: Ledger
    record: dict[str, Any]
    translator: SentenceTranslator
    service: Any
    explain_language: str = "zh"
    not_reviewed_streak: int = 0
    timeout_streak: int = 0
    resume: bool = False

    def done(self, rel: str) -> bool:
        """With --resume: an output that exists and did not fail is kept."""
        path = self.out_dir / rel
        if not (self.resume and path.is_file()):
            return False
        data = load_json(path)
        return not data.get("error_code")

    def save(self, rel: str, data: Any) -> None:
        write_json_atomic(self.out_dir / rel, data)

    async def review(self, key: str, job: SemanticReviewJob) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = await app_layer.semantic_review_async(
            self.service,
            self.translator,
            job,
            request_id=key,
        )
        elapsed = time.perf_counter() - started
        result = {
            "key": key,
            "latency_s": round(elapsed, 3),
            "error_code": outcome.error_code,
            "bytes": None if outcome.data is None else len(outcome.data),
            "body": outcome.body,
            "model_calls": outcome.model_calls,
        }
        if outcome.error_code is not None:
            self.save(f"errors/{key.replace(':', '_')}.json", result)
        if outcome.error_code == app_layer.ERROR_LLM_BUDGET_EXHAUSTED:
            raise Refused(f"budget stop at {key}")
        reason = getattr(outcome.llm_failure, "reason", None)
        if outcome.error_code is not None and reason == "timeout":
            # A slow segment is a result (it fails the latency gate), not a
            # broken gateway: record it and go on, unless it keeps happening.
            result["timeout"] = True
            self.timeout_streak += 1
            if self.timeout_streak >= TIMEOUT_STREAK_STOP:
                raise Refused(f"{TIMEOUT_STREAK_STOP} timeouts in a row at {key}")
            return result
        self.timeout_streak = 0
        if outcome.error_code is not None:
            # A gateway that refuses one request (bad model name, rejected
            # response_format, auth) refuses the rest: stop before spending more.
            raise Refused(f"gateway error {outcome.error_code} at {key}")
        if outcome.body is not None and outcome.body["status"] == "not_reviewed":
            self.not_reviewed_streak += 1
            if self.not_reviewed_streak >= NOT_REVIEWED_STREAK_STOP:
                raise Refused(f"{NOT_REVIEWED_STREAK_STOP} not_reviewed answers in a row at {key}")
        else:
            self.not_reviewed_streak = 0
        return result

    async def translate(self, text: str, context: app_layer.TranslationContext | None) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = await app_layer.translate_request_async(
            self.service,
            self.translator,
            app_layer.TranslationJob(text=text, forced_to_chinese=False, context=context),
        )
        if outcome.error_code == app_layer.ERROR_LLM_BUDGET_EXHAUSTED:
            raise Refused("budget stop during translation")
        if outcome.error_code is not None:
            if getattr(outcome.llm_failure, "reason", None) == "timeout":
                self.timeout_streak += 1
                if self.timeout_streak >= TIMEOUT_STREAK_STOP:
                    raise Refused(f"{TIMEOUT_STREAK_STOP} timeouts in a row during translation")
                return {"kind": outcome.kind, "error_code": outcome.error_code, "text": None,
                        "timeout": True, "latency_s": round(time.perf_counter() - started, 3)}
            raise Refused(f"gateway error {outcome.error_code} during translation")
        self.timeout_streak = 0
        return {
            "kind": outcome.kind,
            "error_code": outcome.error_code,
            "text": outcome.text,
            "latency_s": round(time.perf_counter() - started, 3),
        }

    # -- P ----------------------------------------------------------------
    async def run_p(self) -> None:
        for unit in self.inputs.alignment["units"]:
            for direction, source, target in (
                ("zh->en", unit["zh_text"], unit["en_text"]),
                ("en->zh", unit["en_text"], unit["zh_text"]),
            ):
                key = f"P:{unit['unit_id']}:{direction}"
                if self.done(f"P/{unit['unit_id']}.{direction}.json"):
                    continue
                result = await self.review(
                    key,
                    SemanticReviewJob(
                        source=source,
                        target=target,
                        direction="en" if direction == "zh->en" else "zh",
                        explain_language=self.explain_language,
                    ),
                )
                result.update(layer="P", unit_id=unit["unit_id"], direction=direction)
                self.save(f"P/{unit['unit_id']}.{direction}.json", result)

    # -- E ----------------------------------------------------------------
    async def run_e(self) -> None:
        for variant in self.inputs.variants["variants"]:
            key = f"E:{variant['variant_id']}"
            if self.done(f"E/{variant['variant_id']}.json"):
                continue
            result = await self.review(
                key,
                SemanticReviewJob(
                    source=variant["source_text"],
                    target=variant["target_text"],
                    direction="en",
                    explain_language=self.explain_language,
                ),
            )
            result.update(layer="E", variant_id=variant["variant_id"])
            self.save(f"E/{variant['variant_id']}.json", result)

    # -- M ----------------------------------------------------------------
    async def run_m(self) -> None:
        for doc in self.inputs.m_gold["docs"]:
            if self.done(f"M/{doc}.json"):
                continue
            raw = self.inputs.doc_text(doc)
            # The old path prepares the whole text (blank lines folded away);
            # the new path cuts the text as written, like Core does.
            prepared = self.translator.prepare_text(raw)
            started = time.perf_counter()
            try:
                old_pairs = await legacy_translate_plain(self.translator, prepared)
            except llm_transport.LLMTranslationError as exc:
                raise Refused(f"old-path translation failed in {doc}: {exc.reason}") from exc
            old_latency = time.perf_counter() - started
            # The new path is Core's own long-text path (segmentation-v1,
            # previous segment as context, context prompt rules), observed
            # per segment so its pairs can be reviewed and its assembly checked.
            segments_out: list[dict[str, Any]] = []

            def observe(segment: Any, translated: str) -> None:
                segments_out.append(
                    {
                        "start": segment.start,
                        "end": segment.end,
                        "core_start": segment.core_start,
                        "core_end": segment.core_end,
                        "hard_cut": segment.hard_cut,
                        "translation": translated,
                    }
                )

            started = time.perf_counter()
            outcome = await app_layer._translate_long_plain_text(
                self.translator,
                raw,
                to_chinese=False,
                context=app_layer.TranslationContext(),
                on_segment=observe,
            )
            new_latency = time.perf_counter() - started
            if outcome.error_code == app_layer.ERROR_LLM_BUDGET_EXHAUSTED:
                raise Refused(f"budget stop in {doc}")
            if outcome.error_code is not None:
                raise Refused(f"new-path translation failed in {doc}: {outcome.error_code}")
            reviews = []
            for index, seg in enumerate(segments_out):
                core = raw[seg["core_start"] : seg["core_end"]]
                reviews.append(
                    await self.review(
                        f"M:{doc}:seg{index}",
                        SemanticReviewJob(
                            source=core,
                            target=seg["translation"],
                            direction="en",
                            explain_language=self.explain_language,
                        ),
                    )
                )
            self.save(
                f"M/{doc}.json",
                {
                    "layer": "M",
                    "doc": doc,
                    "raw": raw,
                    "prepared": prepared,
                    "old": {
                        "text": "\n".join(t for _, t in old_pairs),
                        "chunks": [{"source": c, "translation": t} for c, t in old_pairs],
                        "latency_s": round(old_latency, 3),
                    },
                    "new": {
                        "text": outcome.text,
                        "segments": segments_out,
                        "latency_s": round(new_latency, 3),
                    },
                    "reviews": reviews,
                },
            )

    # -- M' ---------------------------------------------------------------
    async def run_mprime(self, include_supplementary: bool) -> None:
        for experiment in self.inputs.mprime["experiments"]:
            if experiment.get("supplementary") and not include_supplementary:
                continue
            raw = experiment["precedent"]
            precedent = Precedent(
                surface=raw["surface"],
                choice=raw["choice"],
                rendering=raw["rendering"],
                snippet=raw["snippet"],
                segment_index=raw["anchor"]["segment_index"],
                source_start=raw["anchor"]["source_start"],
                source_end=raw["anchor"]["source_end"],
            )
            for target in experiment["targets"]:
                if self.done(f"Mprime/{experiment['experiment']}-P{target['para']}.json"):
                    continue
                out: dict[str, Any] = {
                    "layer": "Mprime",
                    "experiment": experiment["experiment"],
                    "target_para": target["para"],
                    "source": target["text"],
                }
                for condition, precedents in (
                    ("with_precedent", (precedent,)),
                    ("without_precedent", ()),
                ):
                    out[condition] = await self.translate(
                        target["text"],
                        app_layer.TranslationContext(precedents=precedents),
                    )
                self.save(
                    f"Mprime/{experiment['experiment']}-P{target['para']}.json", out
                )


def build_runtime(
    inputs: Inputs, out_dir: Path, *, fake: bool, llm_timeout: float = 45.0
) -> tuple[SentenceTranslator, Any, Path]:
    db_copy = out_dir / "_db" / "terms.db"
    db_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(inputs.run_dir / SNAPSHOT_DB, db_copy)
    transport = httpx.MockTransport(fake_gateway) if fake else None
    translator = SentenceTranslator(
        db_copy,
        llm_timeout_seconds=llm_timeout,
        llm_max_concurrency=1,
        llm_transport=transport,
        en_zh_protocol=True,
    )
    return translator, app_layer.build_term_service(db_copy), db_copy


async def _run_layers(runner: Runner, layers: Sequence[str], include_supplementary: bool) -> None:
    try:
        for layer in layers:
            if layer == "P":
                await runner.run_p()
            elif layer == "E":
                await runner.run_e()
            elif layer == "M":
                await runner.run_m()
            elif layer == "Mprime":
                await runner.run_mprime(include_supplementary)
    finally:
        await runner.translator.aclose()


_GATEWAY_ENV = ("WUWATERM_OPENAI_BASE_URL", "WUWATERM_OPENAI_API_KEY", "WUWATERM_OPENAI_MODEL")


def command_run(args: argparse.Namespace) -> int:
    saved = {key: os.environ.get(key) for key in _GATEWAY_ENV}
    try:
        return _command_run(args)
    finally:
        if args.fake_transport:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _command_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    corpus_dir = Path(args.corpus_dir).resolve()
    real_ledger_path = run_dir / "ledger.json"
    real_ledger_hash = sha256_file(real_ledger_path)
    ledger_data = load_json(real_ledger_path)
    problems = verify_frozen(run_dir, corpus_dir, ledger_data)
    if problems:
        for problem in problems:
            print(f"refused: {problem}", file=sys.stderr)
        return 1
    layers = LAYERS if args.layer == "all" else (args.layer,)
    if args.fake_transport:
        out_dir = Path(args.out_dir).resolve()
        if out_dir == run_dir:
            raise Refused("--out-dir must not be the run directory")
        out_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = out_dir / "ledger.dry-run.json"
        shutil.copyfile(real_ledger_path, ledger_path)
        os.environ["WUWATERM_OPENAI_BASE_URL"] = "http://dry-run.invalid/v1"
        os.environ["WUWATERM_OPENAI_API_KEY"] = "dry-run"
        os.environ["WUWATERM_OPENAI_MODEL"] = "dry-run"
    else:
        if args.env_file:
            load_env_file(Path(args.env_file))
        if not llm_transport.llm_configured():
            raise Refused("no model gateway configured (environment or --env-file)")
        out_dir = (
            Path(args.out_dir).resolve()
            if args.out_dir
            else run_dir / "rounds" / f"round-{args.round}"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = real_ledger_path
    ledger = Ledger(ledger_path)
    fingerprint = config_fingerprint()
    frozen = ledger.data.get("config_freeze")
    if not args.fake_transport and args.round >= 1:
        if not frozen:
            raise Refused("no config_freeze in the ledger: run freeze-config before a formal round")
        if frozen.get("config_sha256") != fingerprint["config_sha256"]:
            raise Refused("prompt or request options differ from the frozen config_sha256")
    from wuwaterm.semantic_review import SEMANTIC_TIMEOUT_ENV, semantic_timeout_seconds

    llm_timeout = float(args.llm_timeout or os.getenv("WUWATERM_LLM_TIMEOUT_SECONDS") or 45.0)
    run_parameters = {
        "translation_timeout_s": llm_timeout,
        "semantic_review_timeout_s": semantic_timeout_seconds(os.getenv(SEMANTIC_TIMEOUT_ENV)),
        "note": args.note or "",
    }
    meta_path = out_dir / "run-meta.json"
    meta = load_json(meta_path) if meta_path.is_file() else {}
    history = list(meta.get("runs", []))
    history.append({"at": now_iso(), "resume": bool(args.resume), **run_parameters})
    write_json_atomic(
        meta_path,
        {
            "round": args.round,
            "reused_units": sorted(set(meta.get("reused_units", [])) | set(args.reused_units or [])),
            "config": fingerprint,
            "runs": history,
        },
    )
    record = ledger.open_record(
        purpose="eval",
        round=args.round,
        is_retry=bool(args.retry),
        reused_samples=args.round > 1 or bool(args.reused_samples),
        layers=list(layers),
        reused_units=list(args.reused_units or []),
        config_sha256=fingerprint["config_sha256"],
        resumed=bool(args.resume),
        run_parameters=run_parameters,
        max_calls=args.max_calls,
        dry_run=bool(args.fake_transport),
        status="running",
    )
    remove_hook = llm_transport.add_pre_send_hook(lambda purpose: ledger.charge(record))
    observed: list[dict[str, Any]] = []
    remove_observer = llm_transport.add_call_observer(
        lambda rec: observed.append(
            {"purpose": rec.purpose, "outcome": rec.outcome, "status": rec.status_code, "elapsed_ms": rec.elapsed_ms}
        )
    )
    inputs = load_inputs(run_dir, corpus_dir)
    translator, service, db_copy = build_runtime(
        inputs, out_dir, fake=bool(args.fake_transport), llm_timeout=llm_timeout
    )
    runner = Runner(inputs, out_dir, ledger, record, translator, service, resume=bool(args.resume))
    status = "complete"
    try:
        asyncio.run(_run_layers(runner, layers, args.include_supplementary))
    except Refused as exc:
        status = "capped by --max-calls" if record.get("cap_reached") else f"stopped: {exc}"
    finally:
        remove_hook()
        remove_observer()
        record["status"] = status
        record["finished_at"] = now_iso()
        ledger.save()
        write_json_atomic(out_dir / "calls.json", observed)
        shutil.rmtree(db_copy.parent, ignore_errors=True)
    after = verify_frozen(run_dir, corpus_dir, load_json(real_ledger_path))
    if after:
        print("frozen inputs changed during the run: " + "; ".join(after), file=sys.stderr)
        return 1
    if args.fake_transport and sha256_file(real_ledger_path) != real_ledger_hash:
        print("dry run touched the real ledger", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": status,
                "layers": list(layers),
                "calls_this_run": record["calls"],
                "ledger_total": ledger.total,
                "ledger": str(ledger_path),
                "out_dir": str(out_dir),
            },
            ensure_ascii=False,
        )
    )
    return 0 if status in ("complete", "capped by --max-calls") else 1


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def overlaps(span: Mapping[str, int] | None, rng: Sequence[int] | None) -> bool:
    """Half-open overlap; a zero-width range p counts when start <= p <= end."""
    if span is None or rng is None:
        return False
    a, b = span["start"], span["end"]
    c, d = rng
    if c == d:
        return a <= c <= b
    return a < d and c < b


def gate(state: str, **details: Any) -> dict[str, Any]:
    return {"state": state, **details}


def bounded_gate(lower: float, upper: float, ok: callable) -> str:
    if ok(upper) and ok(lower):
        return MET
    if not ok(lower) and not ok(upper):
        return NOT_MET
    return PENDING


_CLASS_PRIORITY = ("real_error", "undecidable", "allowed_rewrite")


def classify_issue(
    issue: Mapping[str, Any], items: Iterable[Mapping[str, Any]], *, source_key: str, target_key: str
) -> tuple[str | None, list[str], bool]:
    """(class or None for off-list, item ids hit, multi-class hit)."""
    hits: dict[str, list[str]] = {}
    for item in items:
        if overlaps(issue.get("source_span"), item.get(source_key)) or overlaps(
            issue.get("target_span"), item.get(target_key)
        ):
            hits.setdefault(item["class"], []).append(item["id"])
    if not hits:
        return None, [], False
    for cls in _CLASS_PRIORITY:
        if cls in hits:
            return cls, [i for ids in hits.values() for i in ids], len(hits) > 1
    return None, [], False


def _freeze_items_for_unit(freeze: Mapping[str, Any], unit_id: str, direction: str) -> list[dict[str, Any]]:
    out = []
    for item in freeze["items"]:
        if item["unit_id"] != unit_id:
            continue
        zh = None if item.get("zh") is None else item["zh"]["unit_range"]
        en = None if item.get("en") is None else item["en"]["unit_range"]
        source, target = (zh, en) if direction == "zh->en" else (en, zh)
        out.append({"id": item["id"], "class": item["class"], "source": source, "target": target})
    return out


def _issue_key(prefix: str, issue: Mapping[str, Any]) -> str:
    return f"{prefix}:{issue['id']}"


def _span_is_legal(issue: Mapping[str, Any], source: str, target: str) -> bool:
    for name, side in (("source_span", source), ("target_span", target)):
        span = issue.get(name)
        if span is None:
            continue
        if not (0 <= span["start"] < span["end"] <= len(side)):
            return False
        if side[span["start"] : span["end"]] != span["text"]:
            return False
    return issue.get("source_span") is not None or issue.get("target_span") is not None


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def _load_dir(path: Path) -> list[dict[str, Any]]:
    if not path.is_dir():
        return []
    return [load_json(item) for item in sorted(path.glob("*.json"))]


def _adjudication(out_dir: Path, name: str) -> dict[str, Any]:
    path = out_dir / "adjudication" / name
    return load_json(path) if path.is_file() else {}


@dataclass
class ReviewStats:
    returned: int = 0
    dropped: int = 0
    illegal: int = 0
    not_reviewed: int = 0
    errors: int = 0
    timeouts: int = 0
    max_bytes: int = 0
    latencies: list[float] = field(default_factory=list)
    confidence: dict[str, int] = field(default_factory=dict)

    def add(self, result: Mapping[str, Any], source: str, target: str) -> list[dict[str, Any]]:
        if result.get("error_code"):
            self.errors += 1
            if result.get("timeout"):
                # A timed-out review is a latency sample at least this slow.
                self.timeouts += 1
                self.latencies.append(float(result["latency_s"]))
            return []
        body = result["body"]
        self.latencies.append(float(result["latency_s"]))
        self.max_bytes = max(self.max_bytes, int(result["bytes"] or 0))
        if body["status"] != "complete":
            self.not_reviewed += 1
            return []
        self.dropped += int(body["dropped_unanchored"])
        issues = list(body["issues"])
        self.returned += len(issues) + int(body["omitted_issues"])
        for issue in issues:
            self.confidence[issue["confidence"]] = self.confidence.get(issue["confidence"], 0) + 1
            if not _span_is_legal(issue, source, target):
                self.illegal += 1
        return issues


def score_p(inputs: Inputs, out_dir: Path, stats: ReviewStats) -> dict[str, Any]:
    offlist_verdicts = _adjudication(out_dir, "p-offlist.json")
    denominator = int(inputs.alignment["zh_char_denominator"])
    limit = denominator * P_FP_PER_1000_ZH / 1000
    units = {unit["unit_id"]: unit for unit in inputs.alignment["units"]}
    real_items = {item["id"] for item in inputs.freeze["items"] if item["class"] == "real_error"}
    real_hit: set[str] = set()
    per_direction: dict[str, dict[str, Any]] = {}
    worklist: list[dict[str, Any]] = []
    multi_class = 0
    for result in _load_dir(out_dir / "P"):
        direction = result["direction"]
        unit = units[result["unit_id"]]
        source, target = (
            (unit["zh_text"], unit["en_text"]) if direction == "zh->en" else (unit["en_text"], unit["zh_text"])
        )
        issues = stats.add(result, source, target)
        bucket = per_direction.setdefault(
            direction,
            {"on_allowed_mh": 0, "offlist_mh": 0, "offlist_fp_mh": 0, "offlist_pending_mh": 0, "on_real": 0, "on_undecidable": 0, "issues": 0, "not_reviewed": 0},
        )
        if result.get("body") and result["body"]["status"] != "complete":
            bucket["not_reviewed"] += 1
        items = _freeze_items_for_unit(inputs.freeze, result["unit_id"], direction)
        for issue in issues:
            bucket["issues"] += 1
            cls, hit_ids, multi = classify_issue(issue, items, source_key="source", target_key="target")
            multi_class += int(multi)
            mh = issue["confidence"] in ("medium", "high")
            if cls == "real_error":
                bucket["on_real"] += 1
                real_hit.update(i for i in hit_ids if i in real_items)
            elif cls == "undecidable":
                bucket["on_undecidable"] += 1
            elif cls == "allowed_rewrite":
                bucket["on_allowed_mh"] += int(mh)
                if mh:
                    per_unit = bucket.setdefault("per_unit_on_allowed_mh", {})
                    per_unit[result["unit_id"]] = per_unit.get(result["unit_id"], 0) + 1
            else:
                key = _issue_key(f"P:{result['unit_id']}:{direction}", issue)
                verdict = offlist_verdicts.get(key)
                if mh:
                    bucket["offlist_mh"] += 1
                    if verdict in ("allowed_rewrite", "model_misread"):
                        bucket["offlist_fp_mh"] += 1
                    elif verdict is None:
                        bucket["offlist_pending_mh"] += 1
                if verdict is None:
                    worklist.append(
                        {
                            "key": key,
                            "unit_id": result["unit_id"],
                            "direction": direction,
                            "category": issue["category"],
                            "confidence": issue["confidence"],
                            "source_span": issue["source_span"],
                            "target_span": issue["target_span"],
                            "rationale": issue["rationale"],
                        }
                    )
    gates = {}
    for direction in ("zh->en", "en->zh"):
        bucket = per_direction.get(direction)
        if bucket is None:
            gates[f"P_false_positives_{direction}"] = gate(UNDECIDABLE, reason="not run")
            continue
        lower = bucket["on_allowed_mh"] + bucket["offlist_fp_mh"]
        upper = lower + bucket["offlist_pending_mh"]
        state = bounded_gate(lower, upper, lambda value: value <= limit)
        gates[f"P_false_positives_{direction}"] = gate(
            state,
            numerator_lower=lower,
            numerator_upper=upper,
            limit=round(limit, 3),
            zh_char_denominator=denominator,
            per_1000_lower=round(lower * 1000 / denominator, 3),
            per_1000_upper=round(upper * 1000 / denominator, 3),
            **{k: v for k, v in bucket.items()},
        )
    n_real = len(real_items)
    recall = (len(real_hit) / n_real) if n_real else None
    gates["P_real_error_recall"] = gate(
        UNDECIDABLE if n_real < P_REAL_ERROR_MIN_SAMPLE else (MET if recall >= P_REAL_ERROR_RECALL_MIN else NOT_MET),
        n=n_real,
        min_sample=P_REAL_ERROR_MIN_SAMPLE,
        hit=len(real_hit),
        recall=None if recall is None else round(recall, 3),
    )
    write_json_atomic(out_dir / "worklists" / "p-offlist.json", worklist)
    return {"gates": gates, "multi_class_hits": multi_class, "offlist_pending": len(worklist)}


def score_e(inputs: Inputs, out_dir: Path, stats: ReviewStats) -> dict[str, Any]:
    injections = {item["id"]: item for item in inputs.injections["injections"]}
    variants = {item["variant_id"]: item for item in inputs.variants["variants"]}
    hits: dict[str, bool] = {}
    hits_mh: dict[str, bool] = {}
    category_match: dict[str, bool] = {}
    non_injected = {"on_allowed_mh": 0, "on_undecidable": 0, "offlist": 0, "on_real": 0}
    ran = False
    for result in _load_dir(out_dir / "E"):
        ran = True
        variant = variants[result["variant_id"]]
        issues = stats.add(result, variant["source_text"], variant["target_text"])
        for injection in variant["injections"]:
            iid = injection["injection_id"]
            hits.setdefault(iid, False)
            hits_mh.setdefault(iid, False)
            category_match.setdefault(iid, False)
        p_items = [
            {"id": item["diff_id"], "class": item["class"], "source": item["source_range"], "target": item["target_range"]}
            for item in variant.get("p_items_in_variant_coords", [])
        ]
        for issue in issues:
            matched = False
            for injection in variant["injections"]:
                iid = injection["injection_id"]
                if overlaps(issue.get("source_span"), injection["source_range"]) or overlaps(
                    issue.get("target_span"), injection["target_injected_range"]
                ):
                    matched = True
                    hits[iid] = True
                    if issue["confidence"] in ("medium", "high"):
                        hits_mh[iid] = True
                    if issue["category"] == injection["category"]:
                        category_match[iid] = True
            if not matched:
                cls, _, _ = classify_issue(issue, p_items, source_key="source", target_key="target")
                if cls == "allowed_rewrite":
                    non_injected["on_allowed_mh"] += int(issue["confidence"] in ("medium", "high"))
                elif cls == "undecidable":
                    non_injected["on_undecidable"] += 1
                elif cls == "real_error":
                    non_injected["on_real"] += 1
                else:
                    non_injected["offlist"] += 1
    gates: dict[str, Any] = {}
    for category, minimum in E_RECALL_MIN.items():
        ids = [iid for iid, item in injections.items() if item["category"] == category]
        groups = sorted({injections[iid]["doc"] for iid in ids})
        if not ran:
            gates[f"E_recall_{category}"] = gate(UNDECIDABLE, reason="not run")
            continue
        n = len(ids)
        hit = sum(1 for iid in ids if hits.get(iid))
        recall = hit / n if n else 0.0
        enough = n >= E_MIN_PER_CATEGORY and len(groups) >= E_MIN_GROUPS
        gates[f"E_recall_{category}"] = gate(
            (MET if recall >= minimum else NOT_MET) if enough else UNDECIDABLE,
            n=n,
            groups=len(groups),
            hit=hit,
            recall=round(recall, 3),
            threshold=minimum,
            hit_medium_high=sum(1 for iid in ids if hits_mh.get(iid)),
            category_also_matched=sum(1 for iid in ids if category_match.get(iid)),
        )
    return {"gates": gates, "non_injected_issues": non_injected}


_BLANK_LINE = re.compile(r"\n[^\S\n]*\n")


def _blank_lines(text: str) -> int:
    return len(_BLANK_LINE.findall(text))


def separator_fidelity(source: str, segments: Sequence[Mapping[str, Any]], new_text: str) -> dict[str, Any]:
    """Rebuild with segmentation-v1 from the source as written and compare.

    Gate: the output equals ``Σ (leading_i + T_i + trailing_i)`` over the
    source's own segments, every separator verbatim, no T_i with stray edge
    whitespace. Descriptive only: how many segments kept their internal blank
    lines, which is up to the model.
    """
    pieces = []
    seps_ok = True
    edges_ok = True
    line_agree = 0
    expected = [seg for seg in segment_text(source) if seg.core_start != seg.core_end]
    by_start = {seg["start"]: seg for seg in segments}
    cursor = 0
    for seg in segment_text(source):
        if seg.core_start == seg.core_end:
            pieces.append(source[seg.start : seg.end])
            continue
        got = by_start.get(seg.start)
        if got is None or (got["core_start"], got["core_end"]) != (seg.core_start, seg.core_end):
            seps_ok = False
            continue
        text = got["translation"]
        if text and (text[0] in WHITESPACE or text[-1] in WHITESPACE):
            edges_ok = False
        core = source[seg.core_start : seg.core_end]
        line_agree += int(_blank_lines(core) == _blank_lines(text))
        pieces.append(source[seg.start : seg.core_start] + text + source[seg.core_end : seg.end])
        cursor += 1
    rebuilt = "".join(pieces)
    return {
        "ok": seps_ok and edges_ok and rebuilt == new_text and cursor == len(expected),
        "segments": len(expected),
        "measured_against": "source as written, not the prepared text",
        "internal_blank_lines_agree": line_agree,
    }


def _verified_count(service: Any, source: str, target: str) -> int | None:
    try:
        report = review_pair(service, source, target, "en", review_version="review-v2")
    except Exception:  # noqa: BLE001 - a diagnostic only
        return None
    return sum(1 for finding in report.findings if finding.verdict == VERDICT_VERIFIED)


def score_m(inputs: Inputs, out_dir: Path, stats: ReviewStats, service: Any | None) -> dict[str, Any]:
    precision_verdicts = _adjudication(out_dir, "m-precision.json")
    translation_verdicts = _adjudication(out_dir, "m-translation.json")
    blind_key_path = out_dir / "worklists" / "m-blind-key.json"
    docs = _load_dir(out_dir / "M")
    gates: dict[str, Any] = {}
    if not docs:
        for name in ("M_official_terms", "M_ordinary_sense", "M_reference_negation_number", "M_precision", "separator_fidelity"):
            gates[name] = gate(UNDECIDABLE, reason="not run")
        return {"gates": gates}
    fidelity = {}
    mh_issues: list[dict[str, Any]] = []
    verified = {"old": 0, "new": 0}
    for doc in docs:
        fidelity[doc["doc"]] = separator_fidelity(doc["raw"], doc["new"]["segments"], doc["new"]["text"])
        for index, (seg, review) in enumerate(zip(doc["new"]["segments"], doc["reviews"], strict=False)):
            core = doc["raw"][seg["core_start"] : seg["core_end"]]
            for issue in stats.add(review, core, seg["translation"]):
                if issue["confidence"] in ("medium", "high"):
                    mh_issues.append(
                        {
                            "key": _issue_key(f"M:{doc['doc']}:seg{index}", issue),
                            "doc": doc["doc"],
                            "segment": index,
                            "category": issue["category"],
                            "confidence": issue["confidence"],
                            "source_span": issue["source_span"],
                            "target_span": issue["target_span"],
                            "rationale": issue["rationale"],
                            "suggestion": issue["suggestion"],
                        }
                    )
            if service is not None:
                verified["new"] += _verified_count(service, core, seg["translation"]) or 0
        if service is not None:
            for chunk in doc["old"]["chunks"]:
                verified["old"] += _verified_count(service, chunk["source"], chunk["translation"]) or 0
    gates["separator_fidelity"] = gate(
        MET if all(item["ok"] for item in fidelity.values()) else NOT_MET,
        per_doc=fidelity,
    )
    judged = [item for item in mh_issues if item["key"] in precision_verdicts]
    true_count = sum(1 for item in judged if precision_verdicts[item["key"]] == "true")
    pending = [item for item in mh_issues if item["key"] not in precision_verdicts]
    if len(mh_issues) < M_PRECISION_MIN_SAMPLE:
        state = UNDECIDABLE
    elif pending:
        lower = true_count / len(mh_issues)
        upper = (true_count + len(pending)) / len(mh_issues)
        state = bounded_gate(lower, upper, lambda value: value >= M_PRECISION_MIN)
    else:
        decided = [item for item in judged if precision_verdicts[item["key"]] in ("true", "false")]
        precision = true_count / len(decided) if decided else 0.0
        state = MET if precision >= M_PRECISION_MIN else NOT_MET
    gates["M_precision"] = gate(
        state, n=len(mh_issues), min_sample=M_PRECISION_MIN_SAMPLE, judged=len(judged), true=true_count
    )
    write_json_atomic(out_dir / "worklists" / "m-precision.json", pending)

    # Blind translation judging: A/B assignment per document is random and
    # stored apart from the packet the judge reads.
    if blind_key_path.is_file():
        blind_key = load_json(blind_key_path)
    else:
        rng = random.Random(20261110)
        blind_key = {doc["doc"]: ("old", "new") if rng.random() < 0.5 else ("new", "old") for doc in docs}
        write_json_atomic(blind_key_path, blind_key)
    packet = []
    for doc in docs:
        a_side, b_side = blind_key[doc["doc"]]
        packet.append(
            {
                "doc": doc["doc"],
                "source_as_written": doc["raw"],
                "gold_coordinates": "file_range indexes source_as_written",
                "A": doc[a_side]["text"],
                "B": doc[b_side]["text"],
                "gold": [item for item in inputs.m_gold["items"] if item["doc"] == doc["doc"]],
            }
        )
    write_json_atomic(out_dir / "worklists" / "m-translation-packet.json", packet)
    gates.update(_score_m_translation(inputs, translation_verdicts, blind_key, {doc["doc"] for doc in docs}))
    return {"gates": gates, "verified_constraint_diagnostic": verified}


def _verdict(verdicts: Mapping[str, Any], blind_key: Mapping[str, Sequence[str]], item: Mapping[str, Any], side: str) -> str | None:
    entry = verdicts.get(item["id"])
    if not isinstance(entry, Mapping):
        return None
    a_side, _ = blind_key[item["doc"]]
    letter = "A" if a_side == side else "B"
    return entry.get(letter)


def _score_m_translation(
    inputs: Inputs, verdicts: Mapping[str, Any], blind_key: Mapping[str, Sequence[str]], docs: set[str]
) -> dict[str, Any]:
    items = [item for item in inputs.m_gold["items"] if item["doc"] in docs]
    official = [i for i in items if i["kind"] == "term_sense" and i["gold"] == "official"]
    ordinary = [i for i in items if i["kind"] == "term_sense" and i["gold"] == "ordinary"]
    rnn = [i for i in items if i["kind"] in ("reference", "negation", "quantity") and i["gold"] != "undecidable"]

    def counts(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        out = {"n": len(group), "pending": 0, "old_wrong": 0, "new_wrong": 0, "old_right_new_wrong": 0}
        for item in group:
            old = _verdict(verdicts, blind_key, item, "old")
            new = _verdict(verdicts, blind_key, item, "new")
            if old is None or new is None:
                out["pending"] += 1
                continue
            out["old_wrong"] += int(old == "wrong")
            out["new_wrong"] += int(new == "wrong")
            out["old_right_new_wrong"] += int(old == "correct" and new == "wrong")
        return out

    gates: dict[str, Any] = {}
    c = counts(official)
    if c["n"] < M_MIN_OFFICIAL:
        state = UNDECIDABLE
    elif c["pending"]:
        state = PENDING
    else:
        state = MET if c["new_wrong"] <= c["old_wrong"] and c["old_right_new_wrong"] <= 1 else NOT_MET
    gates["M_official_terms"] = gate(state, min_sample=M_MIN_OFFICIAL, **c)
    c = counts(ordinary)
    if c["n"] < M_MIN_ORDINARY:
        state = UNDECIDABLE
    elif c["pending"]:
        state = PENDING
    else:
        ok = c["new_wrong"] <= 1 or c["new_wrong"] <= c["old_wrong"] * 0.5
        state = MET if ok else NOT_MET
    gates["M_ordinary_sense"] = gate(state, min_sample=M_MIN_ORDINARY, **c)
    c = counts(rnn)
    state = PENDING if c["pending"] else (MET if c["new_wrong"] <= c["old_wrong"] else NOT_MET)
    per_doc = {doc: counts([i for i in rnn if i["doc"] == doc]) for doc in sorted(docs)}
    without_a3 = counts([i for i in rnn if i["doc"] != "A3"])
    gates["M_reference_negation_number"] = gate(state, per_doc=per_doc, without_A3=without_a3, **c)
    return gates


def _count_rendering(text: str, rendering: str | None) -> int:
    if not rendering:
        return 0
    return text.casefold().count(rendering.casefold())


def score_mprime(inputs: Inputs, out_dir: Path) -> dict[str, Any]:
    verdicts = _adjudication(out_dir, "mprime.json")
    results = {(r["experiment"], r["target_para"]): r for r in _load_dir(out_dir / "Mprime")}
    experiments = {e["experiment"]: e for e in inputs.mprime["experiments"]}
    occurrences = [
        o for o in inputs.mprime["occurrences"] if (o["experiment"], o["target_para"]) in results
    ]
    same = [o for o in occurrences if o["relation"] == "same" and not o.get("supplementary")]
    diff = [o for o in occurrences if o["relation"] == "diff" and not o.get("supplementary")]
    gates: dict[str, Any] = {}
    if not results:
        gates["Mprime_same_sense"] = gate(UNDECIDABLE, reason="not run")
        gates["Mprime_led_astray"] = gate(UNDECIDABLE, reason="not run")
        return {"gates": gates, "precedents_default": "off"}
    # Same sense: errors with the precedent not more than without.
    wrong = {"with_precedent": 0, "without_precedent": 0}
    pending = 0
    for occ in same:
        entry = verdicts.get(occ["id"])
        if not isinstance(entry, Mapping):
            pending += 1
            continue
        for condition in wrong:
            wrong[condition] += int(entry.get(condition) in ("wrong", "led_astray"))
    if len(same) < MPRIME_MIN_SAME:
        state_same = UNDECIDABLE
    elif pending:
        state_same = PENDING
    else:
        state_same = MET if wrong["with_precedent"] <= wrong["without_precedent"] else NOT_MET
    gates["Mprime_same_sense"] = gate(
        state_same, n_same=len(same), target_paras=len({(o["experiment"], o["target_para"]) for o in same}),
        min_sample=MPRIME_MIN_SAME, pending=pending, **{f"wrong_{k}": v for k, v in wrong.items()},
    )
    # Led astray: an automatic upper bound per target paragraph; a judged
    # verdict replaces the bound for that occurrence.
    upper = 0
    judged_astray = 0
    unresolved = 0
    for (exp_id, para), result in results.items():
        diff_here = [o for o in diff if (o["experiment"], o["target_para"]) == (exp_id, para)]
        if not diff_here:
            continue
        rendering = experiments[exp_id]["precedent"]["rendering"]
        text = result["with_precedent"].get("text") or ""
        bound = min(len(diff_here), _count_rendering(text, rendering))
        undecided = [o for o in diff_here if not isinstance(verdicts.get(o["id"]), Mapping)]
        judged_astray += sum(
            1 for o in diff_here
            if isinstance(verdicts.get(o["id"]), Mapping) and verdicts[o["id"]].get("with_precedent") == "led_astray"
        )
        if bound and undecided:
            unresolved += min(bound, len(undecided))
        upper += bound
    if len(diff) < MPRIME_MIN_DIFF:
        state_diff = UNDECIDABLE
    elif judged_astray:
        state_diff = NOT_MET
    elif unresolved:
        state_diff = PENDING
    else:
        state_diff = MET
    gates["Mprime_led_astray"] = gate(
        state_diff, n_diff=len(diff), target_paras=len({(o["experiment"], o["target_para"]) for o in diff}),
        min_sample=MPRIME_MIN_DIFF, automatic_upper_bound=upper, judged_led_astray=judged_astray,
        unresolved=unresolved,
    )
    default = "on" if state_same == MET and state_diff == MET else "off"
    packet = [
        {
            "occurrence": o,
            "source": results[(o["experiment"], o["target_para"])]["source"],
            "with_precedent": results[(o["experiment"], o["target_para"])]["with_precedent"].get("text"),
            "without_precedent": results[(o["experiment"], o["target_para"])]["without_precedent"].get("text"),
        }
        for o in occurrences
        if not isinstance(verdicts.get(o["id"]), Mapping)
    ]
    write_json_atomic(out_dir / "worklists" / "mprime.json", packet)
    return {"gates": gates, "precedents_default": default}


def score_round(inputs: Inputs, out_dir: Path, *, ledger: Mapping[str, Any], dry_run: bool) -> dict[str, Any]:
    stats = ReviewStats()
    db_path = inputs.run_dir / SNAPSHOT_DB
    service = app_layer.build_term_service(db_path) if db_path.is_file() else None
    p = score_p(inputs, out_dir, stats)
    e = score_e(inputs, out_dir, stats)
    m = score_m(inputs, out_dir, stats, service)
    mp = score_mprime(inputs, out_dir)
    gates: dict[str, Any] = {}
    for part in (p, e, m, mp):
        gates.update(part["gates"])
    attempted = stats.returned + stats.dropped
    dropped_ratio = stats.dropped / attempted if attempted else 0.0
    gates["anchoring"] = gate(
        MET if stats.illegal == 0 and dropped_ratio <= DROPPED_RATIO_LIMIT else NOT_MET,
        returned=stats.returned, dropped_unanchored=stats.dropped, illegal_spans=stats.illegal,
        dropped_ratio=round(dropped_ratio, 3), limit=DROPPED_RATIO_LIMIT,
    )
    gates["bytes"] = gate(
        MET if stats.max_bytes <= SEMANTIC_RESPONSE_BYTE_BUDGET else NOT_MET,
        max_bytes=stats.max_bytes, budget=SEMANTIC_RESPONSE_BYTE_BUDGET,
    )
    p95 = _percentile(stats.latencies, 0.95)
    gates["latency_p95"] = gate(
        UNDECIDABLE if p95 is None else (MET if p95 <= LATENCY_P95_LIMIT_S else NOT_MET),
        p95_s=p95, limit_s=LATENCY_P95_LIMIT_S, n=len(stats.latencies), timeouts=stats.timeouts,
    )
    gates["G_regression"] = gate(UNDECIDABLE, reason="no frozen G set in the run directory")
    states: dict[str, int] = {}
    for item in gates.values():
        states[item["state"]] = states.get(item["state"], 0) + 1
    return {
        "generated_at": now_iso(),
        "dry_run": dry_run,
        "summary": states,
        "headline": (
            f"{states.get(MET, 0)} gates met, {states.get(NOT_MET, 0)} not met, "
            f"{states.get(UNDECIDABLE, 0)} undecidable, {states.get(PENDING, 0)} pending adjudication"
        ),
        "gates": gates,
        "descriptive": {
            "confidence_buckets": stats.confidence,
            "not_reviewed": stats.not_reviewed,
            "review_errors": stats.errors,
            "p_multi_class_hits": p["multi_class_hits"],
            "e_non_injected_issues": e["non_injected_issues"],
            "verified_constraint": m.get("verified_constraint_diagnostic"),
            "precedents_default": mp["precedents_default"],
        },
        "ledger": {
            "model_calls_total": ledger.get("model_calls_total"),
            "eval_self_limit": EVAL_SELF_LIMIT,
            "stop_line_total": STOP_LINE_TOTAL,
        },
        "conclusion_scope": (
            "Internal acceptance on same-publisher official pairs, injected errors and a few "
            "machine translations of community texts; not a quality claim about independent "
            "human translations."
        ),
    }


def command_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    corpus_dir = Path(args.corpus_dir).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else run_dir / "rounds" / f"round-{args.round}"
    dry_run = bool(args.dry_run)
    ledger_path = out_dir / "ledger.dry-run.json" if dry_run else run_dir / "ledger.json"
    inputs = load_inputs(run_dir, corpus_dir)
    receipt = score_round(inputs, out_dir, ledger=load_json(ledger_path), dry_run=dry_run)
    receipt["round"] = args.round
    meta_path = out_dir / "run-meta.json"
    if meta_path.is_file():
        meta = load_json(meta_path)
        receipt["reused_samples"] = {
            "units": meta.get("reused_units", []),
            "note": "tuned on before this round; their numbers are optimistic and are also shown separately",
        }
        receipt["config_sha256"] = meta["config"]["config_sha256"]
        receipt["run_parameters"] = meta.get("runs", [])
    target = out_dir / "receipt.dry-run.json" if dry_run else run_dir / "receipt.json"
    if not dry_run and target.is_file():
        existing = load_json(target)
        rounds = existing.get("rounds", {}) if isinstance(existing, dict) else {}
    else:
        rounds = {}
    rounds[str(args.round)] = receipt
    write_json_atomic(target, {"run": run_dir.name, "rounds": rounds})
    print(receipt["headline"])
    print(str(target))
    return 0


def command_freeze_config(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.run_dir).resolve() / "ledger.json")
    if ledger.data.get("config_freeze") and not args.replace:
        print("refused: config_freeze already present", file=sys.stderr)
        return 1
    fingerprint = config_fingerprint()
    ledger.data["config_freeze"] = {
        **fingerprint,
        "frozen_at": now_iso(),
        "git_commit": args.git_commit,
        "note": args.note or "",
    }
    ledger.save()
    print(json.dumps({"config_sha256": fingerprint["config_sha256"], **fingerprint["options"]}, ensure_ascii=False))
    return 0


def command_verify(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    problems = verify_frozen(run_dir, Path(args.corpus_dir).resolve(), load_json(run_dir / "ledger.json"))
    for problem in problems:
        print(f"refused: {problem}", file=sys.stderr)
    if not problems:
        print("all frozen inputs match")
    return 1 if problems else 0


def command_ledger(args: argparse.Namespace) -> int:
    ledger = Ledger(Path(args.run_dir).resolve() / "ledger.json")
    if args.ledger_command == "show":
        print(
            json.dumps(
                {
                    "model_calls_total": ledger.total,
                    "eval_calls": ledger.eval_total(),
                    "eval_self_limit": EVAL_SELF_LIMIT,
                    "stop_line_total": STOP_LINE_TOTAL,
                    "owner_approved_max": OWNER_APPROVED_MAX,
                    "records": len(ledger.data["records"]),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.ledger_command == "reserve":
        record = ledger.reserve(args.purpose, args.max_calls, args.note or "")
        print(json.dumps({"reservation_id": record["reservation_id"], "ledger_total": ledger.total}))
        return 0
    record = ledger.settle(args.reservation, args.actual)
    print(json.dumps({"reservation_id": record["reservation_id"], "calls": record["calls"], "ledger_total": ledger.total}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, corpus: bool = True) -> None:
        p.add_argument("--run-dir", required=True, help="frozen evaluation run directory")
        if corpus:
            p.add_argument("--corpus-dir", required=True, help="corpus directory named by the ledger")

    p = sub.add_parser("verify")
    common(p)
    p = sub.add_parser("run")
    common(p)
    p.add_argument("--layer", choices=(*LAYERS, "all"), default="all")
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--out-dir")
    p.add_argument("--env-file", help="KEY=VALUE file with the gateway credentials; read, never written")
    p.add_argument("--fake-transport", action="store_true", help="dry run against an in-process fake gateway")
    p.add_argument("--include-supplementary", action="store_true", help="also run supplementary M' experiments")
    p.add_argument("--retry", action="store_true", help="mark this run as a manual retry in the ledger")
    p.add_argument("--max-calls", type=int, help="stop this run after this many gateway attempts")
    p.add_argument("--resume", action="store_true", help="keep finished outputs of this round; run the rest")
    p.add_argument("--llm-timeout", type=float, help="translation model timeout in seconds (default: env or 45)")
    p.add_argument("--note", help="free text recorded in the ledger and run-meta")
    p.add_argument("--reused-samples", action="store_true")
    p.add_argument("--reused-units", nargs="*", default=[], help="units already used for tuning; marked in the receipt")
    p = sub.add_parser("freeze-config", help="record the prompt/request-option SHA-256 in the ledger")
    common(p, corpus=False)
    p.add_argument("--git-commit", required=True)
    p.add_argument("--note")
    p.add_argument("--replace", action="store_true")
    p = sub.add_parser("score")
    common(p)
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--out-dir")
    p.add_argument("--dry-run", action="store_true", help="score a --fake-transport output directory")
    p = sub.add_parser("ledger")
    common(p, corpus=False)
    lsub = p.add_subparsers(dest="ledger_command", required=True)
    lsub.add_parser("show")
    r = lsub.add_parser("reserve")
    r.add_argument("--purpose", required=True, choices=("w2", "deploy-probe"))
    r.add_argument("--max-calls", type=int, required=True)
    r.add_argument("--note")
    s = lsub.add_parser("settle")
    s.add_argument("--reservation", required=True)
    s.add_argument("--actual", type=int, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "verify":
            return command_verify(args)
        if args.command == "run":
            if args.fake_transport and not args.out_dir:
                print("--fake-transport needs --out-dir", file=sys.stderr)
                return 2
            return command_run(args)
        if args.command == "score":
            return command_score(args)
        if args.command == "freeze-config":
            return command_freeze_config(args)
        return command_ledger(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
