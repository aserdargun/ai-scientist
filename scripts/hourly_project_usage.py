"""Project a frozen usage collector into exactly two public docs under one publisher lock."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re

# Hash-pinned collector, fixed argv, no shell.
import subprocess  # nosec B404
import sys
import tempfile
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import publish_reviewed_snapshot as pub

OUTPUTS = ("docs/usage/project-usage-latest.json", "docs/usage/project-usage-latest.md")
PROJECTION_SCHEMA = "strict-public-project-usage.v1"
INT = ("integer",)
TS = ("timestamp",)
DAY = ("date",)
USD = ("usd",)
TOKENS = {
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
}
MODEL = (
    "identity",
    {"gpt-6-astra", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "unknown", "Qwen/Qwen3.5-9B", "none"},
)
PROVIDER = ("identity", {"openai", "unknown", "cpu-no-model", "local-vllm"})
EFFORT = ("identity", {"low", "medium", "high", "xhigh", "max", "ultra", "unknown"})
GAPS = {
    "partial_final_record_deferred",
    "invalid_complete_log_line",
    "invalid_log_record",
    "future_record_deferred",
    "session_metadata_missing",
    "session_identity_missing",
    "parent_metadata_conflict",
    "conflicting_session_metadata",
    "explicit_root_session_missing",
    "record_timestamp_missing",
    "invalid_token_usage",
    "unproved_session_event_boundary",
    "ambiguous_initial_counter_interval",
    "ambiguous_counter_reset_or_decrease",
    "ambiguous_cumulative_delta_differs_last_usage",
    "ambiguous_model_provider_interval",
    "usage_attribution_incomplete",
    "related_session_without_counted_usage",
    "runtime_receipt_unavailable_or_oversize",
    "unsupported_runtime_receipt_schema",
    "conflicting_runtime_run_receipts",
    "duplicate_runtime_run_receipts_ignored",
    "invalid_runtime_receipt",
}
OBSERVATIONS = {
    "duplicate_log_records_ignored",
    "token_event_without_info",
    "parent_chain_copied_prefix_events_excluded",
    "duplicate_cumulative_events_ignored",
    "measured_usage_events",
}
TOKEN_MAP = ("counters", TOKENS)
GAP_MAP = ("counters", GAPS)
IDENTITY = {"provider": PROVIDER, "model": MODEL, "reasoning_effort": EFFORT}
MODEL_ROW = {**IDENTITY, "tokens": TOKEN_MAP, "missing_metric_events": TOKEN_MAP}
DAILY_ROW = {**MODEL_ROW, "date": DAY, "measured_usage_events": INT}
ESTIMATE = (
    "union",
    [
        {
            "usd": None,
            "reason": {
                "price_reference_not_supplied",
                "exact_model_provider_price_missing",
                "cache_write_or_cache_accounting_not_demonstrated",
                "cache_input_subset_invalid",
            },
        },
        {
            "usd": USD,
            "scenario": "current_standard_short_counterfactual_not_bill",
            "source_url": {
                "https://developers.openai.com/api/docs/pricing",
                "https://developers.openai.com/api/docs/pricing?tab=suite",
            },
        },
    ],
)
SCHEMA = {
    "schema": "project-usage-report.v1",
    "development_codex": {
        "source": "Codex session metadata and observed token_count counters",
        "scope": "project cwd and explicit roots plus descendants; all available goal periods",
        "complete_project_lifetime": False,
        "coverage_limits": [
            "missing_or_archived_sessions_unknown",
            "counter_metrics_are_not_billing",
            "provider_is_reported_metadata",
            "cached_input_and_reasoning_output_not_added_to_total",
        ],
        "first_observed_at": ("nullable", TS),
        "last_observed_at": ("nullable", TS),
        "session_files_seen": INT,
        "project_related_sessions": INT,
        "tokens": TOKEN_MAP,
        "missing_metric_events": TOKEN_MAP,
        "ambiguous_interval_counters_excluded": TOKEN_MAP,
        "ambiguous_counter_scope": (
            "not additive to observed totals; ownership/reset interval unproved"
        ),
        "date_timezone": "UTC",
        "observations": ("counters", OBSERVATIONS),
        "gaps": GAP_MAP,
        "daily_model_provider_effort": ("list", DAILY_ROW),
        "model_provider_effort_totals": ("list", MODEL_ROW),
    },
    "goal_counters": {
        "scope": "separate get_goal observations; not API usage or billing",
        "recorded_goal_period_totals": (
            "nullable",
            {
                "observed_periods": INT,
                "tokens_used": INT,
                "active_seconds": INT,
                "complete_project_lifetime": False,
                "captured_at": ("nullable", TS),
            },
        ),
    },
    "product_runtime": {
        "status": {"selected_receipts_only", "not_collected"},
        "source": "explicit review receipt metadata",
        "complete_project_lifetime": False,
        "selected_runs": INT,
        "rows": (
            "list",
            {
                "date": DAY,
                "provider": PROVIDER,
                "model": MODEL,
                "model_calls": INT,
                "input_tokens": INT,
                "output_tokens": INT,
                "total_tokens": INT,
            },
        ),
        "local_model_tokens": {"input_tokens": INT, "output_tokens": INT, "total_tokens": INT},
        "cloud_api_observed_tokens": ("nullable", INT),
        "cloud_api_scope": "selected local/CPU receipts only; other providers and runs unknown",
        "gaps": GAP_MAP,
        "costs": {"electricity": None, "hardware": None, "reason": "not_measured"},
        "limitations": [
            "older_isolated_databases_and_missing_receipts_unknown",
            "receipt_metadata_not_a_billing_ledger",
            "aggregate_usage_matches_individual_calls_not_added_twice",
        ],
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
        "usd_priced_subset": ("nullable", USD),
        "priced_subset_tokens": TOKEN_MAP,
        "all_observed_tokens": TOKEN_MAP,
        "unpriced_rows": INT,
        "rows": ("list", {**IDENTITY, "date": DAY, "estimate": ESTIMATE}),
        "assumptions": [
            "current_standard_short_rates_applied_counterfactually",
            "historical_rates_and_actual_service_tier_unknown",
            "long_context_threshold_not_verified",
            "cached_input_is_a_subset_of_input",
            "reasoning_output_is_a_subset_of_output",
            "cache_write_zero_reported_required",
        ],
        "price_reference_retrieved_at": ("nullable", TS),
    },
}


def validate(value: Any, spec: Any) -> Any:
    if isinstance(spec, dict):
        pub.require(isinstance(value, dict) and set(value) == set(spec), "usage-object-fields")
        return {key: validate(value[key], item) for key, item in spec.items()}
    if isinstance(spec, set):
        pub.require(isinstance(value, str) and value in spec, "usage-enum")
        return value
    if isinstance(spec, tuple):
        kind = spec[0]
        if kind == "nullable":
            return None if value is None else validate(value, spec[1])
        if kind == "union":
            for choice in spec[1]:
                try:
                    return validate(value, choice)
                except pub.PublicationError:
                    pass
            raise pub.PublicationError("usage-variant")
        if kind == "list":
            pub.require(isinstance(value, list) and len(value) <= 10000, "usage-list")
            return [validate(item, spec[1]) for item in value]
        if kind == "counters":
            pub.require(isinstance(value, dict) and set(value) <= spec[1], "usage-counter-keys")
            return {key: validate(item, INT) for key, item in value.items()}
        if kind == "integer":
            pub.require(type(value) is int and 0 <= value <= 10**18, "usage-counter-value")
        elif kind == "identity":
            pub.require(
                isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value),
                "unsafe-usage-identity",
            )
            return value if value in spec[1] else "unknown"
        elif kind == "date":
            pub.require(
                isinstance(value, str)
                and (value == "unknown" or re.fullmatch(r"20\d\d-\d\d-\d\d", value)),
                "usage-date",
            )
            if value != "unknown":
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    raise pub.PublicationError("usage-date") from None
        elif kind == "timestamp":
            pub.require(isinstance(value, str) and len(value) <= 40, "usage-timestamp")
            try:
                stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                pub.require(
                    stamp.tzinfo is not None and stamp.utcoffset().total_seconds() == 0,
                    "usage-timestamp-zone",
                )
            except ValueError:
                raise pub.PublicationError("usage-timestamp") from None
        elif kind == "usd":
            pub.require(
                isinstance(value, str)
                and len(value) <= 40
                and re.fullmatch(r"\d+(?:\.\d+)?", value),
                "usage-usd",
            )
            try:
                amount = Decimal(value)
                pub.require(amount.is_finite() and 0 <= amount <= 10**15, "usage-usd")
            except InvalidOperation:
                raise pub.PublicationError("usage-usd") from None
        return value
    pub.require(type(value) is type(spec) and value == spec, "usage-fixed-value")
    return value


def regroup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        keys = tuple(row.get(key, "") for key in ("date", "provider", "model", "reasoning_effort"))
        target = groups.setdefault(
            keys,
            {
                k: v
                for k, v in row.items()
                if k not in {"tokens", "missing_metric_events", "measured_usage_events"}
            },
        )
        for field in ("tokens", "missing_metric_events"):
            aggregate = Counter(target.get(field, {}))
            aggregate.update(row[field])
            target[field] = dict(aggregate)
        if "measured_usage_events" in row:
            target["measured_usage_events"] = (
                target.get("measured_usage_events", 0) + row["measured_usage_events"]
            )
    return [groups[key] for key in sorted(groups)]


def project(raw: dict[str, Any]) -> dict[str, Any]:
    pub.require(not pub.SECRETS.search(pub.canonical(raw)), "secret-in-collector-output")
    result = validate(raw, SCHEMA)
    dev = result["development_codex"]
    dev["daily_model_provider_effort"] = regroup(dev["daily_model_provider_effort"])
    dev["model_provider_effort_totals"] = regroup(dev["model_provider_effort_totals"])
    pub.require(
        result["api_price_scenario"]["all_observed_tokens"] == dev["tokens"],
        "usage-scenario-counter-mismatch",
    )
    token_rows = [
        dev["tokens"],
        *[r["tokens"] for r in dev["daily_model_provider_effort"]],
        *[r["tokens"] for r in dev["model_provider_effort_totals"]],
        result["product_runtime"]["local_model_tokens"],
        *result["product_runtime"]["rows"],
    ]
    for tokens in token_rows:
        if {"input_tokens", "output_tokens", "total_tokens"} <= set(tokens):
            pub.require(
                tokens["total_tokens"] == tokens["input_tokens"] + tokens["output_tokens"],
                "usage-token-total-mismatch",
            )
        pub.require(
            tokens.get("cached_input_tokens", 0) <= tokens.get("input_tokens", 0)
            and tokens.get("reasoning_output_tokens", 0) <= tokens.get("output_tokens", 0),
            "usage-token-subset-mismatch",
        )
    return validate(result, SCHEMA)


def markdown(report: dict[str, Any]) -> bytes:
    lines = [
        "# Project usage observations",
        "",
        "Observed counters have incomplete coverage. "
        "Actual billing and subscription costs are unknown.",
        "",
        "| UTC date | Provider | Model | Effort | Input | Cached input | Output | Total |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for row in report["development_codex"]["daily_model_provider_effort"]:
        fields = [row["date"], row["provider"], row["model"], row["reasoning_effort"]]
        fields += [
            row["tokens"].get(k, "unknown")
            for k in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
        ]
        lines.append("| " + " | ".join(map(str, fields)) + " |")
    dev = report["development_codex"]["tokens"]
    runtime = report["product_runtime"]
    goal = report["goal_counters"]["recorded_goal_period_totals"]
    lines += [
        "",
        "Development observed totals: "
        + ", ".join(
            key + "=" + str(dev.get(key, "unknown"))
            for key in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens")
        )
        + ".",
        "Runtime reviewed runs="
        + str(runtime["selected_runs"])
        + "; model calls="
        + str(sum(row["model_calls"] for row in runtime["rows"]))
        + "; local model "
        + ", ".join(key + "=" + str(value) for key, value in runtime["local_model_tokens"].items())
        + ".",
        "Recorded goal observations: "
        + (
            ", ".join(
                key + "=" + str(goal[key])
                for key in ("observed_periods", "tokens_used", "active_seconds")
            )
            if goal
            else "unknown"
        )
        + ".",
    ]
    lines += [
        "",
        "Cached input and reasoning output are subsets, not extra tokens. "
        "Ambiguous intervals are excluded; these counters are not bills.",
        "",
        "Counterfactual Standard short-context priced-subset equivalent: "
        + str(report["api_price_scenario"]["usd_priced_subset"])
        + " USD. "
        "Historical rates, service tier and long-context classification are unverified.",
        "",
        "Product runtime includes only explicitly reviewed local/CPU receipts. "
        "Electricity and hardware costs were not measured.",
        "",
        "Goal counters are separate observations, not API charges.",
        "",
    ]
    return "\n".join(lines).encode()


def meaningful(report: dict[str, Any]) -> bytes:
    clean = json.loads(pub.canonical(report))
    for key in ("first_observed_at", "last_observed_at"):
        clean["development_codex"].pop(key)
    goal = clean["goal_counters"]["recorded_goal_period_totals"]
    if goal is not None:
        goal.pop("captured_at")
    clean["api_price_scenario"].pop("price_reference_retrieved_at")
    return pub.canonical(clean)


def seal(
    queue: Path,
    repo: Path,
    report: dict[str, Any],
    config: dict[str, Any],
    publisher_config: dict[str, Any],
    base: str,
) -> str:
    payload: dict[str, bytes] = {}
    modes: dict[str, str] = {}
    for entry in pub.git(repo, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, blob = meta.decode().split()
        pub.require(kind == "blob" and mode in {"100644", "100755"}, "unsupported-public-tree")
        payload[name.decode()] = pub.git(repo, "cat-file", "blob", blob)
        modes[name.decode()] = mode
    payload[OUTPUTS[0]] = pub.canonical(report) + b"\n"
    payload[OUTPUTS[1]] = markdown(report)
    modes.update({name: "100644" for name in OUTPUTS})
    files = {
        name: {"sha256": pub.digest(raw), "size": len(raw), "mode": modes[name]}
        for name, raw in payload.items()
    }
    pins = publisher_config["usage_pipeline"]
    review = {
        "schema": "public-snapshot-review.v1",
        "reviewer": "authorized-deterministic-usage-pipeline",
        "approved": True,
        "file_map_sha256": pub.digest(pub.canonical(files)),
        "quality_gate_receipt_sha256": config["quality_gate_receipt_sha256"],
        "privacy_scan_receipt_sha256": pub.digest(pub.canonical(report)),
        **pins,
        "publisher_sha256": publisher_config["publisher_sha256"],
        "base_commit": base,
        "output_file_map_sha256": pub.digest(
            pub.canonical({name: files[name] for name in OUTPUTS})
        ),
    }
    manifest = {
        "schema": "reviewed-public-snapshot.v1",
        "expected_remote_head": base,
        "files": files,
        "commit_subject": "Update reviewed project usage observations",
        "review_receipt_sha256": pub.digest(pub.canonical(review)),
    }
    identity = pub.digest(pub.canonical(manifest))
    temporary = Path(tempfile.mkdtemp(prefix=".sealing-", dir=queue))
    (temporary / "files").mkdir(mode=0o700)
    for name, raw in payload.items():
        path = temporary / "files" / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o600)
    for directory, children, _ in os.walk(temporary / "files"):
        Path(directory).chmod(0o700)
        for child in children:
            (Path(directory) / child).chmod(0o700)
    pub.atomic_json(temporary / "manifest.json", manifest)
    pub.atomic_json(temporary / "review.json", review)
    os.replace(temporary, queue / "ready" / identity)
    return identity


def hourly(
    config_path: Path, *, execute: bool = False, test_local_remote: bool = False
) -> dict[str, Any]:
    config = pub.read_json(config_path)
    config_raw_sha = pub.digest(config_path.read_bytes())
    pub.require(config.get("schema") == "hourly-public-usage-config.v1", "hourly-config-schema")
    own_sha = pub.digest(Path(__file__).read_bytes())
    pub.require(config.get("hourly_sha256") == own_sha, "hourly-source-changed")
    collector = Path(config["collector_script"])
    pub.require(
        collector.is_absolute()
        and not collector.is_symlink()
        and pub.digest(collector.read_bytes()) == config["collector_sha256"],
        "collector-source-changed",
    )
    publisher_path = Path(config["publisher_config"])
    publisher_config = pub.read_json(publisher_path)
    publisher_config_raw_sha = pub.digest(publisher_path.read_bytes())
    pins = {
        "collector_sha256": config["collector_sha256"],
        "projector_sha256": own_sha,
        "hourly_config_sha256": pub.digest(pub.canonical(config)),
    }
    pub.require(publisher_config.get("usage_pipeline") == pins, "pipeline-config-changed")
    pub.require(
        isinstance(config.get("quality_gate_receipt_sha256"), str)
        and pub.SHA.fullmatch(config["quality_gate_receipt_sha256"]),
        "missing-hourly-gate",
    )
    pub.require(
        isinstance(config.get("python"), str)
        and Path(config["python"]).is_absolute()
        and Path(config["python"]).is_file(),
        "invalid-python-executable",
    )
    args = config["collector_args"]
    allowed = {
        "--sessions-root",
        "--project-root",
        "--root-thread",
        "--goal-metrics",
        "--prices",
        "--runtime-receipt",
    }
    pub.require(
        isinstance(args, list)
        and len(args) <= 64
        and len(args) % 2 == 0
        and all(isinstance(x, str) for x in args)
        and all(args[i] in allowed for i in range(0, len(args), 2)),
        "collector-arguments",
    )
    price_args = [args[i + 1] for i in range(0, len(args), 2) if args[i] == "--prices"]
    pub.require(len(price_args) <= 1, "multiple-price-inputs")
    if price_args:
        price = Path(price_args[0])
        pub.require(
            price.is_absolute()
            and not price.is_symlink()
            and isinstance(config.get("prices_sha256"), str)
            and pub.SHA.fullmatch(config["prices_sha256"])
            and pub.digest(price.read_bytes()) == config["prices_sha256"],
            "price-reference-changed",
        )
    else:
        pub.require(config.get("prices_sha256") is None, "unused-price-reference-pin")
    audit = Path(config["audit_dir"])
    pub.private_path(audit, directory=True)
    state_path = Path(publisher_config["state_path"])
    pub.private_path(state_path.parent, directory=True)
    lock_path = state_path.with_suffix(".lock")
    if lock_path.exists():
        pub.private_path(lock_path)
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise pub.PublicationError("publisher-already-running") from None
        for _ in range(3):
            first = pub.publish(
                publisher_path,
                dry_run=not execute,
                _lock_handle=lock,
                test_local_remote=test_local_remote,
            )
            if first["status"] == "empty-queue":
                break
            if not execute or first["status"] not in {"published", "unchanged"}:
                return {"phase": "reviewed-snapshot", **first}
        else:
            raise pub.PublicationError("reviewed-queue-not-drained")
        repo, base = pub.check_repo(publisher_config, test_local_remote=test_local_remote)
        stage = Path(tempfile.mkdtemp(prefix="usage-", dir=audit))
        output = stage / "collector.private.json"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        # Private pinned config and collector source.
        result = subprocess.run(  # nosec B603
            [config["python"], str(collector), *args, "--output", str(output)],
            capture_output=True,
            timeout=1800,
            env=env,
            check=False,
        )
        pub.require(result.returncode == 0, "collector-failed-last-good-preserved")
        pub.require(output.is_file() and not output.is_symlink(), "invalid-collector-file")
        output.chmod(0o600)
        report = project(pub.read_json(output))
        pub.require(
            pub.digest(config_path.read_bytes()) == config_raw_sha
            and pub.digest(publisher_path.read_bytes()) == publisher_config_raw_sha
            and pub.digest(Path(__file__).read_bytes()) == own_sha
            and pub.digest(Path(pub.__file__).read_bytes()) == publisher_config["publisher_sha256"]
            and pub.digest(collector.read_bytes()) == config["collector_sha256"],
            "pipeline-changed-during-collection",
        )
        if price_args:
            pub.require(
                pub.digest(Path(price_args[0]).read_bytes()) == config["prices_sha256"],
                "price-reference-changed",
            )
        public = repo / OUTPUTS[0]
        if public.is_file():
            previous = project(json.loads(public.read_bytes()))
            old_dev, new_dev = previous["development_codex"], report["development_codex"]
            pub.require(
                all(new_dev["tokens"].get(k, 0) >= v for k, v in old_dev["tokens"].items())
                and new_dev["project_related_sessions"] >= old_dev["project_related_sessions"],
                "usage-regression-last-good-preserved",
            )
            old_runtime, new_runtime = previous["product_runtime"], report["product_runtime"]
            pub.require(
                new_runtime["selected_runs"] >= old_runtime["selected_runs"]
                and all(
                    new_runtime["local_model_tokens"].get(key, 0) >= value
                    for key, value in old_runtime["local_model_tokens"].items()
                )
                and sum(row["model_calls"] for row in new_runtime["rows"])
                >= sum(row["model_calls"] for row in old_runtime["rows"]),
                "runtime-regression-last-good-preserved",
            )
            old_goal = previous["goal_counters"]["recorded_goal_period_totals"]
            new_goal = report["goal_counters"]["recorded_goal_period_totals"]
            if old_goal is not None:
                pub.require(
                    new_goal is not None
                    and all(
                        new_goal[key] >= old_goal[key]
                        for key in ("observed_periods", "tokens_used", "active_seconds")
                    ),
                    "goal-regression-last-good-preserved",
                )
            if meaningful(previous) == meaningful(report):
                pub.atomic_json(
                    audit / "heartbeat.private.json",
                    {
                        "status": "unchanged",
                        "report_sha256": pub.digest(pub.canonical(report)),
                        "base": base,
                    },
                )
                return {"status": "unchanged", "phase": "usage"}
        if not execute:
            return {
                "status": "ready",
                "phase": "usage",
                "report_sha256": pub.digest(pub.canonical(report)),
            }
        queue = Path(publisher_config["queue"])
        pub.require(not any((queue / "ready").iterdir()), "queue-changed-during-collection")
        seal(queue, repo, report, config, publisher_config, base)
        return {
            "phase": "usage",
            **pub.publish(
                publisher_path,
                dry_run=False,
                _lock_handle=lock,
                test_local_remote=test_local_remote,
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(hourly(args.config, execute=args.execute), sort_keys=True))
        return 0
    except (
        pub.PublicationError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": str(error)
                    if isinstance(error, pub.PublicationError)
                    else type(error).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
