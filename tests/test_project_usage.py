"""Selected usage accounting and publication privacy; no external services or live logs."""

import json
from collections import Counter
from pathlib import Path

import pytest

from scripts import collect_project_usage as collector

PROJECT = Path("/fixture-project")


def event(kind, payload, second):
    return dict(type=kind, timestamp=f"2026-01-01T00:00:{second:02d}+00:00", payload=payload)


def meta(identity="root", parent=None, second=0, cwd=str(PROJECT)):
    return event(
        "session_meta",
        dict(
            id=identity,
            parent_thread_id=parent,
            cwd=cwd,
            model_provider="openai",
            base_instructions="SECRET-PROMPT",
        ),
        second,
    )


def context(second=1, model="gpt-6.1-sol"):
    return event("turn_context", dict(model=model, effort="high", summary="SECRET-TEXT"), second)


def values(n):
    return dict(
        input_tokens=n * 100,
        cached_input_tokens=n * 40,
        cache_write_input_tokens=0,
        output_tokens=n * 20,
        reasoning_output_tokens=n * 5,
        total_tokens=n * 120,
    )


def token(n=1, second=2, last=1):
    return event(
        "event_msg",
        dict(
            type="token_count",
            info=dict(total_token_usage=values(n), last_token_usage=values(last)),
        ),
        second,
    )


def write(root, filename, rows):
    root.mkdir(exist_ok=True)
    path = root / filename
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def collect(tmp_path, rows, other=None):
    write(tmp_path, "root.jsonl", rows)
    if other:
        write(tmp_path, "other.jsonl", other)
    return collector.collect_development(tmp_path, (PROJECT,))


def test_duplicate_cumulative_and_duplicate_session_logs_never_add_twice(tmp_path):
    rows = [meta(), context(), token(), token(second=3), token(2, 4)]
    data = collect(tmp_path, rows, rows)
    assert data["tokens"]["total_tokens"] == 240
    assert data["observations"]["measured_usage_events"] == 2
    assert data["observations"]["duplicate_log_records_ignored"] == 4
    assert data["observations"]["duplicate_cumulative_events_ignored"] == 1
    text = json.dumps(data)
    assert "SECRET-" not in text and str(PROJECT) not in text and "root.jsonl" not in text
    assert data["complete_project_lifetime"] is False


def test_parent_chain_identical_copied_prefix_proves_fork_baseline(tmp_path):
    parent = [meta(), context(), token()]
    child = [
        meta("child", "root", second=10, cwd="/other-project"),
        context(),
        token(),
        context(11),
        token(2, 12),
    ]
    data = collect(tmp_path, parent, child)
    assert data["project_related_sessions"] == 2
    assert data["tokens"]["total_tokens"] == 240
    assert data["observations"]["parent_chain_copied_prefix_events_excluded"] == 1


def test_timestamp_alone_does_not_prove_prefix_and_initial_reset_stay_ambiguous(tmp_path):
    rows = [meta(second=5), context(6), token(4, 7), token(5, 8), token(1, 9), token(2, 10)]
    data = collect(tmp_path, rows)
    assert data["tokens"]["total_tokens"] == 240
    assert data["ambiguous_interval_counters_excluded"]["total_tokens"] == 240
    assert data["gaps"]["ambiguous_initial_counter_interval"] == 1
    assert data["gaps"]["ambiguous_counter_reset_or_decrease"] == 1


def test_unknown_model_is_not_backfilled_and_ambiguous_interval_is_bucketed(tmp_path):
    data = collect(
        tmp_path, [meta(), token(second=1), context(2), context(3, "gpt-6-astra"), token(2, 4)]
    )
    assert all(row["model"] == "unknown" for row in data["daily_model_provider_effort"])
    assert data["gaps"]["usage_attribution_incomplete"] == 2
    assert data["gaps"]["ambiguous_model_provider_interval"] == 1


def test_delta_last_mismatch_is_excluded_not_repaired(tmp_path):
    data = collect(tmp_path, [meta(), context(), token(), token(3, 3, last=1)])
    assert data["tokens"]["total_tokens"] == 120
    assert data["ambiguous_interval_counters_excluded"]["total_tokens"] == 240


