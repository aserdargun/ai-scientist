"""Actual child preparation hashes, with synthetic process/model observations."""

import hashlib
import json
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_aos_profile_integration as integration

from lab.llm.aos_gpu_executor import (
    BrokerExecutionError,
    SystemdAOSProfileRuntime,
    authenticated_peer,
)

contract = integration.contract
wired = integration.wired


def prepare_child(p, monkeypatch, tmp_path):
    p.register()
    p.executor._bind_output_request(p.raw, p.profile, p.peer)
    p.executor._intent(p.request_id, p.request_hash, p.profile, p.peer)
    with authenticated_peer(p.peer):
        p.executor.scheduler.submit(
            "aos",
            p.request_id,
            p.raw,
            activation_seconds=10,
            inference_seconds=5,
            total_seconds=15,
            queue_timeout_seconds=60,
        )
        p.executor._set_state(p.request_id, "queued")
        lease = p.executor.scheduler.try_acquire("aos", p.request_id)
        assert lease is not None
    p.executor._set_state(p.request_id, "running")
    runtime = SystemdAOSProfileRuntime(
        p.executor.database,
        units=SimpleNamespace(),
        gpu=SimpleNamespace(),
        work_root=tmp_path / "prepared",
        control_store=p.store,
        clock=lambda: 100.0,
    )
    # No model artifact or process is launched; actual payload preparation remains intact.
    monkeypatch.setattr("lab.llm.aos_gpu_executor._model_paths", lambda *_args: ())
    row, intent = runtime._prepare(lease, p.profile, p.request_id, json.loads(p.raw)["payload"])
    actual_body = intent.input_path.read_bytes()
    assert row["request_sha256"] == hashlib.sha256(actual_body).hexdigest()
    assert row["request_sha256"] != p.request_hash
    control_group = p.receipt.control_group.rsplit("/", 1)[0] + "/" + intent.unit
    with closing(p.executor._connect()) as connection:
        connection.execute(
            "UPDATE aos_gpu_child_bindings SET launch_state='bound',invocation_id=?,"
            "main_pid=?,main_start_ticks=77,boot_id=?,control_group=? WHERE request_id=?",
            (
                p.receipt.invocation_id,
                p.receipt.main_pid,
                p.peer.boot_id,
                control_group,
                p.request_id,
            ),
        )
        connection.commit()
    return replace(p.receipt, unit=intent.unit, control_group=control_group)


def test_real_prepared_payload_can_persist_result_with_distinct_envelope_hash(
    wired, monkeypatch, tmp_path
):
    p = wired
    receipt = prepare_child(p, monkeypatch, tmp_path)
    p.executor._store_result_ready(p.request_id, receipt)
    with closing(p.executor._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_gpu_turn_results").fetchone()
        assert row["state"] == "result_ready" and row["request_sha256"] == p.request_hash


@pytest.mark.parametrize("wrong_hash", ["envelope", "other_payload"])
def test_child_hash_cannot_be_replaced_by_envelope_or_another_payload(
    wired, monkeypatch, tmp_path, wrong_hash
):
    p = wired
    receipt = prepare_child(p, monkeypatch, tmp_path)
    with closing(p.executor._connect()) as connection:
        connection.execute(
            "UPDATE aos_gpu_child_bindings SET request_sha256=?",
            (p.request_hash if wrong_hash == "envelope" else "0" * 64,),
        )
        connection.commit()
    with pytest.raises(BrokerExecutionError):
        p.executor._store_result_ready(p.request_id, receipt)
    with closing(p.executor._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_gpu_turn_results").fetchone()
        assert row["state"] == "running" and row["response_json"] is None
