from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from lab.llm import native_runtime as runtime
from lab.llm.gpu_scheduler import (
    GpuLease,
    PrincipalReceipt,
    ProcessIdentity,
    SharedGpuScheduler,
    _current_process_identity,
)


def test_systemd_duration_parser_accepts_measured_units_and_rejects_unbounded() -> None:
    assert runtime._parse_systemd_duration_usec("2s") == 2_000_000
    assert runtime._parse_systemd_duration_usec("3min 30s") == 210_000_000
    assert runtime._parse_systemd_duration_usec("250ms") == 250_000
    for invalid in ("", "infinity", "[not set]", "2fortnights", "1s trailing"):
        with pytest.raises(ValueError):
            runtime._parse_systemd_duration_usec(invalid)


def test_model_verifier_recognizes_only_known_huggingface_cache_files() -> None:
    assert runtime._is_known_huggingface_cache_metadata(
        ".cache/huggingface/download/model.safetensors.lock"
    )
    assert runtime._is_known_huggingface_cache_metadata(
        f".cache/huggingface/trees/{runtime.MODEL_REVISION}.json"
    )
    assert not runtime._is_known_huggingface_cache_metadata(
        ".cache/huggingface/download/unexpected.bin"
    )
    assert not runtime._is_known_huggingface_cache_metadata("weights/private.safetensors")


def test_uds_response_has_one_absolute_deadline_for_slow_drip(tmp_path: Path) -> None:
    socket_path = tmp_path / "model.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)
    body = json.dumps({"result": "x" * 80}, separators=(",", ":")).encode()
    response_head = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
    )

    def serve_slowly() -> None:
        try:
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(response_head)
                for byte in body:
                    try:
                        connection.send(bytes([byte]))
                    except OSError:
                        break
                    time.sleep(0.04)
        finally:
            server.close()

    thread = threading.Thread(target=serve_slowly, daemon=True)
    thread.start()
    started = time.monotonic()
    with pytest.raises(runtime.ModelRuntimeError, match="absolute deadline"):
        runtime._read_uds_json(socket_path, "GET", "/v1/models", None, timeout=0.15, maximum=256)
    elapsed = time.monotonic() - started
    thread.join(timeout=1)
    assert elapsed < 0.5


class _DrainUnits:
    def __init__(self, *, invocation_id: str, preserve_gpu_on_stop: bool = False) -> None:
        self.snapshot = runtime.UnitSnapshot(
            "loaded",
            "active",
            invocation_id,
            "/user.slice/swapp-gpu.slice/unit.service",
            1234,
            "SWAPP GPU model turn " + "a" * 64,
            "SWAPP_GPU_TURN_NONCE=" + "a" * 64,
            runtime.MODEL_UNIT_MEMORY_BYTES,
            0,
            2_000_000,
            runtime.MODEL_UNIT_TASKS,
            runtime.MODEL_RUNTIME_MAX_SECONDS * 1_000_000,
        )
        self.pids = frozenset({1234})
        self.stopped: list[str] = []
        self.gpu: _DrainGpu | None = None
        self.preserve_gpu_on_stop = preserve_gpu_on_stop

    def inspect(self, unit: str) -> runtime.UnitSnapshot:
        return self.snapshot

    def cgroup_pids(self, _group: str) -> frozenset[int]:
        return self.pids

    def stop(self, unit: str, timeout_seconds: int) -> None:
        assert timeout_seconds == 10
        self.stopped.append(unit)
        self.snapshot = runtime.UnitSnapshot(
            "not-found",
            "inactive",
            "",
            "/user.slice/swapp-gpu.slice/unit.service",
            0,
            "",
            "",
            0,
            0,
            0,
            0,
            0,
        )
        self.pids = frozenset()
        if self.gpu is not None and not self.preserve_gpu_on_stop:
            self.gpu.processes.pop(1234, None)

    def cgroup_empty(self, _group: str) -> bool:
        return not self.pids


class _DrainGpu:
    def __init__(self) -> None:
        self.processes = {1234: 900}

    def snapshot(self) -> runtime.GpuSnapshot:
        used = sum(self.processes.values())
        return runtime.GpuSnapshot(self.processes.copy(), used, 16_384)


