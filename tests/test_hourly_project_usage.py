"""Closed aggregate projection and hourly publication using only local test remotes."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import test_reviewed_publication as shared
from test_reviewed_publication import run, seal, write_private

base_setup = shared.setup
MARKED_README = (
    b"initial reviewed public source\n"
    b"<!-- api-cost-summary:start -->\nold summary\n<!-- api-cost-summary:end -->\n"
)


@pytest.fixture
def setup(base_setup):
    repo = base_setup[2]
    (repo / "README.md").write_bytes(MARKED_README)
    run(repo, "add", "README.md")
    run(repo, "commit", "-m", "Review cost block boundary")
    run(repo, "push", "origin", "main")
    return base_setup


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hourly_project_usage as hourly  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "usage_collector", SCRIPTS / "collect_project_usage.py"
)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def report(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    development = collector.collect_development(sessions, (tmp_path / "project",), ())
    result = collector.build_report(development)
    result["product_runtime"] = collector.collect_runtime(())
    return result


def configure(setup, tmp_path, result):
    config_path, public_config, _, _ = setup
    private = config_path.parent
    fake = private / "frozen_collector.py"
    fake.write_text(
        "import argparse,json\np=argparse.ArgumentParser()\n"
        "p.add_argument('--output')\na=p.parse_args()\n"
        "open(a.output,'w').write(" + repr(json.dumps(result)) + ")\n"
    )
    audit = private / "audit"
    audit.mkdir(mode=0o700)
    hourly_path = private / "hourly.json"
    config = {
        "schema": "hourly-public-usage-config.v1",
        "publisher_config": str(config_path),
        "collector_script": str(fake),
        "collector_sha256": hourly.pub.digest(fake.read_bytes()),
        "hourly_sha256": hourly.pub.digest(Path(hourly.__file__).read_bytes()),
        "python": sys.executable,
        "collector_args": [],
        "audit_dir": str(audit),
        "quality_gate_receipt_sha256": "a" * 64,
    }
    write_private(hourly_path, config)
    public_config["usage_pipeline"] = {
        "collector_sha256": config["collector_sha256"],
        "projector_sha256": config["hourly_sha256"],
        "hourly_config_sha256": hourly.pub.digest(hourly.pub.canonical(config)),
    }
    write_private(config_path, public_config)
    return hourly_path, config, fake


def test_projection_matches_closed_collector_contract(tmp_path):
    result = report(tmp_path)
    projected = hourly.project(result)
    assert projected == result
    assert b"Actual billing" in hourly.markdown(projected)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "nested-extra",
        "nan",
        "billing",
        "modeltext",
        "dynamic-gap",
        "negative",
        "token-subset",
    ],
)
def test_raw_or_malformed_data_rejected(tmp_path, mutation):
    result = report(tmp_path)
    if mutation == "extra":
        result["raw_session"] = "private"
    elif mutation == "nested-extra":
        result["actual_billing"]["api_key"] = "private"
    elif mutation == "nan":
        result["development_codex"]["tokens"]["total_tokens"] = float("nan")
    elif mutation == "billing":
        result["actual_billing"]["usd"] = "123"
    elif mutation == "modeltext":
        result["product_runtime"]["rows"] = [
            {
                "date": "2026-10-03",
                "provider": "local-vllm",
                "model": "private prompt and paths",
                "model_calls": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            }
        ]
    elif mutation == "dynamic-gap":
        result["development_codex"]["gaps"]["/private/path"] = 1
    elif mutation == "negative":
        result["development_codex"]["project_related_sessions"] = -1
    else:
        result["development_codex"]["tokens"] = {"input_tokens": 1, "cached_input_tokens": 2}
        result["api_price_scenario"]["all_observed_tokens"] = result["development_codex"]["tokens"]
    with pytest.raises((hourly.pub.PublicationError, ValueError)):
        hourly.project(result)


def test_three_outputs_published_together_then_stable_noop(setup, tmp_path):
    initial = (setup[2] / "README.md").read_bytes()
    config_path, _, _ = configure(setup, tmp_path, report(tmp_path))
    assert hourly.hourly(config_path, execute=False, test_local_remote=True)["status"] == "ready"
    first = hourly.hourly(config_path, execute=True, test_local_remote=True)
    assert first["status"] == "published"
    assert (setup[2] / "README.md").read_bytes() == hourly.pub.update_readme_cost(
        initial, json.loads((setup[2] / hourly.OUTPUTS[0]).read_bytes())
    )
    assert set(run(setup[2], "ls-files").splitlines()) == {"README.md", *hourly.OUTPUTS}
    second = hourly.hourly(config_path, execute=True, test_local_remote=True)
    assert second["status"] == "unchanged"
    assert run(setup[2], "rev-list", "--count", "HEAD") == "3"


def test_collector_failure_keeps_last_good(setup, tmp_path):
    path, config, fake = configure(setup, tmp_path, report(tmp_path))
    fake.write_text("raise RuntimeError('private error text must not escape')\n")
    config["collector_sha256"] = hourly.pub.digest(fake.read_bytes())
    write_private(path, config)
    public_config = setup[1]
    public_config["usage_pipeline"]["collector_sha256"] = config["collector_sha256"]
    public_config["usage_pipeline"]["hourly_config_sha256"] = hourly.pub.digest(
        hourly.pub.canonical(config)
    )
    write_private(setup[0], public_config)
    with pytest.raises(hourly.pub.PublicationError, match="collector-failed-last-good-preserved"):
        hourly.hourly(path, execute=True, test_local_remote=True)
    assert run(setup[2], "status", "--porcelain") == ""
    assert not list((Path(public_config["queue"]) / "ready").iterdir())


def test_source_pin_changed_and_generated_code_overlay_reject(setup, tmp_path):
    path, _, fake = configure(setup, tmp_path, report(tmp_path))
    fake.write_text("unexpected source change")
    with pytest.raises(hourly.pub.PublicationError, match="collector-source-changed"):
        hourly.hourly(path, execute=True, test_local_remote=True)


def test_timestamp_only_changes_have_same_meaning(tmp_path):
    first = report(tmp_path)
    second = copy.deepcopy(first)
    second["development_codex"]["first_observed_at"] = "2026-10-03T12:00:00+00:00"
    second["development_codex"]["last_observed_at"] = "2026-10-03T13:00:00+00:00"
    second["api_price_scenario"]["price_reference_retrieved_at"] = "2026-10-03T14:00:00+00:00"
    assert hourly.meaningful(hourly.project(first)) == hourly.meaningful(hourly.project(second))


def test_root_reviewed_snapshot_precedes_generated_docs(setup, tmp_path):
    path, _, _ = configure(setup, tmp_path, report(tmp_path))
    updated = MARKED_README.replace(
        b"initial reviewed public source", b"root reviewed release source"
    )
    seal(setup, {"README.md": updated})
    result = hourly.hourly(path, execute=True, test_local_remote=True)
    assert result["status"] == "published"
    assert (setup[2] / "README.md").read_bytes() == hourly.pub.update_readme_cost(
        updated, json.loads((setup[2] / hourly.OUTPUTS[0]).read_bytes())
    )
    assert run(setup[2], "rev-list", "--count", "HEAD") == "4"


def test_generated_overlay_cannot_change_other_source(setup, tmp_path):
    config_path, config, _ = configure(setup, tmp_path, report(tmp_path))
    public_config = setup[1]
    queue = Path(public_config["queue"])
    identity = hourly.seal(
        queue, setup[2], report(tmp_path), config, public_config, run(setup[2], "rev-parse", "HEAD")
    )
    root = queue / "ready" / identity
    manifest = json.loads((root / "manifest.json").read_bytes())
    review = json.loads((root / "review.json").read_bytes())
    raw = (
        (root / "files/README.md")
        .read_bytes()
        .replace(b"initial reviewed public source", b"unreviewed code modification")
    )
    (root / "files/README.md").write_bytes(raw)
    manifest["files"]["README.md"]["sha256"] = hourly.pub.digest(raw)
    manifest["files"]["README.md"]["size"] = len(raw)
    review["file_map_sha256"] = hourly.pub.digest(hourly.pub.canonical(manifest["files"]))
    review["output_file_map_sha256"] = hourly.pub.digest(
        hourly.pub.canonical({name: manifest["files"][name] for name in hourly.OUTPUTS})
    )
    manifest["review_receipt_sha256"] = hourly.pub.digest(hourly.pub.canonical(review))
    write_private(root / "manifest.json", manifest)
    write_private(root / "review.json", review)
    new_id = hourly.pub.digest(hourly.pub.canonical(manifest))
    root.rename(queue / "ready" / new_id)
    with pytest.raises(
        hourly.pub.PublicationError, match="generated-readme-outside-deterministic-block"
    ):
        hourly.hourly(config_path, execute=True, test_local_remote=True)
    assert run(setup[2], "status", "--porcelain") == ""


@pytest.mark.parametrize("scope", ["runtime", "goal"])
def test_loss_of_previously_observed_runtime_or_goal_preserves_last_good(setup, tmp_path, scope):
    previous = report(tmp_path)
    if scope == "runtime":
        previous["product_runtime"].update(
            selected_runs=1,
            local_model_tokens={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            rows=[
                {
                    "date": "2026-10-03",
                    "provider": "local-vllm",
                    "model": "Qwen/Qwen3.5-9B",
                    "model_calls": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                }
            ],
        )
    else:
        previous["goal_counters"]["recorded_goal_period_totals"] = {
            "observed_periods": 1,
            "tokens_used": 10,
            "active_seconds": 5,
            "complete_project_lifetime": False,
            "captured_at": "2026-10-03T12:00:00+00:00",
        }
    seal(
        setup,
        {
            "README.md": (setup[2] / "README.md").read_bytes(),
            hourly.OUTPUTS[0]: hourly.pub.canonical(previous) + b"\n",
            hourly.OUTPUTS[1]: hourly.markdown(previous),
        },
    )
    shared.publish(setup, dry_run=False)
    before = run(setup[2], "rev-parse", "HEAD")
    path, _, _ = configure(setup, tmp_path, report(tmp_path))
    with pytest.raises(
        hourly.pub.PublicationError, match=scope + "-regression-last-good-preserved"
    ):
        hourly.hourly(path, execute=True, test_local_remote=True)
    assert run(setup[2], "rev-parse", "HEAD") == before
    assert run(setup[2], "status", "--porcelain") == ""


def test_collector_source_changes_during_collection_rejected(setup, tmp_path):
    path, config, fake = configure(setup, tmp_path, report(tmp_path))
    with fake.open("a") as stream:
        stream.write("open(__file__, 'a').write('# modified during collection')\n")
    config["collector_sha256"] = hourly.pub.digest(fake.read_bytes())
    write_private(path, config)
    setup[1]["usage_pipeline"]["collector_sha256"] = config["collector_sha256"]
    setup[1]["usage_pipeline"]["hourly_config_sha256"] = hourly.pub.digest(
        hourly.pub.canonical(config)
    )
    write_private(setup[0], setup[1])
    with pytest.raises(hourly.pub.PublicationError, match="pipeline-changed-during-collection"):
        hourly.hourly(path, execute=True, test_local_remote=True)
    assert run(setup[2], "status", "--porcelain") == ""


@pytest.mark.parametrize("change", ["missing", "duplicate", "reversed"])
def test_hourly_malformed_readme_keeps_public_tree(setup, tmp_path, change):
    repo = setup[2]
    raw = MARKED_README
    if change == "missing":
        raw = raw.replace(hourly.pub.COST_END, b"")
    elif change == "duplicate":
        raw += hourly.pub.COST_START + b"\n"
    else:
        raw = hourly.pub.COST_END + b"\n" + hourly.pub.COST_START + b"\n"
    (repo / "README.md").write_bytes(raw)
    run(repo, "add", "README.md")
    run(repo, "commit", "-m", "Malformed fixture markers")
    run(repo, "push", "origin", "main")
    before = run(repo, "rev-parse", "HEAD")
    path, _, _ = configure(setup, tmp_path, report(tmp_path))
    with pytest.raises(hourly.pub.PublicationError, match="readme-cost-markers"):
        hourly.hourly(path, execute=True, test_local_remote=True)
    assert run(repo, "rev-parse", "HEAD") == before
    assert run(repo, "status", "--porcelain") == ""
    assert not list((Path(setup[1]["queue"]) / "ready").iterdir())
