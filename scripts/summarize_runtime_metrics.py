#!/usr/bin/env python3
"""Summarize bounded production runtime metrics from API and Telegram logs.

Reads ``request complete``, ``request timed out``, ``llm call``, ``llm strategy``,
``llm translation failed``, ``llm translation diagnostic``, and
``channel_translation`` log lines from file paths or standard input, and prints
success/timeout rates, per-direction call cost multipliers, latency percentiles,
and strategy ROI counters without echoing free-text fields.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any


_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_RATIO_RE = re.compile(r"^(\d+)/(\d+)$")

_PREFIX_MARKERS = (
    ("request_complete", "request complete "),
    ("request_timed_out", "request timed out "),
    ("llm_strategy", "llm strategy "),
    ("llm_call", "llm call "),
    ("llm_translation_failed", "llm translation failed "),
    ("llm_diagnostic", "llm translation diagnostic "),
    ("channel_translation", "channel_translation "),
)


def _safe_token(value: str, fallback: str = "-") -> str:
    return value if _SAFE_TOKEN_RE.fullmatch(value) else fallback


def _parse_kv_tail(tail: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for token in tail.strip().split():
        key, sep, value = token.partition("=")
        if sep == "=" and _SAFE_TOKEN_RE.fullmatch(key):
            parsed[key] = value
    return parsed


def _parse_float(value: str | None) -> float | None:
    if value is None or value == "-":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0.0 else None


def _parse_int(value: str | None) -> int | None:
    if value is None or value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_ratio(value: str | None) -> tuple[int, int]:
    if not value:
        return 0, 0
    match = _RATIO_RE.fullmatch(value)
    if match is None:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _rate(num: int | float, den: int | float) -> float | None:
    if den <= 0:
        return None
    return round(float(num) / float(den), 4)


def _percentile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _latency_stats(values: list[float]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "mean": round(sum(ordered) / n, 3),
        "p50": round(_percentile(ordered, 0.50), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
        "max": round(ordered[-1], 3),
    }


def summarize_lines(lines: list[str] | tuple[str, ...] | Any) -> dict[str, Any]:
    """Aggregate bounded operational metrics from an iterable of log lines."""
    requests_total = 0
    requests_succeeded = 0
    requests_client_error = 0
    requests_server_error = 0
    requests_timed_out = 0
    requests_client_gone = 0
    request_duration_ms: list[float] = []
    request_slot_wait_ms: list[float] = []
    request_llm_post_ms: list[float] = []

    llm_requests = 0
    llm_requests_succeeded = 0
    llm_requests_timed_out = 0
    llm_requests_failed = 0
    llm_requests_calls = 0
    by_direction: dict[str, dict[str, int]] = {}
    timed_out_by_stage: Counter[str] = Counter()

    llm_calls_logged = 0
    llm_call_outcomes: Counter[str] = Counter()
    llm_call_statuses: Counter[str] = Counter()
    llm_call_purposes: Counter[str] = Counter()
    llm_call_elapsed_ms: list[float] = []

    configured_thinking: Counter[str] = Counter()
    initial_thinking: Counter[str] = Counter()
    timeout_fallback_attempts = 0
    timeout_fallback_succeeded = 0
    paragraph_regroup_applied = 0
    paragraph_repair_attempts = 0
    paragraph_repair_succeeded = 0
    html_fallback_attempts = 0
    budget_exhausted_count = 0

    diagnostic_reasons: Counter[str] = Counter()
    diagnostic_details: Counter[str] = Counter()
    channel_events: Counter[str] = Counter()

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        kind: str | None = None
        tail = ""
        for event_kind, marker in _PREFIX_MARKERS:
            pos = line.find(marker)
            if pos != -1:
                kind = event_kind
                tail = line[pos + len(marker) :]
                break
        if kind is None:
            continue
        fields = _parse_kv_tail(tail)

        if kind == "request_complete":
            status = _parse_int(fields.get("status"))
            if status is None:
                continue
            requests_total += 1
            dur = _parse_float(fields.get("duration_ms"))
            if dur is not None:
                request_duration_ms.append(dur)
            is_timed_out = status == 504
            is_succeeded = 200 <= status < 400
            if is_timed_out:
                requests_timed_out += 1
            if is_succeeded:
                requests_succeeded += 1
            elif status == 499:
                requests_client_gone += 1
            elif 400 <= status < 500:
                requests_client_error += 1
            elif 500 <= status < 600:
                requests_server_error += 1

            calls = _parse_int(fields.get("llm_calls"))
            wait = _parse_float(fields.get("slot_wait_ms"))
            post = _parse_float(fields.get("llm_post_ms"))
            if wait is not None:
                request_slot_wait_ms.append(wait)
            if post is not None:
                request_llm_post_ms.append(post)
            if calls is not None:
                llm_requests += 1
                llm_requests_calls += calls
                if is_succeeded:
                    llm_requests_succeeded += 1
                elif is_timed_out:
                    llm_requests_timed_out += 1
                else:
                    llm_requests_failed += 1
                direction = _safe_token(fields.get("direction", "-"))
                bucket = by_direction.setdefault(
                    direction,
                    {"requests": 0, "succeeded": 0, "timed_out": 0, "calls": 0},
                )
                bucket["requests"] += 1
                if is_succeeded:
                    bucket["succeeded"] += 1
                elif is_timed_out:
                    bucket["timed_out"] += 1
                bucket["calls"] += calls

        elif kind == "request_timed_out":
            stage = _safe_token(fields.get("stage", "-"))
            timed_out_by_stage[stage] += 1
            wait = _parse_float(fields.get("slot_wait_ms"))
            post = _parse_float(fields.get("llm_post_ms"))
            elapsed = _parse_float(fields.get("elapsed_ms"))
            if elapsed is not None and fields.get("sent_status", "-") != "-":
                # Record timed-out body close if request complete was omitted.
                request_duration_ms.append(elapsed)
            if wait is not None:
                request_slot_wait_ms.append(wait)
            if post is not None:
                request_llm_post_ms.append(post)

        elif kind == "llm_call":
            llm_calls_logged += 1
            llm_call_outcomes[_safe_token(fields.get("outcome", "-"))] += 1
            llm_call_statuses[_safe_token(fields.get("status", "-"))] += 1
            llm_call_purposes[_safe_token(fields.get("purpose", "-"))] += 1
            elapsed = _parse_float(fields.get("elapsed_ms"))
            if elapsed is not None:
                llm_call_elapsed_ms.append(elapsed)

        elif kind == "llm_strategy":
            cfg = _safe_token(fields.get("configured_thinking", "-"))
            init_mode = _safe_token(fields.get("initial_thinking", "-"))
            if cfg != "-":
                configured_thinking[cfg] += 1
            if init_mode != "-":
                initial_thinking[init_mode] += 1
            fb_ok, fb_att = _parse_ratio(fields.get("timeout_fallback"))
            timeout_fallback_succeeded += fb_ok
            timeout_fallback_attempts += fb_att
            paragraph_regroup_applied += _parse_int(fields.get("paragraph_regroup")) or 0
            rp_ok, rp_att = _parse_ratio(fields.get("paragraph_repair"))
            paragraph_repair_succeeded += rp_ok
            paragraph_repair_attempts += rp_att
            html_fallback_attempts += _parse_int(fields.get("html_fallback")) or 0
            budget_exhausted_count += _parse_int(fields.get("budget_exhausted")) or 0

        elif kind in {"llm_translation_failed", "llm_diagnostic"}:
            diagnostic_reasons[_safe_token(fields.get("reason", "-"))] += 1
            diagnostic_details[_safe_token(fields.get("detail", "-"))] += 1

        elif kind == "channel_translation":
            stage = _safe_token(fields.get("stage", "-"))
            reason = _safe_token(fields.get("reason", "-"))
            channel_events[f"{stage}:{reason}"] += 1

    return {
        "requests": {
            "total": requests_total,
            "succeeded": requests_succeeded,
            "client_error": requests_client_error,
            "server_error": requests_server_error,
            "timed_out": requests_timed_out,
            "client_gone": requests_client_gone,
            "success_rate": _rate(requests_succeeded, requests_total),
            "timeout_rate": _rate(requests_timed_out, requests_total),
            "client_gone_rate": _rate(requests_client_gone, requests_total),
            "timed_out_by_stage": dict(sorted(timed_out_by_stage.items())),
        },
        "llm": {
            "requests": llm_requests,
            "succeeded": llm_requests_succeeded,
            "timed_out": llm_requests_timed_out,
            "failed": llm_requests_failed,
            "success_rate": _rate(llm_requests_succeeded, llm_requests),
            "timeout_rate": _rate(llm_requests_timed_out, llm_requests),
            "calls_total": llm_requests_calls,
            "avg_calls_per_request": _rate(llm_requests_calls, llm_requests),
            "by_direction": {
                direction: {
                    **entry,
                    "avg_calls_per_request": _rate(entry["calls"], entry["requests"]),
                }
                for direction, entry in sorted(by_direction.items())
            },
            "calls_logged": llm_calls_logged,
            "outcomes": dict(sorted(llm_call_outcomes.items())),
            "statuses": dict(sorted(llm_call_statuses.items())),
            "purposes": dict(sorted(llm_call_purposes.items())),
        },
        "latency_ms": {
            "request_elapsed": _latency_stats(request_duration_ms),
            "slot_wait": _latency_stats(request_slot_wait_ms),
            "llm_post": _latency_stats(request_llm_post_ms),
            "llm_call": _latency_stats(llm_call_elapsed_ms),
        },
        "strategy": {
            "configured_thinking": dict(sorted(configured_thinking.items())),
            "initial_thinking": dict(sorted(initial_thinking.items())),
            "timeout_fallback": {
                "attempts": timeout_fallback_attempts,
                "succeeded": timeout_fallback_succeeded,
                "rescue_rate": _rate(
                    timeout_fallback_succeeded, timeout_fallback_attempts
                ),
            },
            "paragraph_regroup_applied": paragraph_regroup_applied,
            "paragraph_repair": {
                "attempts": paragraph_repair_attempts,
                "succeeded": paragraph_repair_succeeded,
                "rescue_rate": _rate(
                    paragraph_repair_succeeded, paragraph_repair_attempts
                ),
            },
            "html_fallback_attempts": html_fallback_attempts,
            "budget_exhausted_count": budget_exhausted_count,
        },
        "diagnostics": {
            "reasons": dict(sorted(diagnostic_reasons.items())),
            "details": dict(sorted(diagnostic_details.items())),
        },
        "channel_events": dict(sorted(channel_events.items())),
    }


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(summary: dict[str, int | float | None]) -> str:
    if not summary["count"]:
        return "n=0"
    return (
        f"n={summary['count']} mean={summary['mean']} "
        f"p50={summary['p50']} p95={summary['p95']} max={summary['max']}"
    )


def format_markdown(summary: dict[str, Any]) -> str:
    """Format a bounded metric summary as concise Markdown tables."""
    req = summary["requests"]
    llm = summary["llm"]
    lat = summary["latency_ms"]
    strat = summary["strategy"]
    lines = [
        "# WuwaTerm Runtime Metrics Summary",
        "",
        "## Requests",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Total | {req['total']} |",
        f"| Success rate | {_fmt_pct(req['success_rate'])} ({req['succeeded']}/{req['total']}) |",
        f"| Timeout rate | {_fmt_pct(req['timeout_rate'])} ({req['timed_out']}/{req['total']}) |",
        f"| Client gone (499) | {_fmt_pct(req['client_gone_rate'])} ({req['client_gone']}/{req['total']}) |",
        f"| Client / Server errors | {req['client_error']} / {req['server_error']} |",
        "",
        "## LLM Execution & Cost",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Model-backed requests | {llm['requests']} (ok={llm['succeeded']}, timed_out={llm['timed_out']}, failed={llm['failed']}) |",
        f"| Calls total / per request | {llm['calls_total']} / {llm['avg_calls_per_request'] if llm['avg_calls_per_request'] is not None else '-'} |",
        f"| Logged POST outcomes | {json.dumps(llm['outcomes'], sort_keys=True) if llm['outcomes'] else '-'} |",
        "",
        "## Latency (ms)",
        "",
        "| Stage | Distribution |",
        "| --- | --- |",
        f"| request_elapsed | {_fmt_ms(lat['request_elapsed'])} |",
        f"| slot_wait | {_fmt_ms(lat['slot_wait'])} |",
        f"| llm_post | {_fmt_ms(lat['llm_post'])} |",
        f"| llm_call | {_fmt_ms(lat['llm_call'])} |",
        "",
        "## Strategy ROI",
        "",
        "| Strategy | Value |",
        "| --- | --- |",
        f"| configured_thinking | {json.dumps(strat['configured_thinking'], sort_keys=True) if strat['configured_thinking'] else '-'} |",
        f"| initial_thinking | {json.dumps(strat['initial_thinking'], sort_keys=True) if strat['initial_thinking'] else '-'} |",
        f"| timeout_fallback | {strat['timeout_fallback']['succeeded']}/{strat['timeout_fallback']['attempts']} ({_fmt_pct(strat['timeout_fallback']['rescue_rate'])}) |",
        f"| paragraph_regroup_applied | {strat['paragraph_regroup_applied']} |",
        f"| paragraph_repair | {strat['paragraph_repair']['succeeded']}/{strat['paragraph_repair']['attempts']} ({_fmt_pct(strat['paragraph_repair']['rescue_rate'])}) |",
        f"| html_fallback_attempts | {strat['html_fallback_attempts']} |",
        f"| budget_exhausted_count | {strat['budget_exhausted_count']} |",
    ]
    if llm["by_direction"]:
        lines.extend(
            [
                "",
                "## By Direction",
                "",
                "| Direction | Requests | Succeeded | Timed Out | Calls | Avg Calls/Req |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for direction, entry in llm["by_direction"].items():
            lines.append(
                f"| {direction} | {entry['requests']} | {entry['succeeded']} | "
                f"{entry['timed_out']} | {entry['calls']} | "
                f"{entry['avg_calls_per_request'] if entry['avg_calls_per_request'] is not None else '-'} |"
            )
    if summary["diagnostics"]["reasons"]:
        lines.extend(
            [
                "",
                "## Diagnostics",
                "",
                f"- Reasons: `{json.dumps(summary['diagnostics']['reasons'], sort_keys=True)}`",
                f"- Details: `{json.dumps(summary['diagnostics']['details'], sort_keys=True)}`",
            ]
        )
    if summary["channel_events"]:
        lines.extend(
            [
                "",
                "## Channel Events",
                "",
                f"- `{json.dumps(summary['channel_events'], sort_keys=True)}`",
            ]
        )
    return "\n".join(lines)


def _iter_input_lines(paths: list[Path]) -> list[str]:
    if not paths:
        return sys.stdin.read().splitlines()
    lines: list[str] = []
    for path in paths:
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "logs",
        nargs="*",
        type=Path,
        help="Log file paths to summarize (reads stdin when omitted).",
    )
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    args = parser.parse_args(argv)
    summary = summarize_lines(_iter_input_lines(args.logs))
    if args.format == "json":
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(format_markdown(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
