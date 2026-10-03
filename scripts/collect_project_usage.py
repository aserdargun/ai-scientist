"""Publish selected usage counters; development, runtime and billing have separate scopes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
REQUIRED = ("input_tokens", "output_tokens", "total_tokens")
MAX_FILES = 2048
MAX_FILE_BYTES = 1024**3
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_LINE_BYTES = 16 * 1024**2
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def name(value: Any) -> str:
    return value if isinstance(value, str) and SAFE_NAME.fullmatch(value) else "unknown"


def timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).isoformat()
    except ValueError:
        return None


def records(
    path: Path, captured: os.stat_result, cutoff: str, gaps: Counter[str]
) -> list[dict[str, Any]]:
    """Read one captured prefix twice, permitting append but rejecting replacement/mutation."""
    selected = []
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != captured.st_dev
            or before.st_ino != captured.st_ino
            or before.st_size < captured.st_size
        ):
            raise ValueError
        first_hash = hashlib.sha256()
        remaining = captured.st_size
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while remaining:
                line = stream.readline(min(MAX_LINE_BYTES + 1, remaining))
                if not line:
                    raise ValueError
                remaining -= len(line)
                first_hash.update(line)
                if len(line) > MAX_LINE_BYTES:
                    raise ValueError
                if not line.endswith(b"\n") and remaining == 0:
                    gaps["partial_final_record_deferred"] += 1
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    gaps["invalid_complete_log_line"] += 1
                    continue
                if not isinstance(row, dict):
                    gaps["invalid_log_record"] += 1
                    continue
                kind, payload = row.get("type"), row.get("payload")
                if kind not in {"session_meta", "turn_context", "event_msg"} or not isinstance(
                    payload, dict
                ):
                    continue
                if kind == "event_msg" and payload.get("type") != "token_count":
                    continue
                stamp = timestamp(row.get("timestamp"))
                if stamp is not None and stamp > cutoff:
                    gaps["future_record_deferred"] += 1
                    continue
                keys = {
                    "session_meta": (
                        "id",
                        "cwd",
                        "parent_thread_id",
                        "model_provider",
                        "timestamp",
                    ),
                    "turn_context": ("model", "model_provider", "effort", "cwd"),
                    "event_msg": ("info",),
                }[kind]
                slim = {key: payload.get(key) for key in keys}
                if kind == "session_meta":
                    source = payload.get("source")
                    nested = (
                        source.get("subagent", {}).get("thread_spawn", {})
                        if isinstance(source, dict) and isinstance(source.get("subagent"), dict)
                        else {}
                    )
                    slim["legacy_parent"] = (
                        nested.get("parent_thread_id") if isinstance(nested, dict) else None
                    )
                selected.append({"type": kind, "timestamp": stamp, "payload": slim})
        os.lseek(descriptor, 0, os.SEEK_SET)
        check_hash = hashlib.sha256()
        remaining = captured.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError
            check_hash.update(chunk)
            remaining -= len(chunk)
        after, named = os.fstat(descriptor), path.stat(follow_symlinks=False)
        if (
            check_hash.digest() != first_hash.digest()
            or after.st_size < captured.st_size
            or (named.st_dev, named.st_ino) != (captured.st_dev, captured.st_ino)
        ):
            raise ValueError
        return selected
    except (OSError, ValueError):
        # Caller refuses publication on any captured-file stability failure.
        raise ValueError("captured session prefix changed or could not be read") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def usage(value: Any) -> dict[str, int | None] | None:
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in FIELDS}
    if any(type(result[key]) is not int or cast(int, result[key]) < 0 for key in REQUIRED):
        return None
    if any(v is not None and (type(v) is not int or v < 0) for v in result.values()):
        return None
    if result["cached_input_tokens"] is not None and cast(
        int, result["cached_input_tokens"]
    ) > cast(int, result["input_tokens"]):
        return None
    if result["reasoning_output_tokens"] is not None and cast(
        int, result["reasoning_output_tokens"]
    ) > cast(int, result["output_tokens"]):
        return None
    if result["total_tokens"] != cast(int, result["input_tokens"]) + cast(
        int, result["output_tokens"]
    ):
        return None
    return result


def project_cwd(value: Any, roots: tuple[Path, ...]) -> bool:
    if not isinstance(value, str):
        return False
    candidate = Path(value)
    return any(candidate == root or root in candidate.parents for root in roots)


def collect_development(
    session_root: Path, project_roots: tuple[Path, ...], root_threads: tuple[str, ...] = ()
) -> dict[str, Any]:
    gaps: Counter[str] = Counter()
    sessions: dict[str, dict[str, Any]] = {}
    files = sorted(session_root.rglob("*.jsonl")) if session_root.is_dir() else []
    if len(files) > MAX_FILES:
        raise ValueError("session file bound exceeded")
    cutoff = datetime.now(UTC).isoformat()
    captures = {}
    for path in files:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > MAX_FILE_BYTES:
            raise ValueError("session snapshot contains an unsupported file")
        captures[path] = info
    if sum(info.st_size for info in captures.values()) > MAX_TOTAL_BYTES:
        raise ValueError("captured session aggregate byte bound exceeded")
    selected_records = {path: records(path, captures[path], cutoff, gaps) for path in files}
    for path in files:
        meta = next((r for r in selected_records[path] if r["type"] == "session_meta"), None)
        if meta is None:
            gaps["session_metadata_missing"] += 1
            continue
        p = meta["payload"]
        identity = p.get("id")
        if not isinstance(identity, str):
            gaps["session_identity_missing"] += 1
            continue
        parent = p.get("parent_thread_id") or p.get("legacy_parent")
        if (
            p.get("parent_thread_id")
            and p.get("legacy_parent")
            and p["parent_thread_id"] != p["legacy_parent"]
        ):
            gaps["parent_metadata_conflict"] += 1
            parent = None
        stable = {
            "parent": parent,
            "provider": name(p.get("model_provider")),
            "started": meta["timestamp"] or timestamp(p.get("timestamp")),
        }
        if identity in sessions and any(sessions[identity][k] != stable[k] for k in stable):
            gaps["conflicting_session_metadata"] += 1
            sessions[identity]["conflicted"] = True
        entry = sessions.setdefault(
            identity, {**stable, "files": [], "project_cwd": False, "conflicted": False}
        )
        entry["files"].append(path)
        entry["project_cwd"] |= project_cwd(p.get("cwd"), project_roots)
        if not entry["project_cwd"]:
            entry["project_cwd"] = any(
                project_cwd(r["payload"].get("cwd"), project_roots)
                for r in selected_records[path]
                if r["type"] == "turn_context"
            )
    related = {identity for identity, meta in sessions.items() if meta["project_cwd"]} | set(
        root_threads
    )
    while True:
        found = {identity for identity, meta in sessions.items() if meta["parent"] in related}
        if found <= related:
            break
        related |= found
    gaps["explicit_root_session_missing"] += len(set(root_threads) - sessions.keys())
    rows: dict[tuple[str, ...], dict[str, Any]] = {}
    total: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    observations: Counter[str] = Counter()
    earliest, latest = None, None
    event_map = {}
    needed = set(related & sessions.keys())
    while True:
        parents = {sessions[i]["parent"] for i in needed if sessions[i]["parent"] in sessions}
        if parents <= needed:
            break
        needed |= parents
    for identity in sorted(needed):
        events = []
        seen = set()
        for path in sessions[identity]["files"]:
            for r in selected_records[path]:
                if r["type"] == "session_meta":
                    continue
                sig = canonical(r)
                if sig in seen:
                    if identity in related:
                        observations["duplicate_log_records_ignored"] += 1
                    continue
                seen.add(sig)
                events.append(r)
        events.sort(key=lambda r: (r["timestamp"] or "", r["type"] != "turn_context"))
        event_map[identity] = events
    ambiguous: Counter[str] = Counter()
    for identity in sorted(related & sessions.keys()):
        meta = sessions[identity]
        if meta["conflicted"]:
            continue
        events = event_map[identity]
        child_tokens = [canonical(r) for r in events if r["type"] == "event_msg"]
        inherited = set()
        visited = {identity}
        parent = meta["parent"]
        while parent in event_map and parent not in visited:
            visited.add(parent)
            parent_tokens = [canonical(r) for r in event_map[parent] if r["type"] == "event_msg"]
            if child_tokens and child_tokens[0] in parent_tokens:
                offset = parent_tokens.index(child_tokens[0])
                for child_token, parent_token in zip(
                    child_tokens, parent_tokens[offset:], strict=False
                ):
                    if child_token != parent_token:
                        break
                    inherited.add(child_token)
            parent = sessions[parent]["parent"]
        current = ("unknown", "unknown", meta["provider"])
        contexts = set()
        previous = None
        counted = False
        for r in events:
            stamp = r["timestamp"]
            if stamp is None:
                gaps["record_timestamp_missing"] += 1
                continue
            if r["type"] == "turn_context":
                if meta["started"] is not None and stamp < meta["started"]:
                    continue
                p = r["payload"]
                current = (
                    name(p.get("model")),
                    name(p.get("effort")),
                    name(p.get("model_provider"))
                    if p.get("model_provider") is not None
                    else meta["provider"],
                )
                contexts.add(current)
                continue
            info = r["payload"].get("info")
            if not isinstance(info, dict):
                observations["token_event_without_info"] += 1
                continue
            cumulative, last = (
                usage(info.get("total_token_usage")),
                usage(info.get("last_token_usage")),
            )
            if cumulative is None or last is None:
                gaps["invalid_token_usage"] += 1
                continue
            if canonical(r) in inherited:
                previous = cumulative
                observations["parent_chain_copied_prefix_events_excluded"] += 1
                continue
            uncertain = meta["started"] is None or stamp < meta["started"]
            if uncertain:
                gaps["unproved_session_event_boundary"] += 1
            if previous is not None and cumulative == previous:
                observations["duplicate_cumulative_events_ignored"] += 1
                continue
            if previous is None:
                delta = last
                if cumulative != last:
                    gaps["ambiguous_initial_counter_interval"] += 1
                    uncertain = True
            else:
                delta = {
                    key: cast(int, cumulative[key]) - cast(int, previous[key])
                    if cumulative[key] is not None and previous[key] is not None
                    else None
                    for key in FIELDS
                }
                if any(v is not None and v < 0 for v in delta.values()):
                    gaps["ambiguous_counter_reset_or_decrease"] += 1
                    delta = last
                    uncertain = True
                elif any(
                    delta[k] != last[k]
                    for k in FIELDS
                    if delta[k] is not None and last[k] is not None
                ):
                    gaps["ambiguous_cumulative_delta_differs_last_usage"] += 1
                    uncertain = True
            previous = cumulative
            if uncertain:
                ambiguous.update(
                    {key: value for key, value in delta.items() if value is not None and value >= 0}
                )
                contexts.clear()
                continue
            if not any(delta[k] for k in REQUIRED):
                continue
            model, effort, provider = current
            if len(contexts) > 1:
                model, effort, provider = "unknown", "unknown", "unknown"
                gaps["ambiguous_model_provider_interval"] += 1
            if "unknown" in (model, effort, provider):
                gaps["usage_attribution_incomplete"] += 1
            contexts.clear()
            key = (stamp[:10], provider, model, effort)
            row = rows.setdefault(
                key,
                dict(
                    date=key[0],
                    provider=provider,
                    model=model,
                    reasoning_effort=effort,
                    measured_usage_events=0,
                    tokens=Counter(),
                    missing_metric_events=Counter(),
                ),
            )
            row["measured_usage_events"] += 1
            for field, value in delta.items():
                if value is None:
                    row["missing_metric_events"][field] += 1
                    missing[field] += 1
                else:
                    row["tokens"][field] += value
                    total[field] += value
            counted = True
            observations["measured_usage_events"] += 1
            earliest = stamp if earliest is None else min(earliest, stamp)
            latest = stamp if latest is None else max(latest, stamp)
        if not counted:
            gaps["related_session_without_counted_usage"] += 1
    return {
        "source": "Codex session metadata and observed token_count counters",
        "scope": "project cwd and explicit roots plus descendants; all available goal periods",
        "complete_project_lifetime": False,
        "coverage_limits": [
            "missing_or_archived_sessions_unknown",
            "counter_metrics_are_not_billing",
            "provider_is_reported_metadata",
            "cached_input_and_reasoning_output_not_added_to_total",
        ],
        "first_observed_at": earliest,
        "last_observed_at": latest,
        "session_files_seen": len(files),
        "project_related_sessions": len(related & sessions.keys()),
        "tokens": dict(total),
        "missing_metric_events": dict(missing),
        "ambiguous_interval_counters_excluded": dict(ambiguous),
        "ambiguous_counter_scope": (
            "not additive to observed totals; ownership/reset interval unproved"
        ),
        "date_timezone": "UTC",
        "observations": dict(observations),
        "gaps": dict(sorted((k, v) for k, v in gaps.items() if v)),
        "daily_model_provider_effort": [
            dict(
                r, tokens=dict(r["tokens"]), missing_metric_events=dict(r["missing_metric_events"])
            )
            for _, r in sorted(rows.items())
        ],
    }


def estimate(row: Mapping[str, Any], prices: Mapping[str, Any]) -> dict[str, Any]:
    entry = prices.get("models", {}).get(row["model"])
    tokens = row["tokens"]
    if entry is None or entry.get("provider") != row["provider"]:
        return {"usd": None, "reason": "exact_model_provider_price_missing"}
    if any(
        row["missing_metric_events"].get(k)
        for k in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "cache_write_input_tokens",
        )
    ) or tokens.get("cache_write_input_tokens", 0):
        return {"usd": None, "reason": "cache_write_or_cache_accounting_not_demonstrated"}
    values = entry["standard_short_per_million"]
    rates = {key: Decimal(values[key]) for key in ("input", "cached_input", "output")}
    if any(not value.is_finite() or value < 0 for value in rates.values()):
        raise ValueError("invalid decimal rate reference")
    cached = tokens.get("cached_input_tokens", 0)
    uncached = tokens.get("input_tokens", 0) - cached
    if uncached < 0:
        return {"usd": None, "reason": "cache_input_subset_invalid"}
    amount = (
        Decimal(uncached) * rates["input"]
        + Decimal(cached) * rates["cached_input"]
        + Decimal(tokens.get("output_tokens", 0)) * rates["output"]
    ) / Decimal(1_000_000)
    return {
        "usd": format(amount, "f"),
        "source_url": entry["source_url"],
        "scenario": "current_standard_short_counterfactual_not_bill",
    }


def build_report(
    development: dict[str, Any],
    prices: dict[str, Any] | None = None,
    goal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    priced_tokens: Counter[str] = Counter()
    estimates = []
    amount = Decimal(0)
    for row in development["daily_model_provider_effort"]:
        cost = (
            estimate(row, prices)
            if prices
            else {"usd": None, "reason": "price_reference_not_supplied"}
        )
        if cost["usd"] is not None:
            amount += Decimal(cost["usd"])
            priced_tokens.update(row["tokens"])
        estimates.append(
            {key: row[key] for key in ("date", "model", "provider", "reasoning_effort")}
            | {"estimate": cost}
        )
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in development["daily_model_provider_effort"]:
        key = (row["provider"], row["model"], row["reasoning_effort"])
        group = grouped.setdefault(
            key,
            dict(
                provider=key[0],
                model=key[1],
                reasoning_effort=key[2],
                tokens=Counter(),
                missing_metric_events=Counter(),
            ),
        )
        group["tokens"].update(row["tokens"])
        group["missing_metric_events"].update(row["missing_metric_events"])
    development = dict(
        development,
        model_provider_effort_totals=[
            dict(
                row,
                tokens=dict(row["tokens"]),
                missing_metric_events=dict(row["missing_metric_events"]),
            )
            for _, row in sorted(grouped.items())
        ],
    )
    goal_periods = goal.get("recorded_goal_period_totals") if goal else None
    goal_summary = None
    if isinstance(goal_periods, dict):
        selected = ("observed_periods", "tokens_used", "active_seconds")
        if any(type(goal_periods.get(key)) is not int or goal_periods[key] < 0 for key in selected):
            raise ValueError("invalid goal period counter observations")
        goal_summary = {key: goal_periods[key] for key in selected}
        goal_summary["complete_project_lifetime"] = False
        goal_summary["captured_at"] = timestamp(goal.get("captured_at") if goal else None)
    return {
        "schema": "project-usage-report.v1",
        "development_codex": development,
        "goal_counters": {
            "scope": "separate get_goal observations; not API usage or billing",
            "recorded_goal_period_totals": goal_summary,
        },
        "product_runtime": {
            "status": "not_collected",
            "tokens": None,
            "reason": "explicit_verified_receipt_list_not_supplied",
        },
        "actual_billing": {"usd": None, "status": "provider_billing_ledger_not_available"},
        "subscription": {
            "amount": None,
            "status": "subscription_invoice_not_available",
            "separate_from_api_billing": True,
        },
        "api_price_scenario": {
            "currency": "USD",
            "actual_bill": False,
            "usd_priced_subset": format(amount, "f") if priced_tokens else None,
            "priced_subset_tokens": dict(priced_tokens),
            "all_observed_tokens": development["tokens"],
            "unpriced_rows": sum(r["estimate"]["usd"] is None for r in estimates),
            "rows": estimates,
            "assumptions": [
                "current_standard_short_rates_applied_counterfactually",
                "historical_rates_and_actual_service_tier_unknown",
                "long_context_threshold_not_verified",
                "cached_input_is_a_subset_of_input",
                "reasoning_output_is_a_subset_of_output",
                "cache_write_zero_reported_required",
            ],
            "price_reference_retrieved_at": prices.get("retrieved_at") if prices else None,
        },
    }


def collect_runtime(paths: tuple[Path, ...]) -> dict[str, Any]:
    """Selected review receipts only; aggregate and individual calls are never added twice."""
    if len(paths) > 64:
        raise ValueError("runtime receipt count bound exceeded")
    selected: dict[str, dict[str, Any]] = {}
    gaps: Counter[str] = Counter()
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024**2:
            gaps["runtime_receipt_unavailable_or_oversize"] += 1
            continue
        try:
            document = json.loads(path.read_bytes())
            schema = document.get("schema")
            run = document.get("run_id") or document.get("run", {}).get("run_id")
            if not isinstance(run, str) or not re.fullmatch(
                r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", run
            ):
                raise ValueError
            observed = timestamp(document.get("recorded_at") or document.get("observed_at"))
            if schema == "mode-six-real-local-research.v1":
                model = name(document["model"]["repository"])
                measured = document["aggregate"]["provider_usage"]
                calls = document["model_calls"]
                if not isinstance(calls, list) or not 1 <= len(calls) <= 64:
                    raise ValueError
                pairs = [(call["prompt_tokens"], call["completion_tokens"]) for call in calls]
                if any(type(value) is not int or value < 0 for pair in pairs for value in pair):
                    raise ValueError
                if len({call["request_id"] for call in calls}) != len(calls):
                    raise ValueError
                inputs, outputs = sum(pair[0] for pair in pairs), sum(pair[1] for pair in pairs)
                if (inputs, outputs, inputs + outputs) != (
                    measured["prompt_tokens"],
                    measured["completion_tokens"],
                    measured["model_tokens"],
                ):
                    raise ValueError
                row = dict(
                    date=observed[:10] if observed else "unknown",
                    provider="local-vllm",
                    model=model,
                    model_calls=len(calls),
                    input_tokens=inputs,
                    output_tokens=outputs,
                    total_tokens=inputs + outputs,
                )
            elif (
                isinstance(schema, str)
                and re.fullmatch(r"field-lab-[a-z-]+-delivery\.v1", schema)
                and type(document.get("runtime", {}).get("model_calls")) is int
                and document["runtime"]["model_calls"] == 0
                and document["runtime"].get("gpu_jobs") == 0
            ):
                row = dict(
                    date=observed[:10] if observed else "unknown",
                    provider="cpu-no-model",
                    model="none",
                    model_calls=0,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                )
            else:
                gaps["unsupported_runtime_receipt_schema"] += 1
                continue
            if run in selected:
                if selected[run] != row:
                    gaps["conflicting_runtime_run_receipts"] += 1
                    selected[run] = {"conflicted": True}
                else:
                    gaps["duplicate_runtime_run_receipts_ignored"] += 1
            else:
                selected[run] = row
        except (KeyError, TypeError, ValueError, OSError):
            gaps["invalid_runtime_receipt"] += 1
    rows = [row for _, row in sorted(selected.items()) if not row.get("conflicted")]
    return {
        "status": "selected_receipts_only" if paths else "not_collected",
        "source": "explicit review receipt metadata",
        "complete_project_lifetime": False,
        "selected_runs": len(rows),
        "rows": rows,
        "local_model_tokens": {
            key: sum(row[key] for row in rows if row["provider"] == "local-vllm")
            for key in ("input_tokens", "output_tokens", "total_tokens")
        },
        "cloud_api_observed_tokens": 0 if rows else None,
        "cloud_api_scope": "selected local/CPU receipts only; other providers and runs unknown",
        "gaps": dict(gaps),
        "costs": {"electricity": None, "hardware": None, "reason": "not_measured"},
        "limitations": [
            "older_isolated_databases_and_missing_receipts_unknown",
            "receipt_metadata_not_a_billing_ledger",
            "aggregate_usage_matches_individual_calls_not_added_twice",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Small public summary, containing no raw log identifiers, text or private paths."""
    development, scenario = report["development_codex"], report["api_price_scenario"]
    lines = [
        "# Project usage observations",
        "",
        "Observed local records are incomplete project coverage. "
        "Actual billing and subscription costs are unknown.",
        "",
        "## Codex development token counters",
        "",
        f"Observed interval: {development['first_observed_at']} through "
        f"{development['last_observed_at']}; "
        f"{development['project_related_sessions']} related sessions.",
        f"Observed total: {development['tokens'].get('total_tokens', 0):,} tokens "
        "across the available records; this is not a complete lifetime total.",
        "",
        "| UTC date | Reported provider | Model | Effort | Input | Cached input | Output | Total |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in development["daily_model_provider_effort"]:
        tokens = row["tokens"]
        lines.append(
            "| "
            + " | ".join(
                str(value)
                for value in (
                    row["date"],
                    row["provider"],
                    row["model"],
                    row["reasoning_effort"],
                    tokens.get("input_tokens", 0),
                    tokens.get("cached_input_tokens", "unknown"),
                    tokens.get("output_tokens", 0),
                    tokens.get("total_tokens", 0),
                )
            )
            + " |"
        )
    lines += [
        "",
        "Cached input and reasoning output are subsets, not extra tokens. "
        "Ambiguous reset/fork intervals are excluded; model/provider attribution can be unknown.",
        "",
        "## Counterfactual API price scenario",
        "",
        f"Current Standard short-context priced-subset equivalent: "
        f"{scenario['usd_priced_subset']} USD. This is not an actual bill; "
        "historical prices, service tier and long-context classification are unverified.",
        f"Unpriced daily model/effort rows: {scenario['unpriced_rows']}.",
        "",
        "## Product runtime",
        "",
        "Only explicit local/CPU review receipts are included; this is not lifetime product usage. "
        "Electricity and hardware costs were not measured.",
        f"Selected runs: {report['product_runtime'].get('selected_runs', 0)}; "
        f"local model tokens: "
        f"{report['product_runtime'].get('local_model_tokens', {}).get('total_tokens', 0):,}. "
        "Cloud API usage outside these selected receipts is unknown.",
        "",
        "Goal counters remain a separate measurement and are not API tokens or charges.",
        "",
    ]
    return "\n".join(lines)


def write_if_changed(path: Path, data: dict[str, Any]) -> bool:
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode()
        + b"\n"
    )
    if path.exists() and path.read_bytes() == payload:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--project-root", type=Path, action="append", required=True)
    parser.add_argument("--root-thread", action="append", default=[])
    parser.add_argument("--goal-metrics", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--runtime-receipt", type=Path, action="append", default=[])
    args = parser.parse_args()
    goal = json.loads(args.goal_metrics.read_bytes()) if args.goal_metrics else None
    threads = args.root_thread + (
        [goal["goal_thread_id"]] if goal and isinstance(goal.get("goal_thread_id"), str) else []
    )
    development = collect_development(
        args.sessions_root, tuple(p.absolute() for p in args.project_root), tuple(threads)
    )
    prices = json.loads(args.prices.read_bytes()) if args.prices else None
    report = build_report(development, prices, goal)
    report["product_runtime"] = collect_runtime(tuple(args.runtime_receipt))
    changed = write_if_changed(args.output, report)
    if args.markdown:
        text = render_markdown(report)
        if not args.markdown.exists() or args.markdown.read_text() != text:
            args.markdown.parent.mkdir(parents=True, exist_ok=True)
            args.markdown.write_text(text)
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "changed": changed,
                "related_sessions": development["project_related_sessions"],
                "complete_project_lifetime": False,
                "actual_billing_known": False,
                "report_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