def _drain_runtime(
    tmp_path: Path, invocation_id: str, *, preserve_gpu_on_stop: bool = False
) -> tuple[runtime.OwnedVllmRuntime, _DrainUnits]:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE gpu_runtime_bindings (
                owner TEXT, request_id TEXT, fencing_token INTEGER, unit TEXT, nonce TEXT,
                uds_directory TEXT, model_sha256 TEXT, expected_description TEXT,
                created_boottime REAL, runtime_max_seconds INTEGER, launch_state TEXT,
                launch_finished_boottime REAL, invocation_id TEXT, main_pid INTEGER,
                main_start_ticks INTEGER, boot_id TEXT, control_group TEXT,
                observed_gpu_pids_json TEXT, peak_gpu_memory_mib INTEGER,
                start_gpu_used_memory_mib INTEGER, end_gpu_used_memory_mib INTEGER,
                startup_seconds REAL, inference_seconds REAL, drain_seconds REAL,
                PRIMARY KEY(owner,request_id,fencing_token))"""
        )
        connection.execute(
            "INSERT INTO gpu_runtime_bindings "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lab",
                "req-1",
                1,
                "swapp-lab-gpu-turn-" + "b" * 32 + ".service",
                "a" * 64,
                str(tmp_path),
                "c" * 64,
                "SWAPP GPU model turn " + "a" * 64,
                1.0,
                runtime.MODEL_RUNTIME_MAX_SECONDS,
                "created",
                2.0,
                invocation_id,
                None,
                None,
                None,
                "/user.slice/swapp-gpu.slice/unit.service",
                "[]",
                0,
                None,
                None,
                None,
                None,
                None,
            ),
        )
    instance = object.__new__(runtime.OwnedVllmRuntime)
    instance._database = database
    instance._clock = runtime.boottime
    instance._sleep = lambda _seconds: None
    units = _DrainUnits(invocation_id=invocation_id, preserve_gpu_on_stop=preserve_gpu_on_stop)
    instance.units = units
    instance.gpu = _DrainGpu()
    units.gpu = instance.gpu
    return instance, units


def _lease() -> GpuLease:
    return GpuLease(
        "lab",
        "req-1",
        1,
        "inference",
        10.0,
        20.0,
        30.0,
        5.0,
        30,
        ProcessIdentity(10, 20, "boot"),
        "swapp-lab.service",
        "d" * 32,
    )


def test_drain_stops_only_the_persisted_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    instance, units = _drain_runtime(tmp_path, "a" * 32)
    allowed, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 3)
    assert allowed
    assert units.stopped == ["swapp-lab-gpu-turn-" + "b" * 32 + ".service"]


def test_drain_refuses_a_reused_unit_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    instance, units = _drain_runtime(tmp_path, "e" * 32)
    units.snapshot = runtime.UnitSnapshot(
        units.snapshot.load_state,
        units.snapshot.active_state,
        "a" * 32,
        units.snapshot.control_group,
        units.snapshot.main_pid,
        units.snapshot.description,
        units.snapshot.environment,
        units.snapshot.memory_max,
        units.snapshot.memory_swap_max,
        units.snapshot.cpu_quota_usec,
        units.snapshot.tasks_max,
        units.snapshot.runtime_max_usec,
    )
    allowed, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 1)
    assert not allowed
    assert units.stopped == []


def test_lab_drain_verifier_fails_closed_for_aos_owned_turn() -> None:
    instance = object.__new__(runtime.OwnedVllmRuntime)
    instance._clock = runtime.boottime
    aos_lease = GpuLease(
        "aos",
        "request-foreign",
        1,
        "inference",
        10.0,
        20.0,
        30.0,
        5.0,
        30,
        ProcessIdentity(10, 20, "boot"),
        "swapp-aos-gpu.service",
        "d" * 32,
    )

    assert not instance.verify_drained(aos_lease)


def test_native_qwen_rejects_aos_owner_before_profile_or_scheduler_access() -> None:
    instance = object.__new__(runtime.OwnedVllmRuntime)
    instance._clock = lambda: 1.0
    instance._failure_phase = "stale"
    instance._last_response_metadata = {"stale": True}
    instance._last_request_profile = {"stale": True}

    with pytest.raises(ValueError, match="native Qwen turns are Lab-owned"):
        instance.run_turn(
            "aos",
            "a" * 32,
            [{"role": "user", "content": "must not launch"}],
            enable_thinking=False,
        )

    assert instance._failure_phase is None
    assert instance._last_response_metadata is None
    assert instance._last_request_profile is None


def test_lab_scheduler_keeps_quarantined_aos_turn_without_its_binding(
    tmp_path: Path,
) -> None:
    class Resolver:
        def resolve(self, owner: str) -> PrincipalReceipt:
            identity = _current_process_identity()
            return PrincipalReceipt(owner, identity, f"swapp-{owner}-gpu-test.service", owner * 32)

        def verify(self, receipt: PrincipalReceipt) -> bool:
            identity = _current_process_identity()
            return (
                receipt.owner in {"aos", "lab"}
                and receipt.identity == identity
                and receipt.unit == f"swapp-{receipt.owner}-gpu-test.service"
                and receipt.invocation_id == receipt.owner * 32
            )

    clock_value = [100.0]
    lab_runtime = object.__new__(runtime.OwnedVllmRuntime)
    lab_runtime._clock = lambda: clock_value[0]
    scheduler = SharedGpuScheduler(
        tmp_path / "shared.sqlite3",
        principal_resolver=Resolver(),
        drain_verifier=lab_runtime.verify_drained,
        max_activation_seconds=2,
        max_inference_seconds=2,
        max_total_seconds=4,
        queue_timeout_seconds=60,
        clock=lambda: clock_value[0],
    )
    scheduler.submit(
        "aos",
        "active-aos-turn",
        b"payload",
        activation_seconds=2,
        inference_seconds=2,
        total_seconds=4,
        queue_timeout_seconds=60,
    )
    lease = scheduler.try_acquire("aos", "active-aos-turn")
    assert lease is not None
    ready = scheduler.mark_ready(lease)
    assert ready is not None
    clock_value[0] = ready.inference_deadline + 1
    assert scheduler.heartbeat(ready) is None
    assert not scheduler.recover_quarantined()
    with sqlite3.connect(tmp_path / "shared.sqlite3") as connection:
        owner, request_id, phase = connection.execute(
            "SELECT active_owner,active_request_id,phase FROM gpu_turn_state WHERE singleton=1"
        ).fetchone()
    assert (owner, request_id, phase) == ("aos", "active-aos-turn", "quarantined")


def test_drain_persists_gpu_pid_observed_during_crash_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    instance, units = _drain_runtime(tmp_path, "a" * 32, preserve_gpu_on_stop=True)
    allowed, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 0.03)
    assert not allowed
    assert units.stopped
    with sqlite3.connect(instance._database) as connection:
        observed = connection.execute(
            "SELECT observed_gpu_pids_json FROM gpu_runtime_bindings"
        ).fetchone()[0]
    assert json.loads(observed) == [1234]
    again, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 0.03)
    assert not again
    instance.gpu.processes.clear()
    recovered, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 1)
    assert recovered


def test_foreign_gpu_pid_does_not_prevent_owned_unit_stop_or_get_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    instance, units = _drain_runtime(tmp_path, "a" * 32)
    instance.gpu.processes[9999] = 64
    allowed, _, _ = instance._verify_and_drain(_lease(), deadline=runtime.boottime() + 1)
    assert not allowed
    assert units.stopped
    assert instance.gpu.processes == {9999: 64}


class _LocalFakeServer:
    def __init__(self, socket_path: Path, *, slow_completion: bool) -> None:
        self.socket_path = socket_path
        self.slow_completion = slow_completion
        self.stopping = threading.Event()
        self.listener: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.post_requests: list[dict[str, object]] = []

    def start(self) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        assert self.listener is not None
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(1)
                request_bytes = bytearray()
                while b"\r\n\r\n" not in request_bytes:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    request_bytes.extend(chunk)
                request_head, separator, request_body = bytes(request_bytes).partition(b"\r\n\r\n")
                first = request_head.split(b"\r\n", maxsplit=1)[0]
                if first.startswith(b"POST "):
                    headers = {}
                    for header in request_head.split(b"\r\n")[1:]:
                        name, colon, value = header.partition(b":")
                        if colon:
                            headers[name.strip().lower()] = value.strip()
                    content_length = int(headers[b"content-length"])
                    body_bytes = bytearray(request_body if separator else b"")
                    while len(body_bytes) < content_length:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        body_bytes.extend(chunk)
                    self.post_requests.append(json.loads(body_bytes[:content_length]))
                if first.startswith(b"GET /v1/models"):
                    body = b'{"data":[{"id":"qwen3.5-9b"}]}'
                else:
                    body = (
                        b'{"choices":[{"finish_reason":"stop",'
                        b'"message":{"content":"ok"}}],'
                        b'"usage":{"prompt_tokens":1,"completion_tokens":1}}'
                    )
                response_head = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                )
                try:
                    connection.sendall(response_head)
                    if self.slow_completion and first.startswith(b"POST "):
                        for byte in body:
                            connection.send(bytes([byte]))
                            time.sleep(0.08)
                    else:
                        connection.sendall(body)
                except OSError:
                    continue

    def stop(self) -> None:
        self.stopping.set()
        if self.listener is not None:
            self.listener.close()
        if self.thread is not None:
            self.thread.join(timeout=1)


class _FakeRuntimeUnits:
    def __init__(self, *, slow_completion: bool) -> None:
        self.slow_completion = slow_completion
        self.server: _LocalFakeServer | None = None
        self.servers: list[_LocalFakeServer] = []
        self.running = False
        self.unit = ""
        self.nonce = ""
        self.directory: Path | None = None
        self.cgroup = "/user.slice/swapp-gpu.slice/fake.service"
        self.invocation = "f" * 32
        self.stop_calls: list[str] = []

    def ensure_slice(self) -> None:
        return None

    def launch(
        self,
        unit: str,
        nonce: str,
        uds_directory: Path,
        pin: runtime.ModelPin,
        diagnostic_log: Path,
        *,
        model_max_len: int,
    ) -> None:
        assert pin.revision == runtime.MODEL_REVISION
        assert diagnostic_log.parent.parent.name == "native-doctor"
        assert model_max_len == 4_096
        uds_directory.mkdir(mode=0o700)
        self.unit, self.nonce, self.directory = unit, nonce, uds_directory
        self.server = _LocalFakeServer(
            uds_directory / "vllm.sock", slow_completion=self.slow_completion
        )
        self.server.start()
        self.servers.append(self.server)
        self.running = True

    def inspect(self, unit: str) -> runtime.UnitSnapshot:
        assert unit == self.unit
        if not self.running:
            return runtime.UnitSnapshot(
                "not-found", "inactive", "", self.cgroup, 0, "", "", 0, 0, 0, 0, 0
            )
        assert self.directory is not None
        return runtime.UnitSnapshot(
            "loaded",
            "active",
            self.invocation,
            self.cgroup,
            os.getpid(),
            f"SWAPP GPU model turn {self.nonce}",
            f"SWAPP_GPU_TURN_NONCE={self.nonce}",
            runtime.MODEL_UNIT_MEMORY_BYTES,
            0,
            2_000_000,
            runtime.MODEL_UNIT_TASKS,
            runtime.MODEL_RUNTIME_MAX_SECONDS * 1_000_000,
        )

    def stop(self, unit: str, timeout_seconds: int) -> None:
        assert unit == self.unit and timeout_seconds == 10
        self.stop_calls.append(unit)
        self.running = False
        if self.server is not None:
            self.server.stop()

    def cgroup_pids(self, control_group: str) -> frozenset[int]:
        assert control_group == self.cgroup
        return frozenset({os.getpid()}) if self.running else frozenset()

    def cgroup_empty(self, control_group: str) -> bool:
        assert control_group == self.cgroup
        return not self.running

    def cgroup_memory_peak(self, control_group: str) -> int:
        assert control_group == self.cgroup
        return 512 * 1024 * 1024

    def cgroup_cpu_usage(self, control_group: str) -> int:
        assert control_group == self.cgroup
        return 20_000


class _FakeRuntimeGpu:
    def __init__(self, units: _FakeRuntimeUnits) -> None:
        self.units = units

    def snapshot(self) -> runtime.GpuSnapshot:
        processes = {os.getpid(): 16} if self.units.running else {}
        return runtime.GpuSnapshot(processes, sum(processes.values()), 16_384)


class _CurrentProcessPrincipal:
    def __init__(self) -> None:
        identity = ProcessIdentity(
            os.getpid(),
            int(Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )
        from lab.llm.gpu_scheduler import PrincipalReceipt

        self.receipt = PrincipalReceipt("lab", identity, "swapp-lab.service", "e" * 32)

    def resolve(self, owner: str):
        assert owner == "lab"
        return self.receipt

    def verify(self, receipt) -> bool:
        return receipt == self.receipt


def _fake_runtime(
    tmp_path: Path, runtime_dir: Path, *, slow_completion: bool
) -> tuple[runtime.OwnedVllmRuntime, _FakeRuntimeUnits]:
    model_dir = tmp_path / "model"
    model_dir.mkdir(mode=0o700)
    model_file = model_dir / "weights.bin"
    model_file.write_bytes(b"pinned model fixture")
    import hashlib

    digest = hashlib.sha256(model_file.read_bytes()).hexdigest()
    pin = runtime.ModelPin(
        model_dir,
        runtime.MODEL_REVISION,
        "c" * 64,
        (("weights.bin", model_file.stat().st_size, digest),),
    )
    units = _FakeRuntimeUnits(slow_completion=slow_completion)
    instance = runtime.OwnedVllmRuntime(
        tmp_path / "scheduler.sqlite3",
        principal_resolver=_CurrentProcessPrincipal(),
        pin=pin,
        unit_manager=units,
        gpu_observer=_FakeRuntimeGpu(units),
        activation_seconds=5,
        inference_seconds=1,
        total_seconds=6,
        queue_timeout_seconds=40,
        required_free_disk_bytes=0,
    )
    return instance, units


def test_run_turn_fake_server_returns_and_drains_exact_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / f"gpu-{uuid4().hex[:8]}"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        reply = instance.run_turn(
            "lab",
            "normal-1",
            [{"role": "user", "content": "short fixture prompt"}],
            enable_thinking=False,
            max_output_tokens=8,
        )
        assert reply.text == "ok"
        assert reply.prompt_tokens == 1 and reply.completion_tokens == 1
        assert instance._last_response_metadata == {
            "http_content_type": "application/json",
            "content_length_bytes": len(
                b'{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}'
            ),
            "finish_reason": "stop",
            "usage_valid": True,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "message_content_type": "str",
            "content_length_chars": 2,
            "reasoning_present": False,
            "reasoning_length_chars": None,
        }
        assert len(units.servers[0].post_requests) == 1
        assert "thinking_token_budget" not in units.servers[0].post_requests[0]
        assert reply.measurements.peak_gpu_memory_mib == 16
        assert len(units.stop_calls) == 1
        assert not units.running
    finally:
        shutil.rmtree(runtime_dir)


def test_run_turn_sends_explicit_thinking_budget_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / f"gpu-{uuid4().hex[:8]}"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        instance.run_turn(
            "lab",
            "thinking-1",
            [{"role": "user", "content": "short fixture prompt"}],
            enable_thinking=True,
            max_output_tokens=8,
            thinking_token_budget=4,
        )
        post_body = units.servers[0].post_requests[0]
        assert post_body["max_tokens"] == 8
        assert post_body["thinking_token_budget"] == 4
    finally:
        shutil.rmtree(runtime_dir)


@pytest.mark.parametrize(
    ("temperature", "top_p"),
    [
        (True, 0.8),
        (float("nan"), 0.8),
        (float("inf"), 0.8),
        (-0.1, 0.8),
        (2.1, 0.8),
        (0.7, True),
        (0.7, float("nan")),
        (0.7, 0.0),
        (0.7, 1.1),
    ],
)
def test_sampling_profile_validation_rejects_malformed_values(
    temperature: object, top_p: object
) -> None:
    with pytest.raises(ValueError):
        runtime._validate_sampling_parameters(temperature, top_p)


@pytest.mark.parametrize("value", [True, 0, -1, 129, 1.5, "20"])
def test_top_k_validation_rejects_non_profile_values(value: object) -> None:
    with pytest.raises(ValueError):
        runtime._validate_top_k(value)


def test_registered_s2_profile_binds_top_k_and_bounded_capacity() -> None:
    profile = runtime.LOCAL_SMOKE_S2_PROFILE
    assert profile.profile_id.endswith("s2-bounded-smoke.v2")
    assert profile.top_k == 20
    assert profile.max_output_tokens == 4_096
    assert profile.thinking_token_budget == 512
    assert profile.model_max_len == profile.max_context_tokens + profile.max_output_tokens
    assert profile.activation_seconds == 120
    assert profile.inference_seconds == 90
    assert profile.total_seconds == 210


def test_run_turn_serializes_sampling_profile_into_request_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / f"gpu-{uuid4().hex[:8]}"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        instance.run_turn(
            "lab",
            "sampling-1",
            [{"role": "user", "content": "short fixture prompt"}],
            enable_thinking=True,
            max_output_tokens=8,
            thinking_token_budget=4,
            temperature=0.6,
            top_p=0.95,
            top_k=20,
        )
        post_body = units.servers[0].post_requests[0]
        assert post_body["temperature"] == 0.6
        assert post_body["top_p"] == 0.95
        assert post_body["top_k"] == 20
        assert {
            key: instance._last_request_profile[key]
            for key in (
                "max_output_tokens",
                "enable_thinking",
                "thinking_token_budget",
                "temperature",
                "top_p",
                "top_k",
            )
        } == {
            "max_output_tokens": 8,
            "enable_thinking": True,
            "thinking_token_budget": 4,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
        }
        with pytest.raises(runtime.LeaseConflict, match="different request terms"):
            instance.run_turn(
                "lab",
                "sampling-1",
                [{"role": "user", "content": "short fixture prompt"}],
                enable_thinking=True,
                max_output_tokens=8,
                thinking_token_budget=4,
                temperature=0.6,
                top_p=0.95,
                top_k=19,
            )
        assert len(units.servers) == 1
    finally:
        shutil.rmtree(runtime_dir)


def test_run_turn_sends_a_frozen_strict_json_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / f"gpu-{uuid4().hex[:8]}"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=False)
        schema: dict[str, object] = {
            "type": "object",
            "properties": {"candidate_source": {"type": "string"}},
            "required": ["candidate_source"],
            "additionalProperties": False,
        }
        instance.run_turn(
            "lab",
            "structured-1",
            [{"role": "user", "content": "return candidate JSON"}],
            enable_thinking=False,
            max_output_tokens=8,
            response_schema_name="candidate-proposal-v1",
            response_schema=schema,
        )
        schema["properties"] = {"unexpected": {"type": "string"}}
        body = units.servers[0].post_requests[0]
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "candidate-proposal-v1",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"candidate_source": {"type": "string"}},
                    "required": ["candidate_source"],
                    "additionalProperties": False,
                },
            },
        }
        assert instance._last_request_profile["response_schema_sha256"] is not None
        assert len(units.servers[0].post_requests) == 1
    finally:
        shutil.rmtree(runtime_dir)


@pytest.mark.parametrize(
    ("value", "enable_thinking", "maximum"),
    [
        (True, True, 8),
        (0, True, 8),
        (-1, True, 8),
        (8, True, 8),
        (4, False, 8),
    ],
)
def test_thinking_budget_validation_is_strict(
    value: object, enable_thinking: bool, maximum: int
) -> None:
    with pytest.raises(ValueError):
        runtime._validate_thinking_token_budget(
            value, enable_thinking=enable_thinking, max_output_tokens=maximum
        )


def test_run_turn_timeout_drains_exact_unit_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    runtime_dir = Path("/tmp") / f"gpu-{uuid4().hex[:8]}"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setattr(
        runtime.OwnedVllmRuntime, "_expected_control_group", staticmethod(lambda *_: True)
    )
    try:
        instance, units = _fake_runtime(tmp_path, runtime_dir, slow_completion=True)
        with pytest.raises(runtime.ModelRuntimeError, match="deadline|expired"):
            instance.run_turn(
                "lab",
                "slow-1",
                [{"role": "user", "content": "short fixture prompt"}],
                enable_thinking=False,
                max_output_tokens=8,
            )
        assert len(units.stop_calls) == 1
        assert not units.running
    finally:
        shutil.rmtree(runtime_dir)


def test_response_metadata_is_bounded_and_omits_generated_text() -> None:
    secret_text = "private generated reasoning and answer"
    metadata = runtime._capture_response_metadata(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "content": None,
                        "reasoning": secret_text,
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 512},
        },
        {"http_content_type": "application/json", "content_length_bytes": 900},
    )
    assert metadata["finish_reason"] == "length"
    assert metadata["usage_valid"] is True
    assert metadata["completion_tokens"] == 512
    assert metadata["message_content_type"] == "null_or_missing"
    assert metadata["reasoning_present"] is True
    assert metadata["reasoning_length_chars"] == len(secret_text)
    assert secret_text not in repr(metadata)
    with pytest.raises(runtime.ModelOutputBudgetExceeded, match="output token budget"):
        runtime._reject_output_truncation(metadata)


def test_response_metadata_marks_invalid_usage_without_echoing_values() -> None:
    metadata = runtime._capture_response_metadata(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": 42}}],
            "usage": {"prompt_tokens": True, "completion_tokens": -1},
        },
        {},
    )
    assert metadata["usage_valid"] is False
    assert metadata["prompt_tokens"] is None
    assert metadata["completion_tokens"] is None
    assert metadata["message_content_type"] == "int"
    assert metadata["content_length_chars"] is None


def _fake_vllm_environment(tmp_path: Path) -> tuple[Path, Path]:
    environment = tmp_path / "vllm-env"
    executable = environment / "bin/vllm"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="ascii")
    executable.chmod(0o700)
    toolkit = environment / "lib/python3.12/site-packages/nvidia/cu13"
    (toolkit / "bin").mkdir(parents=True)
    nvcc = toolkit / "bin/nvcc"
    nvcc.write_text(
        "#!/bin/sh\necho 'Cuda compilation tools, release 13.2, V13.2.78'\n",
        encoding="ascii",
    )
    nvcc.chmod(0o700)
    (toolkit / "include").mkdir()
    (toolkit / "include/cuda.h").write_text("#define CUDA_VERSION 13020\n", encoding="ascii")
    (toolkit / "include/cuda_runtime_api.h").write_text(
        "#define CUDART_VERSION 13020\n", encoding="ascii"
    )
    (toolkit / "nvvm/libdevice").mkdir(parents=True)
    (toolkit / "nvvm/libdevice/libdevice.10.bc").touch()
    return executable, toolkit


def test_pinned_cuda_toolkit_is_resolved_only_inside_vllm_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable, toolkit = _fake_vllm_environment(tmp_path)
    monkeypatch.setattr(runtime, "VLLM_EXECUTABLE", executable)
    assert runtime._pinned_cuda_toolkit_home() == toolkit.resolve()

    escaped = tmp_path / "outside-toolkit"
    escaped.mkdir()
    (toolkit / "bin/nvcc").unlink()
    (toolkit / "bin/nvcc").symlink_to(escaped / "nvcc")
    with pytest.raises(runtime.ModelRuntimeError, match="incomplete or escape"):
        runtime._pinned_cuda_toolkit_home()


@pytest.mark.parametrize("model_max_len", [4_096, 6_144])
def test_model_server_command_has_pinned_qwen3_parsers_and_no_public_ingress(
    model_max_len: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    pin = runtime.ModelPin(model_dir, runtime.MODEL_REVISION, "a" * 64, ())
    executable, cuda_home = _fake_vllm_environment(tmp_path)
    monkeypatch.setattr(runtime, "VLLM_EXECUTABLE", executable)
    diagnostic_log = tmp_path / "diagnostic.log"
    diagnostic_log.touch(mode=0o600)
    captured: list[str] = []

    def fake_run(args, *, timeout):
        captured.extend(args)
        stdout = (
            "Cuda compilation tools, release 13.2, V13.2.78" if args[-1:] == ["--version"] else ""
        )
        assert timeout in {5, 25}
        return runtime.subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(runtime.SystemdUnitManager, "_run", staticmethod(fake_run))
    runtime.SystemdUnitManager().launch(
        "swapp-lab-gpu-turn-" + "b" * 32 + ".service",
        "c" * 64,
        tmp_path,
        pin,
        diagnostic_log,
        model_max_len=model_max_len,
    )
    assert captured[captured.index("--max-model-len") + 1] == str(model_max_len)
    for flag in (
        "--uds",
        "--reasoning-parser",
        "qwen3",
        "--tool-call-parser",
        "qwen3_coder",
        "--enable-auto-tool-choice",
        "--language-model-only",
        "--enforce-eager",
    ):
        assert flag in captured
    assert "--host" not in captured and "--port" not in captured
    assert "--service-type=exec" in captured
    assert f"--property=LimitFSIZE={runtime.MAX_DIAGNOSTIC_LOG_BYTES}" in captured
    for setting in (
        f"--setenv=CUDA_HOME={cuda_home}",
        f"--setenv=CUDA_PATH={cuda_home}",
        f"--setenv=CUDACXX={cuda_home / 'bin/nvcc'}",
        f"--setenv=PATH={cuda_home / 'bin'}:/usr/bin:/bin",
        "--setenv=MAX_JOBS=2",
        "--setenv=FLASHINFER_NVCC_THREADS=1",
        "--setenv=XDG_CACHE_HOME=/tmp/model-cache/xdg",
        "--setenv=VLLM_CACHE_ROOT=/tmp/model-cache/vllm",
        "--setenv=VLLM_USE_FLASHINFER_SAMPLER=0",
        "--setenv=FLASHINFER_WORKSPACE_BASE=/tmp/model-cache/flashinfer",
        "--setenv=VLLM_FLASHINFER_AUTOTUNE_CACHE_DIR=/tmp/model-cache/flashinfer-autotune",
        "--setenv=TRITON_CACHE_DIR=/tmp/model-cache/triton",
        "--setenv=TORCHINDUCTOR_CACHE_DIR=/tmp/model-cache/torchinductor",
        "--setenv=TORCH_EXTENSIONS_DIR=/tmp/model-cache/torch-extensions",
        "--setenv=TORCH_HOME=/tmp/model-cache/torch",
        "--setenv=CUDA_CACHE_PATH=/tmp/model-cache/cuda-driver",
        "--setenv=CUDA_CACHE_MAXSIZE=268435456",
        "--setenv=FLASH_ATTENTION_CUTE_DSL_CACHE_DIR=/tmp/model-cache/flash-attention-cute",
        "--setenv=CUTE_DSL_CACHE_DIR=/tmp/model-cache/cute-dsl",
        "--setenv=HF_HOME=/tmp/model-cache/huggingface",
        "--setenv=TRANSFORMERS_CACHE=/tmp/model-cache/transformers",
    ):
        assert setting in captured


def test_doctor_profile_is_fixed_and_result_receipt_omits_generated_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    reply = runtime.ModelReply(
        "diagnostic answer",
        12,
        8,
        runtime.ModelMeasurements(2.0, 1.0, 0.2, 512, 1200, 62, 62, 9000),
    )
    captured: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, database: Path, *, principal_resolver, profile) -> None:
            captured["database"] = database
            captured["resolver"] = principal_resolver
            captured["profile"] = profile
            self._failure_phase = None
            self._last_response_metadata = None
            self._last_request_profile = None

        def run_turn(
            self,
            owner,
            request_id,
            messages,
            *,
            enable_thinking,
            max_output_tokens,
            thinking_token_budget,
            temperature,
            top_p,
            top_k,
            profile,
        ):
            captured.update(
                owner=owner,
                request_id=request_id,
                messages=messages,
                enable_thinking=enable_thinking,
                max_output_tokens=max_output_tokens,
                thinking_token_budget=thinking_token_budget,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                profile=profile,
            )
            self._last_request_profile = {
                "max_output_tokens": max_output_tokens,
                "enable_thinking": enable_thinking,
                "thinking_token_budget": thinking_token_budget,
                "temperature": temperature,
                "top_p": top_p,
                "top_k": top_k,
            }
            return reply

    output = tmp_path / "data/runtime/native-doctor" / "result.json"
    monkeypatch.setattr(runtime, "OwnedVllmRuntime", FakeRuntime)
    monkeypatch.setattr(runtime, "_write_doctor_result", lambda *_: output)
    monkeypatch.setenv("SWAPP_AOS_GPU_UNIT", "swapp-aos-gpu-test.service")
    monkeypatch.setenv("SWAPP_LAB_GPU_UNIT", "swapp-lab-gpu-test.service")
    runtime_db = tmp_path / "data/runtime/gpu/test.db"
    monkeypatch.setenv("SWAPP_GPU_RUNTIME_DB", str(runtime_db))

    request_id = "d" * 32
    assert runtime.doctor_main(["--request-id", request_id, "--profile", "s1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert "text" not in result
    assert captured["owner"] == "lab"
    assert captured["request_id"] == request_id
    assert captured["enable_thinking"] is False
    assert captured["max_output_tokens"] == 128
    assert captured["thinking_token_budget"] is None
    assert captured["temperature"] == 0.0
    assert captured["top_p"] == 1.0
    assert captured["top_k"] is None
    assert result["diagnostics"]["request_profile"] == {
        "max_output_tokens": 128,
        "enable_thinking": False,
        "thinking_token_budget": None,
        "temperature": 0.0,
        "top_p": 1.0,
        "top_k": None,
    }
    messages = captured["messages"]
    assert messages == runtime._DOCTOR_MESSAGES["s1"]
    assert runtime.doctor_main(["--request-id", "e" * 32, "--profile", "s2"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert captured["enable_thinking"] is True
    assert captured["max_output_tokens"] == 512
    assert captured["thinking_token_budget"] == 128
    assert captured["temperature"] == 0.6
    assert captured["top_p"] == 0.95
    assert captured["top_k"] == 20
    assert captured["messages"] == runtime._DOCTOR_MESSAGES["s2"]
    assert result["diagnostics"]["request_profile"] == {
        "max_output_tokens": 512,
        "enable_thinking": True,
        "thinking_token_budget": 128,
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
    }
    assert runtime.doctor_main(
        ["--request-id", "f" * 32, "--profile", "research-s2"]
    ) == 0
    research_result = json.loads(capsys.readouterr().out)
    assert research_result["capacity_status"] == "measurement_only_not_capacity_acceptance"
    assert captured["profile"] == runtime.LOCAL_RESEARCH_S2_PROFILE
    assert captured["max_output_tokens"] == 8_192
    assert captured["enable_thinking"] is True


def test_doctor_diagnostics_root_is_private_before_any_model_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "data/runtime/native-doctor"
    (root / "logs").mkdir(parents=True)
    root.chmod(0o755)
    (root / "logs").chmod(0o755)
    prepared, logs = runtime._prepare_doctor_directories()
    assert prepared.stat().st_mode & 0o777 == 0o700
    assert logs.stat().st_mode & 0o777 == 0o700
