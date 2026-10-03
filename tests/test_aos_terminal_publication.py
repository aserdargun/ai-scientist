"""Deterministic CPU interleavings; synthetic peers and drain observations only."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from aos_admission_fixture import BOOT, grant_for, output_pin

from lab.llm import aos_gpu_executor
from lab.llm.aos_gpu_broker import PeerGeneration, TurnReceipt
from lab.llm.aos_gpu_control_store import ControlStore, ControlStoreError, canonical, digest
from lab.llm.aos_gpu_executor import (
    BrokerExecutionError,
    BrokerOwnedTurnExecutor,
    SystemdAOSProfileRuntime,
    TurnBudgets,
    authenticated_peer,
)
from lab.llm.aos_profile_output import load_contract


def _bind_fixture_child(p, lease, directory):
    with closing(p.store._connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        p.store.launch_handoff(connection, asdict(lease), "plan")
        connection.execute(
            "INSERT INTO aos_gpu_child_bindings(owner,request_id,fencing_token,profile_id,"
            "deployment_digest,request_sha256,unit,nonce,workdir,launch_state,"
            "created_boottime,total_seconds,invocation_id,main_pid,main_start_ticks,boot_id,"
            "control_group) VALUES('aos',?,?,?,?,?,?,?,?, 'bound',100,15,?,?,?,?,?)",
            (
                p.request_id,
                lease.fencing_token,
                p.profile.profile_id,
                p.profile.deployment_digest,
                hashlib.sha256(
                    aos_gpu_executor._profile_payload(p.profile, p.payload)
                ).hexdigest(),
                p.result.unit,
                "9" * 64,
                str(directory),
                p.result.invocation_id,
                p.result.main_pid,
                77,
                p.peer.boot_id,
                p.result.control_group,
            ),
        )
        p.store.launch_handoff(connection, asdict(lease), "start")
        p.store.launch_handoff(connection, asdict(lease), "go")
        connection.commit()


@pytest.fixture
def publication(tmp_path, monkeypatch):
    peer = PeerGeneration(
        1000,
        123,
        55,
        BOOT,
        "swapp-aos-gpu-fixture.service",
        "e" * 32,
        "/fixture/aos",
        122,
        54,
    )
    monkeypatch.setattr("lab.llm.gpu_scheduler._boot_id", lambda: peer.boot_id)
    monkeypatch.setattr("lab.llm.gpu_scheduler._process_identity_alive", lambda _identity: True)
    monkeypatch.setattr("lab.llm.aos_gpu_executor._profile_payload", lambda *_args: b"fixture")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    database = private / "arbiter.sqlite3"
    store = ControlStore(
        database,
        clock=lambda: 100.0,
        boot_id=lambda: peer.boot_id,
        peer_verifier=lambda actual: actual == asdict(peer),
    )
    pin = output_pin()
    contract = load_contract(
        Path(__file__).resolve().parents[1] / "lab/llm/contracts/profile_output_v2",
        pin["bundle_sha256"],
    )
    payload = {
        "request": {
            "state": "synthetic state",
            "question": "choose",
            "options": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        }
    }
    profile = SimpleNamespace(
        profile_id="aos.decider.turn.v1",
        kind="decider",
        deployment_digest="b" * 64,
        config_sha256="c" * 64,
        response_schema_sha256=contract.content_pin("aos.decider.turn.v1"),
        output_contract=pin,
        budgets=TurnBudgets(10, 5, 15, 60),
        max_output_tokens=16,
        context_tokens=256,
    )
    request_id = "a" * 32
    frame = canonical(
        {
            "version": 1,
            "op": "infer",
            "request_id": request_id,
            "profile_id": profile.profile_id,
            "deployment_digest": profile.deployment_digest,
            "payload": payload,
        }
    ).encode()
    request_hash = hashlib.sha256(frame).hexdigest()
    target = {
        "request_id": request_id,
        "request_sha256": request_hash,
        "original_peer_generation_sha256": digest(asdict(peer)),
    }
    metrics = {
        "latency_ms": 3.0,
        "load_ms": 0.0,
        "inference_ms": 2.0,
        "reused": False,
        "prepared_cpu": False,
        "input_tokens": 3,
        "peak_vram_bytes": 0,
        "broker_activation_load_ms": 4.0,
    }
    unit = "swapp-aos-gpu-turn-" + "f" * 32 + ".service"
    result = TurnReceipt(
        {
            "deployment_digest": profile.deployment_digest,
            "prediction": {"selected_option": "a", "probabilities": {"a": 0.5, "b": 0.5}},
            "metrics": metrics,
        },
        metrics,
        unit,
        "f" * 32,
        456,
        "/fixture/swapp-gpu.slice/" + unit,
    )
    cleaned = []
    child = {
        "unit": result.unit,
        "invocation_id": result.invocation_id,
        "pid": result.main_pid,
        "start_ticks": 77,
        "boot_id": peer.boot_id,
        "control_group": result.control_group,
    }
    schema_runtime = SystemdAOSProfileRuntime(
        database,
        units=SimpleNamespace(),
        gpu=SimpleNamespace(),
        work_root=private / "synthetic-turns",
        clock=lambda: 100.0,
        control_store=store,
    )
    binding = SimpleNamespace(
        store=store,
        request_id=request_id,
        profile=profile,
        payload=payload,
        request_hash=request_hash,
        result=result,
        peer=peer,
    )

    def prepare(lease):
        _bind_fixture_child(binding, lease, schema_runtime.work_root / ("1" * 32))

    def drain(lease):
        schema_runtime._mark_drained(lease)
        store.record_drain(
            asdict(lease),
            {
                "fixture_only": True,
                "child_generation": child,
                "late_start_fenced": True,
                "cgroup_empty": True,
                "gpu_absent": True,
            },
        )
        return True

    runtime = SimpleNamespace(
        execute=lambda lease, *_args: (prepare(lease), result)[1],
        verify_drained=drain,
        after_release=lambda lease: cleaned.append(lease.request_id),
        failure_diagnostics=lambda _lease: "",
    )
    executor = BrokerOwnedTurnExecutor(
        database,
        authenticator=SimpleNamespace(still_current=lambda actual: actual == peer),
        lab_units={"aos": peer.unit, "lab": "swapp-lab-gpu-fixture.service"},
        profiles=SimpleNamespace(get=lambda *_args: profile),
        runtime=runtime,
        clock=lambda: 100.0,
        control_store=store,
    )
    budget = {**asdict(profile.budgets), "max_output_tokens": 16, "context_tokens": 256}
    admission = grant_for(
        asdict(peer),
        profile.profile_id,
        profile.deployment_digest,
        profile.config_sha256,
        profile.response_schema_sha256,
    )

    def register():
        return store.register_intent(
            asdict(peer),
            request_id,
            request_hash,
            profile.profile_id,
            profile.deployment_digest,
            profile.config_sha256,
            profile.response_schema_sha256,
            160.0,
            budget,
            admission=admission,
        )

    def cancel():
        return store.cancel(asdict(peer), target, profile.profile_id, profile.deployment_digest)

    return SimpleNamespace(
        peer=peer,
        admission=admission,
        profile=profile,
        store=store,
        executor=executor,
        result=result,
        target=target,
        frame=frame,
        payload=payload,
        request_id=request_id,
        request_hash=request_hash,
        register=register,
        cancel=cancel,
        cleaned=cleaned,
        prepare_lease=prepare,
    )


def _released_result_ready(publication, *, canceled=True):
    p = publication
    p.register()
    p.executor._bind_output_request(p.frame, p.profile, p.peer)
    p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer)
    with authenticated_peer(p.peer):
        p.executor.scheduler.submit(
            "aos",
            p.request_id,
            p.frame,
            activation_seconds=10,
            inference_seconds=5,
            total_seconds=15,
            queue_timeout_seconds=60,
        )
        p.executor._set_state(p.request_id, "queued")
        lease = p.executor.scheduler.try_acquire("aos", p.request_id)
        assert lease is not None
        p.executor._set_state(p.request_id, "running")
        prepare = getattr(p, "prepare_lease", None)
        if prepare is not None:
            prepare(lease)
        # Duplicate B registers before cancel and pauses before reading its cache.
        assert p.register() == 160.0
        p.executor._store_result_ready(p.request_id, p.result)
        p.store.record_result(
            p.request_id,
            p.result.response,
            p.result.usage,
            {
                "unit": p.result.unit,
                "invocation_id": p.result.invocation_id,
                "main_pid": p.result.main_pid,
                "control_group": p.result.control_group,
            },
        )
        if canceled:
            p.cancel()
        p.executor.scheduler.release(lease)
    status = p.store.lookup(
        asdict(p.peer), p.target, p.profile.profile_id, p.profile.deployment_digest
    )
    assert status["state"] == ("canceled" if canceled else "completed")
    with closing(p.executor._connect()) as connection:
        assert connection.execute("SELECT state FROM gpu_turn_requests").fetchone()[0] == "done"


@pytest.mark.parametrize("publication_path", ["intent", "poll", "complete"])
def test_inflight_duplicate_cannot_publish_canceled_result(publication, publication_path):
    p = publication
    _released_result_ready(p)
    with pytest.raises((BrokerExecutionError, ControlStoreError)):
        if publication_path == "intent":
            p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer)
        elif publication_path == "poll":
            p.executor._completed_result(p.request_id)
        else:
            p.executor._complete(p.request_id)
    with closing(p.executor._connect()) as connection:
        assert (
            connection.execute("SELECT state FROM aos_gpu_turn_results").fetchone()[0]
            == "result_ready"
        )


def test_canceled_result_ready_cleans_workdir_after_committed_release(publication, monkeypatch):
    p = publication
    release = p.executor.scheduler.release

    def cancel_then_release(lease):
        p.cancel()
        release(lease)

    monkeypatch.setattr(p.executor.scheduler, "release", cancel_then_release)
    with pytest.raises((BrokerExecutionError, ControlStoreError)):
        p.executor.run_turn(
            peer=p.peer,
            request_id=p.request_id,
            request_bytes=p.frame,
            request_sha256=p.request_hash,
            profile_id=p.profile.profile_id,
            deployment_digest=p.profile.deployment_digest,
            payload=p.payload,
            deadline=time.monotonic() + 60,
            admission=p.admission,
        )
    assert p.cleaned == [p.request_id]


@pytest.mark.parametrize("publication_path", ["intent", "poll", "complete"])
def test_completed_receipt_allows_exact_cached_result(publication, publication_path):
    p = publication
    _released_result_ready(p, canceled=False)
    terminal = p.store.lookup(
        asdict(p.peer), p.target, p.profile.profile_id, p.profile.deployment_digest
    )["terminal_receipt"]
    assert terminal["admission_binding"] == json.loads(p.admission.binding_json)
    assert terminal["admission_binding_sha256"] == digest(terminal["admission_binding"])
    if publication_path == "intent":
        result = p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer)
    elif publication_path == "poll":
        result = p.executor._completed_result(p.request_id)
    else:
        result = p.executor._complete(p.request_id)
    assert result == p.result
    # The already-completed branch must use the same authority on every replay.
    assert p.executor._completed_result(p.request_id) == p.result
    assert p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer) == p.result
    with closing(p.executor._connect()) as connection:
        assert (
            connection.execute("SELECT state FROM aos_gpu_turn_results").fetchone()[0]
            == "completed"
        )
        assert connection.execute(
            "SELECT active_owner,active_token FROM gpu_turn_state"
        ).fetchone()[:] == (None, 1)


@pytest.mark.parametrize("tamper", ["cache", "result", "terminal_hash", "terminal_principal"])
def test_result_and_terminal_mismatch_block_every_publication_path(publication, tamper):
    p = publication
    _released_result_ready(p, canceled=False)
    with closing(p.executor._connect()) as connection:
        if tamper == "cache":
            connection.execute(
                "UPDATE aos_gpu_turn_results SET response_json=?",
                (canonical({"answer": "changed"}),),
            )
        elif tamper == "result":
            row = connection.execute("SELECT result_json FROM aos_control_requests").fetchone()
            result = json.loads(row[0])
            result["response"] = {"answer": "changed"}
            connection.execute(
                "UPDATE aos_control_requests SET result_json=?", (canonical(result),)
            )
        else:
            row = connection.execute("SELECT receipt_json FROM aos_control_requests").fetchone()
            receipt = json.loads(row[0])
            if tamper == "terminal_hash":
                receipt["result_sha256"] = "0" * 64
            else:
                receipt["original_principal"]["invocation_id"] = "0" * 32
            receipt["receipt_sha256"] = digest(
                {key: value for key, value in receipt.items() if key != "receipt_sha256"}
            )
            connection.execute(
                "UPDATE aos_control_requests SET receipt_json=?", (canonical(receipt),)
            )
    for publish in (
        lambda: p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer),
        lambda: p.executor._completed_result(p.request_id),
        lambda: p.executor._complete(p.request_id),
    ):
        with pytest.raises((BrokerExecutionError, ControlStoreError)):
            publish()
    if tamper != "cache":
        with pytest.raises(ControlStoreError):
            p.store.lookup(
                asdict(p.peer), p.target, p.profile.profile_id, p.profile.deployment_digest
            )
    with closing(p.executor._connect()) as connection:
        assert (
            connection.execute("SELECT state FROM aos_gpu_turn_results").fetchone()[0]
            == "result_ready"
        )


def _owned_released_directory(p, tmp_path):
    runtime = SystemdAOSProfileRuntime(
        p.executor.database,
        units=SimpleNamespace(),
        gpu=SimpleNamespace(),
        work_root=tmp_path / "owned-turns",
        clock=lambda: 100.0,
        control_store=p.store,
    )
    directory = runtime.work_root / ("1" * 32)
    directory.mkdir(mode=0o700)
    (directory / "worker.log").write_text("private synthetic fixture")
    child = {
        "unit": p.result.unit,
        "invocation_id": p.result.invocation_id,
        "pid": p.result.main_pid,
        "start_ticks": 77,
        "boot_id": p.peer.boot_id,
        "control_group": p.result.control_group,
    }

    def prepare(lease):
        _bind_fixture_child(p, lease, directory)

    def drain(lease):
        runtime._mark_drained(lease)
        p.store.record_drain(
            asdict(lease),
            {
                "fixture_only": True,
                "child_generation": child,
                "late_start_fenced": True,
                "cgroup_empty": True,
                "gpu_absent": True,
            },
        )
        return True

    p.prepare_lease = prepare
    p.executor.scheduler._drain_verifier = drain
    _released_result_ready(p)
    p.executor._runtime = runtime
    return runtime, directory


def test_restart_cleanup_is_exact_bounded_and_idempotent(publication, tmp_path):
    p = publication
    runtime, directory = _owned_released_directory(p, tmp_path)
    restarted = BrokerOwnedTurnExecutor(
        p.executor.database,
        authenticator=SimpleNamespace(still_current=lambda peer: peer == p.peer),
        lab_units={"aos": p.peer.unit, "lab": "swapp-lab-gpu-fixture.service"},
        profiles=SimpleNamespace(get=lambda *_args: p.profile),
        runtime=runtime,
        clock=lambda: 100.0,
        control_store=ControlStore(
            p.executor.database, clock=lambda: 100.0, boot_id=lambda: p.peer.boot_id
        ),
    )
    assert directory.exists()
    assert restarted.cleanup_terminal_workdirs(limit=1) == 1
    assert not directory.exists()
    assert restarted.cleanup_terminal_workdirs(limit=1) == 0
    with closing(p.store._connect()) as connection:
        row = connection.execute(
            "SELECT receipt_json,cleanup_receipt_sha256 FROM aos_control_requests"
        ).fetchone()
        assert row[1] == json.loads(row[0])["receipt_sha256"]
        assert connection.execute("SELECT active_token FROM gpu_turn_state").fetchone()[0] == 1


@pytest.mark.parametrize("barrier", ["quarantined", "nonterminal", "foreign", "partial_remove"])
def test_cleanup_preserves_unproven_or_foreign_directory(
    publication, tmp_path, monkeypatch, barrier
):
    p = publication
    _runtime, directory = _owned_released_directory(p, tmp_path)
    with closing(p.store._connect()) as connection:
        if barrier == "quarantined":
            connection.execute("UPDATE aos_control_requests SET state='quarantined'")
        elif barrier == "nonterminal":
            connection.execute("UPDATE aos_control_requests SET receipt_json=NULL,state='running'")
        elif barrier == "foreign":
            foreign = tmp_path / "unrelated"
            foreign.mkdir(mode=0o700)
            (foreign / "keep").write_text("unrelated synthetic data")
            connection.execute("UPDATE aos_gpu_child_bindings SET workdir=?", (str(foreign),))
        else:

            def incomplete_remove(_path):
                raise FileNotFoundError("nested child vanished while owned root still exists")

            monkeypatch.setattr("lab.llm.aos_gpu_executor.shutil.rmtree", incomplete_remove)
    assert p.executor.cleanup_terminal_workdirs(limit=8) == 0
    assert directory.exists()
    if barrier == "foreign":
        assert (foreign / "keep").exists()
    with closing(p.store._connect()) as connection:
        assert (
            connection.execute(
                "SELECT cleanup_receipt_sha256 FROM aos_control_requests"
            ).fetchone()[0]
            is None
        )


def test_cleanup_rotates_past_eight_invalid_records(publication, tmp_path):
    p = publication
    _runtime, directory = _owned_released_directory(p, tmp_path)
    with closing(p.store._connect()) as connection:
        original = dict(connection.execute("SELECT * FROM aos_control_requests").fetchone())
        binding = dict(connection.execute("SELECT * FROM aos_gpu_child_bindings").fetchone())
        for number in range(8):
            request_id = f"{number:032x}"
            # These synthetic corrupt clones cannot pass exact receipt identity.
            # They sort ahead of the genuine terminal and must not starve it.
            invalid = {**original, "request_id": request_id}
            child = {
                **binding,
                "request_id": request_id,
                "unit": f"fixture-corrupt-{number}.service",
            }
            for table, row in (
                ("aos_control_requests", invalid),
                ("aos_gpu_child_bindings", child),
            ):
                connection.execute(
                    f"INSERT INTO {table}({','.join(row)}) VALUES({','.join('?' for _ in row)})",
                    tuple(row.values()),
                )
    assert p.executor.cleanup_terminal_workdirs(limit=8) == 0
    assert directory.exists()
    assert p.executor.cleanup_terminal_workdirs(limit=8) == 1
    assert not directory.exists()