def test_model_time_groups_and_no_change_snapshot(tmp_path):
    data = collect(tmp_path, [meta(), context(), token(), context(3, "gpt-6-astra"), token(2, 4)])
    report = collector.build_report(data)
    assert {row["model"] for row in data["daily_model_provider_effort"]} == {
        "gpt-6.1-sol",
        "gpt-6-astra",
    }
    assert report["actual_billing"]["usd"] is None and report["subscription"]["amount"] is None
    path = tmp_path / "report.json"
    assert collector.write_if_changed(path, report)
    assert not collector.write_if_changed(path, report)
    assert "SECRET-" not in collector.render_markdown(report)


def test_decimal_scenario_prices_exact_provider_model_only(tmp_path):
    data = collect(tmp_path, [meta(), context(), token()])
    prices = {
        "retrieved_at": "2026-10-03",
        "models": {
            "gpt-6.1-sol": {
                "provider": "openai",
                "source_url": "https://example.org/pricing",
                "standard_short_per_million": {"input": "2", "cached_input": "0.1", "output": "10"},
            }
        },
    }
    report = collector.build_report(data, prices)
    assert report["api_price_scenario"]["usd_priced_subset"] == "0.000324"
    assert report["api_price_scenario"]["priced_subset_tokens"]["total_tokens"] == 120
    assert report["api_price_scenario"]["actual_bill"] is False
    unknown = data["daily_model_provider_effort"][0] | {"model": "unknown"}
    assert collector.estimate(unknown, prices)["usd"] is None
    missing = data["daily_model_provider_effort"][0] | {
        "missing_metric_events": {"cache_write_input_tokens": 1}
    }
    assert collector.estimate(missing, prices)["usd"] is None


def test_partial_line_deferred_and_captured_append_not_read(tmp_path):
    path = write(tmp_path, "root.jsonl", [meta(), context(), token()])
    captured = path.stat()
    with path.open("a") as handle:
        handle.write(json.dumps(token(2, 3)) + "\n")
    rows = collector.records(path, captured, "2026-02-01T00:00:00+00:00", Counter())
    assert len(rows) == 3
    path.write_text(json.dumps(meta()) + "\n" + json.dumps(context()))
    gaps = Counter()
    assert len(collector.records(path, path.stat(), "2026-02-01T00:00:00+00:00", gaps)) == 1
    assert gaps["partial_final_record_deferred"] == 1


def test_truncate_and_replace_refuse_the_captured_snapshot(tmp_path):
    path = write(tmp_path, "root.jsonl", [meta(), context(), token()])
    captured = path.stat()
    path.write_text("{}\n")
    with pytest.raises(ValueError, match="captured session prefix"):
        collector.records(path, captured, "2026-02-01T00:00:00+00:00", Counter())
    path.unlink()
    write(tmp_path, "root.jsonl", [meta(), context(), token()])
    with pytest.raises(ValueError, match="captured session prefix"):
        collector.records(path, captured, "2026-02-01T00:00:00+00:00", Counter())


def test_mutated_prefix_during_read_refuses_publication(tmp_path, monkeypatch):
    path = write(tmp_path, "root.jsonl", [meta(), context(), token()])
    original = collector.os.lseek

    def mutate(fd, offset, whence):
        raw = path.read_bytes().replace(b"gpt-6.1-sol", b"gpt-6.1-xyz")
        path.write_bytes(raw)
        return original(fd, offset, whence)

    monkeypatch.setattr(collector.os, "lseek", mutate)
    with pytest.raises(ValueError, match="captured session prefix"):
        collector.records(path, path.stat(), "2026-02-01T00:00:00+00:00", Counter())


def test_runtime_aggregate_and_call_details_are_not_summed_twice(tmp_path):
    document = {
        "schema": "mode-six-real-local-research.v1",
        "run_id": "00000000-0000-0000-0000-000000000001",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "model": {"repository": "Qwen/test"},
        "aggregate": {
            "provider_usage": {"prompt_tokens": 100, "completion_tokens": 20, "model_tokens": 120}
        },
        "model_calls": [{"request_id": "call1", "prompt_tokens": 100, "completion_tokens": 20}],
        "private_prompt": "SECRET-RUNTIME",
    }
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(document))
    result = collector.collect_runtime((path, path))
    assert result["local_model_tokens"]["total_tokens"] == 120 and result["selected_runs"] == 1
    assert result["cloud_api_observed_tokens"] == 0
    assert "SECRET-" not in json.dumps(result) and str(path) not in json.dumps(result)
    document["aggregate"]["provider_usage"]["model_tokens"] = 999
    path.write_text(json.dumps(document))
    assert collector.collect_runtime((path,))["gaps"]["invalid_runtime_receipt"] == 1
