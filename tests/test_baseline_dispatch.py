"""Baseline dispatch has no provider or research-loop route."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from lab import cli
from lab.director import baseline_operation


class _Result:
    def __init__(self, *, row=None, scalar=None):
        self.row = row
        self.scalar = scalar

    def mappings(self):
        return self

    def one_or_none(self):
        return self.row

    def one(self):
        return self.row

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar


class _Connection:
    def __init__(self, request, payload_sha256, run_id):
        self.request = request
        self.payload_sha256 = payload_sha256
        self.run_id = run_id
        self.claims = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, _params=None):
        sql = str(statement)
        if "SELECT state, request_json, payload_sha256, origin, owner_id" in sql:
            return _Result(
                row={
                    "state": "queued",
                    "request_json": self.request,
                    "payload_sha256": self.payload_sha256,
                    "origin": "local",
                    "owner_id": "local:test",
                }
            )
        if "UPDATE lab.runs SET state='running'" in sql:
            return _Result(scalar=self.run_id)
        if "SELECT lab.claim_initial_director_execution(" in sql:
            self.claims.append(dict(_params))
            return _Result(
                scalar={
                    "generation": 1,
                    "worker_invocation_id": _params["worker_invocation_id"],
                    "state": "running",
                    "newly_claimed": True,
                }
            )
        if "SELECT state, report_sha256 FROM lab.runs" in sql:
            return _Result(row={"state": "stop_requested", "report_sha256": None})
        return _Result()


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection

    @contextmanager
    def begin(self):
        yield self.connection

    def dispose(self):
        return None


def test_baseline_dispatch_never_constructs_provider_or_enters_research_loop(tmp_path, monkeypatch):
    run_id = uuid4()
    manifest = {
        "schema": "director-suite.v1",
        "suite_id": "fixture.baseline",
        "suite_version": 7,
        "weight_policy": "spec-3.2.5-appendix-c-waterfill.v1",
        "family_cap": 0.25,
        "type_shares": {"EVT": 1.0},
        "family_shares": {f"family-{index}": 0.25 for index in range(4)},
        "tasks": [
            {
                "task_id": f"task-{index}",
                "dataset_id": f"fixture-{index}",
                "split_id": "dev",
                "session_id": "local",
                "profile_sha256": "f" * 64,
                "family": "EVT",
                "task_weight": 0.25,
                "independent_family": f"family-{index}",
                "label_tier": "gold",
                "provenance": {
                    "dataset_id": f"fixture-{index}",
                    "split_id": "dev",
                    "session_id": "local",
                    "source_manifest_sha256": "f" * 64,
                    "source_revision": "fixture-v1",
                    "license_id": "CC0-1.0",
                    "attribution": "Synthetic dispatch fixture",
                    "access_terms": "Locally generated test data",
                    "usage_profile": "noncommercial_research",
                },
                "context": {
                    "seed": 0,
                    "signals": ["x"],
                    "regime_signals": [],
                    "sampling_s": 60,
                    "time_budget_s": 30.0,
                },
                "columns": ["x"],
                "train": [[0.0], [1.0]],
                "evaluation": [[1.0], [2.0]],
            }
            for index in range(4)
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    (tmp_path / "suite.json").write_bytes(manifest_bytes)
    request = {
        "purpose": "baseline",
        "track": "anomaly",
        "suite": "fixture.baseline",
        "budget": {"experiments": 0, "wall_seconds": 60, "model_tokens": 0},
        "program_version": "fixture-v1",
        "idempotency_key": "baseline-dispatch-key-001",
        "suite_manifest_sha256": manifest_sha,
        "scenario_sha256": "b" * 64,
        "provider": "fake-json",
        "provider_config_sha256": None,
        "provider_registry_entry_sha256": "c" * 64,
        "proposal_limit": 0,
        "harness_sha256": "d" * 64,
        "image_sha256": "e" * 64,
    }
    payload = json.dumps(
        {key: value for key, value in request.items() if key != "idempotency_key"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    payload_sha = hashlib.sha256(payload).hexdigest()
    connection = _Connection(request, payload_sha, run_id)
    engine = _Engine(connection)
    entry = SimpleNamespace(
        suite_id="fixture.baseline",
        track="anomaly",
        program_version="fixture-v1",
        provider="fake-json",
        proposal_limit=2,
        suite_manifest_sha256=manifest_sha,
        scenario_sha256="b" * 64,
        provider_config_sha256=None,
        proposal_contract="candidate-python.v1",
        snapshot_sha256=None,
    )
    registry = SimpleNamespace(
        get=lambda _suite: entry,
        verify_entry=lambda _entry: (tmp_path / "suite.json", tmp_path / "scenario.json"),
        entry_sha256=lambda _entry: "c" * 64,
    )
    (tmp_path / "scenario.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("LAB_SUITE_REGISTRY_FILE", str(tmp_path / "registry.json"))
    monkeypatch.setattr(cli, "load_suite_registry", lambda *_args: registry)
    monkeypatch.setattr(cli, "_director_engine", lambda: engine)
    monkeypatch.setattr(cli, "_planner_engine", lambda: engine)
    monkeypatch.setattr(cli, "_private_runtime_directory", lambda path: path)
    monkeypatch.setattr(
        cli,
        "capture_current_owner",
        lambda digest, _run_id: SimpleNamespace(
            payload_sha256=digest,
            worker_pid=100,
            worker_start_ticks=200,
            worker_boot_id="00000000-0000-0000-0000-000000000001",
            worker_unit="swapp-ai-scientist-director-dispatch-00000000000000000000000000000001.service",
            worker_invocation_id="1" * 32,
            worker_cgroup="/user.slice/user-1000.slice/swapp-ai-scientist-director-dispatch-00000000000000000000000000000001.service",
        ),
    )

    class _Lease:
        def __init__(self, _engine, _run_id):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(cli, "DirectorRunLease", _Lease)
    monkeypatch.setattr(
        baseline_operation,
        "run_baseline_operation",
        lambda *_args, **_kwargs: {"run_id": str(run_id), "state": "stop_requested"},
    )
    monkeypatch.setattr(
        cli,
        "load_fake_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fake provider called")),
    )
    monkeypatch.setattr(
        cli,
        "_local_qwen_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model provider called")),
    )
    monkeypatch.setattr(
        cli,
        "_run_director",
        lambda *_args: (_ for _ in ()).throw(AssertionError("research loop / holdout called")),
    )

    result = cli._dispatch_director_run(run_id)
    assert result["state"] == "stop_requested"
    assert result["dispatch"] == "stop_requested"
    assert len(connection.claims) == 1
    contract = json.loads(connection.claims[0]["execution_json"])
    assert contract["suite_version"] == 7
    assert contract["suite_manifest_sha256"] == manifest_sha
    assert contract["request"]["purpose"] == "baseline"
    assert cli.active_execution_owner() is None
