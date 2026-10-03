"""Durable broker replay must retain the authenticated caller generation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.llm import aos_gpu_executor as executor_module
from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_executor import BrokerOwnedTurnExecutor
from lab.llm.gpu_scheduler import LeaseConflict


def _cached_turn(tmp_path: Path, monkeypatch, *, bound: bool = True):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    executor = object.__new__(BrokerOwnedTurnExecutor)
    executor.control_store = None
    executor.database = private / "turns.sqlite"
    executor._clock = lambda: 100.0
    executor._authenticator = SimpleNamespace(still_current=lambda _peer: True)
    profile = SimpleNamespace(
        profile_id="aos.decider.v1", deployment_digest="b" * 64, config_sha256="c" * 64
    )
    executor._profiles = SimpleNamespace(get=lambda *_args: profile)
    executor._ensure_tables()
    peer = PeerGeneration(os.getuid(), 123, 456, "boot", "aos.service", "d" * 32, "/aos")
    request = {
        "version": 1,
        "op": "infer",
        "request_id": "a" * 32,
        "profile_id": profile.profile_id,
        "deployment_digest": profile.deployment_digest,
        "payload": {"input": "synthetic"},
    }
    canonical = executor_module._canonical_json(request).encode()
    columns = [
        "request_id",
        "request_sha256",
        "profile_id",
        "deployment_digest",
        "profile_config_sha256",
        "state",
        "response_json",
        "usage_json",
        "generation_json",
        "created_boottime",
        "updated_boottime",
    ]
    values = [
        request["request_id"],
        hashlib.sha256(canonical).hexdigest(),
        profile.profile_id,
        profile.deployment_digest,
        profile.config_sha256,
        "completed",
        '{"ok":true}',
        "{}",
        json.dumps(
            {
                "unit": "child.service",
                "invocation_id": "e" * 32,
                "main_pid": 321,
                "control_group": "/child",
            }
        ),
        100.0,
        100.0,
    ]
    with sqlite3.connect(executor.database) as connection:
        existing = {row[1] for row in connection.execute("PRAGMA table_info(aos_gpu_turn_results)")}
        if "principal_json" in existing:
            columns.append("principal_json")
            values.append(executor_module._canonical_json(asdict(peer)) if bound else None)
        connection.execute(
            f"INSERT INTO aos_gpu_turn_results({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in values)})",
            values,
        )
    monkeypatch.setattr(executor_module, "_profile_payload", lambda *_args: b"synthetic")
    arguments = {
        "request_id": request["request_id"],
        "request_bytes": canonical,
        "request_sha256": hashlib.sha256(canonical).hexdigest(),
        "profile_id": profile.profile_id,
        "deployment_digest": profile.deployment_digest,
        "payload": request["payload"],
        "deadline": time.monotonic() + 10,
    }
    return executor, peer, arguments


def test_completed_replay_rejects_new_authenticated_generation(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    successor = replace(peer, pid=124, start_ticks=457, invocation_id="f" * 32)
    with pytest.raises(LeaseConflict, match="principal"):
        executor.run_turn(peer=successor, **arguments)


def test_completed_replay_accepts_exact_original_generation(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    assert executor.run_turn(peer=peer, **arguments).response == {"ok": True}


def test_legacy_unbound_result_cannot_be_adopted(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch, bound=False)
    with pytest.raises(LeaseConflict, match="principal"):
        executor.run_turn(peer=peer, **arguments)


@pytest.mark.parametrize("state", ["intent", "queued", "running", "result_ready", "failed"])
def test_other_durable_states_reject_successor_before_scheduler(
    tmp_path: Path, monkeypatch, state: str
):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    with sqlite3.connect(executor.database) as connection:
        if state == "result_ready":
            connection.execute("UPDATE aos_gpu_turn_results SET state=?", (state,))
        else:
            connection.execute(
                "UPDATE aos_gpu_turn_results SET state=?,response_json=NULL,usage_json=NULL,"
                "generation_json=NULL",
                (state,),
            )
    with pytest.raises(LeaseConflict, match="principal"):
        executor.run_turn(peer=replace(peer, invocation_id="f" * 32), **arguments)


def test_new_intent_persists_principal_across_executor_restart(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    profile = executor._profiles.get()
    request_id = "9" * 32
    assert executor._intent(request_id, arguments["request_sha256"], profile, peer) is None
    restarted = object.__new__(BrokerOwnedTurnExecutor)
    restarted.database = executor.database
    restarted._clock = lambda: 101.0
    restarted._ensure_tables()
    assert restarted._intent(request_id, arguments["request_sha256"], profile, peer) is None
    with pytest.raises(LeaseConflict, match="principal"):
        restarted._intent(
            request_id, arguments["request_sha256"], profile, replace(peer, start_ticks=999)
        )
    with pytest.raises(LeaseConflict, match="immutable input"):
        restarted._intent(request_id, "0" * 64, profile, peer)


def test_old_schema_migration_leaves_receipt_unbound(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    with sqlite3.connect(executor.database) as connection:
        connection.execute("ALTER TABLE aos_gpu_turn_results DROP COLUMN principal_json")
    executor._ensure_tables()
    with pytest.raises(LeaseConflict, match="principal"):
        executor.run_turn(peer=peer, **arguments)


def test_peer_generation_change_during_replay_is_rejected(tmp_path: Path, monkeypatch):
    executor, peer, arguments = _cached_turn(tmp_path, monkeypatch)
    checks = iter([True, False])
    executor._authenticator = SimpleNamespace(still_current=lambda _peer: next(checks))
    with pytest.raises(executor_module.BrokerExecutionError, match="before result replay"):
        executor.run_turn(peer=peer, **arguments)
