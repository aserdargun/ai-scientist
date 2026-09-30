from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.llm.aos_gpu_executor import SystemdAOSProfileRuntime
from lab.llm.aos_gpu_service import _prepare_socket_path
from lab.llm.gpu_scheduler import GpuLease


def test_aos_unbound_starting_unit_keeps_quarantine_until_late_launch_fence() -> None:
    now = [100.0]
    runtime = object.__new__(SystemdAOSProfileRuntime)
    runtime.clock = lambda: now[0]
    runtime._binding = lambda _lease: {
        "observed_gpu_pids_json": "[]",
        "control_group": None,
        "launch_state": "starting",
        "unit": "swapp-aos-gpu-turn-test.service",
        "created_boottime": 100.0,
        "total_seconds": 720,
        "invocation_id": None,
    }
    runtime.units = SimpleNamespace(inspect=lambda _unit: SimpleNamespace(load_state="not-found"))

    lease = GpuLease(
        owner="aos",
        request_id="a" * 32,
        fencing_token=1,
        phase="activating",
        activation_deadline=700.0,
        inference_deadline=820.0,
        total_deadline=820.0,
        heartbeat_deadline=105.0,
        slice_seconds=120,
        owner_identity=None,
        owner_unit="swapp-aos-gpu-test.service",
        owner_invocation_id="b" * 32,
    )

    assert runtime.verify_drained(lease) is False


def test_broker_start_removes_only_stale_owned_socket(tmp_path: Path) -> None:
    path = tmp_path / "broker.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    _prepare_socket_path(path)
    assert not path.exists()

    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    live.listen(1)
    try:
        with pytest.raises(FileExistsError, match="already served"):
            _prepare_socket_path(path)
        assert path.exists()
    finally:
        live.close()
