"""CPU grant checks use real installed sources; process and DB boundaries stay inert."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from lab import cli
from lab.api.registry import load_suite_registry
from lab.director.ownership import ExecutionOwner
from lab.director.resume import ResumeReceipt
from tests import test_aos_cpu_registry as fixtures

aos_cpu_installation = fixtures.aos_cpu_installation


def row_for(f):
    entry = f.entry
    request = dict(
        suite=entry.suite_id,
        track=entry.track,
        program_version=entry.program_version,
        provider=entry.provider,
        proposal_limit=1,
        suite_manifest_sha256=entry.suite_manifest_sha256,
        scenario_sha256=entry.scenario_sha256,
        provider_config_sha256=entry.provider_config_sha256,
        provider_registry_entry_sha256=f.registry.entry_sha256(entry),
        snapshot_sha256=entry.snapshot_sha256,
        study_kind="single_snapshot_study",
        aos_cpu_study=f.grant.model_dump(mode="json"),
        aos_cpu_study_sha256=f.grant.sha256,
        budget=dict(experiments=1, wall_seconds=60, model_tokens=0),
    )
    row = dict(request_json=request, owner_id=f.grant.owner_id, origin="aos")
    rehash(row)
    return row


def rehash(row):
    raw = json.dumps(
        row["request_json"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    row["payload_sha256"] = hashlib.sha256(raw).hexdigest()


def configure(f, monkeypatch):
    monkeypatch.setattr(cli, "PROJECT_ROOT", f.runtime.parent.parent)
    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(f.registry_path))
    monkeypatch.setattr(cli, "compute_harness_hash", lambda *_: SimpleNamespace(sha256="c" * 64))
    monkeypatch.setattr(cli, "DEFAULT_SANDBOX_IMAGE", "image@sha256:" + "d" * 64)


def test_installed_grant_binds_identical_initial_and_takeover_contract(
    aos_cpu_installation, monkeypatch
):
    f = aos_cpu_installation
    configure(f, monkeypatch)
    row, run_id = row_for(f), uuid4()
    first = cli._validate_dispatch_target(run_id, row, f.registry)
    resumed = cli._validate_dispatch_target(run_id, row, f.registry)
    assert first.contract == resumed.contract
    assert first.contract.execution_json["request"]["aos_cpu_study_sha256"] == f.grant.sha256
    cli._registered_aos_cpu_grid_binding(
        row["request_json"],
        first.suite_path,
        first.scenario_path,
        owner_id=row["owner_id"],
        origin=row["origin"],
    )


@pytest.mark.parametrize(
    "change",
    [
        "owner",
        "origin",
        "missing_grant",
        "null_grant",
        "grant_sha",
        "snapshot",
        "config",
        "wall",
        "tokens",
        "boolean_tokens",
        "purpose",
        "proposal_count",
    ],
)
def test_durable_request_cannot_expand_or_remove_cpu_authority(
    aos_cpu_installation, monkeypatch, change
):
    f = aos_cpu_installation
    configure(f, monkeypatch)
    row = row_for(f)
    request = row["request_json"]
    if change == "owner":
        row["owner_id"] = "other-owner"
    elif change == "origin":
        row["origin"] = "local"
    elif change == "missing_grant":
        del request["aos_cpu_study"]
        del request["aos_cpu_study_sha256"]
    elif change == "null_grant":
        request["aos_cpu_study"] = None
    elif change == "grant_sha":
        request["aos_cpu_study_sha256"] = "f" * 64
    elif change == "snapshot":
        request["snapshot_sha256"] = "f" * 64
    elif change == "config":
        request["provider_config_sha256"] = "f" * 64
    elif change == "wall":
        request["budget"]["wall_seconds"] = f.grant.max_wall_seconds + 1
    elif change == "tokens":
        request["budget"]["model_tokens"] = 1
    elif change == "boolean_tokens":
        request["budget"]["model_tokens"] = False
    elif change == "purpose":
        request["purpose"] = "baseline"
    else:
        request["proposal_limit"] = 2
    rehash(row)  # Correct content digest never substitutes for grant/owner authority.
    suite, scenario = f.registry.verify_entry(f.entry)
    with pytest.raises(ValueError):
        cli._validate_dispatch_target(uuid4(), row, f.registry)
    with pytest.raises(ValueError):
        cli._registered_aos_cpu_grid_binding(
            request, suite, scenario, owner_id=row["owner_id"], origin=row["origin"]
        )


def test_revoked_registry_grant_does_not_fall_back_to_legacy_grid(
    aos_cpu_installation, monkeypatch
):
    f = aos_cpu_installation
    configure(f, monkeypatch)
    row = row_for(f)
    suite, scenario = f.registry.verify_entry(f.entry)
    document = json.loads(f.registry_path.read_bytes())
    entry = next(item for item in document["suites"] if item["suite_id"] == f.entry.suite_id)
    entry.pop("aos_cpu_study")
    entry.pop("snapshot_sha256")
    f.registry_path.write_text(json.dumps(document))
    registry = load_suite_registry(f.registry_path, f.runtime)
    # Remove the client-side marker too: DB AOS origin still demands the explicit grant.
    row["request_json"].pop("aos_cpu_study")
    row["request_json"].pop("aos_cpu_study_sha256")
    rehash(row)
    with pytest.raises(ValueError, match="explicit registry grant"):
        cli._validate_dispatch_target(uuid4(), row, registry)
    with pytest.raises(ValueError, match="explicit registry grant"):
        cli._registered_aos_cpu_grid_binding(
            row["request_json"], suite, scenario, owner_id=row["owner_id"], origin="aos"
        )


def test_changed_installed_source_or_alternate_path_denied(aos_cpu_installation, monkeypatch):
    f = aos_cpu_installation
    configure(f, monkeypatch)
    row = row_for(f)
    suite, scenario = f.registry.verify_entry(f.entry)
    with pytest.raises(ValueError, match="paths"):
        cli._registered_aos_cpu_grid_binding(
            row["request_json"], scenario, scenario, owner_id=row["owner_id"], origin="aos"
        )
    scenario.write_bytes(scenario.read_bytes() + b" ")
    with pytest.raises(ValueError):
        cli._registered_aos_cpu_grid_binding(
            row["request_json"], suite, scenario, owner_id=row["owner_id"], origin="aos"
        )


@pytest.mark.parametrize("resume", [False, True])
def test_real_director_entry_rejects_wrong_owner_before_baseline(
    aos_cpu_installation, monkeypatch, resume
):
    f = aos_cpu_installation
    configure(f, monkeypatch)
    row = row_for(f) | {"state": "running", "owner_id": "other-owner"}
    run_id = uuid4()
    owner = ExecutionOwner(run_id, 1, "a" * 32, "b" * 64)
    suite, scenario = f.registry.verify_entry(f.entry)
    args = SimpleNamespace(
        run_id=run_id,
        seed_wall_seconds=30,
        artifact_root=f.runtime,
        suite_file=suite,
        scenario_file=scenario,
    )
    if resume:
        now = datetime.now(UTC)
        args.resume_receipt = ResumeReceipt(
            owner, uuid4(), now, now + timedelta(seconds=60), False, "c" * 64
        )
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.one_or_none.return_value = row
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_planner_engine", MagicMock())
    monkeypatch.setattr(cli, "active_execution_owner", lambda: owner)
    monkeypatch.setattr(cli, "assert_execution_owner_transaction", MagicMock())
    monkeypatch.setattr(cli, "_private_runtime_directory", lambda path: path)
    baseline, loader = MagicMock(), MagicMock()
    monkeypatch.setattr(cli, "run_baseline_suite", baseline)
    monkeypatch.setattr(cli, "load_suite_manifest", loader)
    with pytest.raises(ValueError):
        cli._run_director(args)
    baseline.assert_not_called()
    loader.assert_not_called()
