"""Fair two-principal GPU turns and cross-process fencing regressions."""

from __future__ import annotations

import multiprocessing
import sqlite3
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from lab.llm.gpu_scheduler import (
    SYSTEMD_PROPERTIES,
    GpuLease,
    LeaseConflict,
    PrincipalReceipt,
    ProcessIdentity,
    SharedGpuScheduler,
    SystemdPrincipalResolver,
    _current_process_identity,
    _process_identity_alive,
    _systemctl_show,
)


class FixturePrincipalResolver:
    """Test-only resolver: lane maps are fixed, process identities stay live."""

    def resolve(self, owner: str) -> PrincipalReceipt:
        return PrincipalReceipt(
            owner, _current_process_identity(), f"swapp-{owner}-gpu-test.service", owner * 32
        )

    def verify(self, receipt: PrincipalReceipt) -> bool:
        return (
            receipt.owner in {"aos", "lab"}
            and receipt.unit == f"swapp-{receipt.owner}-gpu-test.service"
            and receipt.invocation_id == receipt.owner * 32
            and _process_identity_alive(receipt.identity)
        )


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _at(scheduler: SharedGpuScheduler, value: float) -> None:
    clock = scheduler._clock
    assert isinstance(clock, FakeClock)
    clock.value = value


def _scheduler(
    database: Path, *, drain: Callable[[GpuLease], bool] | None = None
) -> SharedGpuScheduler:
    return SharedGpuScheduler(
        database,
        principal_resolver=FixturePrincipalResolver(),
        drain_verifier=drain or (lambda _lease: True),
        max_activation_seconds=10,
        max_inference_seconds=5,
        max_total_seconds=15,
        queue_timeout_seconds=300,
        clock=FakeClock(),
    )


def _start(scheduler: SharedGpuScheduler, owner: str, request: str, at: float) -> GpuLease | None:
    _at(scheduler, at)
    scheduler.submit(owner, request, request.encode())
    return scheduler.try_acquire(owner, request)


def _finish(scheduler: SharedGpuScheduler, lease: GpuLease, at: float) -> None:
    _at(scheduler, at)
    ready = scheduler.mark_ready(lease)
    assert ready is not None
    _at(scheduler, at + 0.1)
    scheduler.release(ready)


def test_run_bound_director_dispatch_unit_is_only_a_lab_principal(monkeypatch) -> None:
    aos_unit = "swapp-aos-gpu-review.service"
    lab_unit = "swapp-ai-scientist-director-dispatch-0123456789abcdef0123456789abcdef.service"
    resolver = SystemdPrincipalResolver({"aos": aos_unit, "lab": lab_unit})
    assert resolver._units == {"aos": aos_unit, "lab": lab_unit}

    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "".join(f"{name}=active\n" for name in SYSTEMD_PROPERTIES)

    def fake_run(command, **_kwargs):
        calls.append(command)
        return Completed()

    monkeypatch.setattr("lab.llm.gpu_scheduler.subprocess.run", fake_run)
    properties = _systemctl_show(lab_unit, owner="lab")
    assert set(properties) == set(SYSTEMD_PROPERTIES)
    assert calls[0][-1] == lab_unit

    with pytest.raises(ValueError, match="fixed owner"):
        SystemdPrincipalResolver({"aos": lab_unit, "lab": "swapp-lab-gpu-review.service"})
    with pytest.raises(ValueError, match="fixed deployment namespace"):
        _systemctl_show(lab_unit, owner="aos")


@pytest.mark.parametrize(
    "unit",
    [
        "swapp-lab-director-dispatch-0123456789abcdef0123456789abcdef.service",
        "swapp-ai-scientist-director-dispatch-0123456789abcdef.service",
        "swapp-ai-scientist-director-dispatch-0123456789abcdef0123456789abcdeG.service",
    ],
)
def test_noncanonical_director_units_are_rejected(unit: str) -> None:
    with pytest.raises(ValueError, match="fixed owner"):
        SystemdPrincipalResolver({"aos": "swapp-aos-gpu-review.service", "lab": unit})


def test_principal_verification_keeps_owner_bound_unit_lookup(monkeypatch) -> None:
    unit = "swapp-ai-scientist-director-dispatch-0123456789abcdef0123456789abcdef.service"
    resolver = SystemdPrincipalResolver({"aos": "swapp-aos-gpu-review.service", "lab": unit})
    identity = _current_process_identity()
    receipt = PrincipalReceipt("lab", identity, unit, "a" * 32)
    calls: list[tuple[str, str]] = []

    def fake_show(selected_unit: str, *, owner: str) -> dict[str, str]:
        calls.append((selected_unit, owner))
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "a" * 32,
            "ControlGroup": f"/user.slice/swapp-gpu.slice/{unit}",
            "MainPID": str(identity.pid),
        }

    monkeypatch.setattr("lab.llm.gpu_scheduler._systemctl_show", fake_show)
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_cgroup",
        lambda pid: f"/user.slice/swapp-gpu.slice/{unit}" if pid == identity.pid else "",
    )
    assert resolver.verify(receipt)
    assert calls == [(unit, "lab")]


def test_broker_verifies_run_bound_lab_parent_without_changing_fixed_resolution(
    monkeypatch,
) -> None:
    configured_lab = "swapp-lab-gpu-train-" + "1" * 32 + ".service"
    run_unit = "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service"
    resolver = SystemdPrincipalResolver(
        {"aos": "swapp-aos-gpu-review.service", "lab": configured_lab}
    )
    identity = ProcessIdentity(31415, 27182, "a" * 36)
    receipt = PrincipalReceipt("lab", identity, run_unit, "b" * 32)
    cgroup = f"/user.slice/user-1000.slice/user@1000.service/swapp.slice/swapp-gpu.slice/{run_unit}"
    calls: list[tuple[str, str]] = []

    def fake_show(unit: str, *, owner: str) -> dict[str, str]:
        calls.append((unit, owner))
        return {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "b" * 32,
            "ControlGroup": cgroup,
            "MainPID": str(identity.pid),
        }

    monkeypatch.setattr("lab.llm.gpu_scheduler._systemctl_show", fake_show)
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_identity_alive", lambda seen: seen == identity
    )
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_cgroup", lambda pid: cgroup if pid == identity.pid else ""
    )

    assert resolver._units["lab"] == configured_lab
    assert resolver.verify(receipt)
    assert calls == [(run_unit, "lab")]


@pytest.mark.parametrize(
    ("owner", "unit", "invocation", "unit_cgroup", "process_matches"),
    [
        (
            "aos",
            "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service",
            "b" * 32,
            None,
            True,
        ),
        (
            "lab",
            "swapp-ai-scientist-director-dispatch-" + "2" * 31 + "G.service",
            "b" * 32,
            None,
            True,
        ),
        ("lab", "swapp-lab-gpu-train-" + "3" * 32 + ".service", "b" * 32, None, True),
        (
            "lab",
            "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service",
            "c" * 32,
            None,
            True,
        ),
        (
            "lab",
            "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service",
            "b" * 32,
            "/user.slice/other",
            True,
        ),
        (
            "lab",
            "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service",
            "b" * 32,
            None,
            False,
        ),
    ],
)
def test_run_bound_lab_verification_rejects_wrong_identity_and_namespace(
    monkeypatch, owner, unit, invocation, unit_cgroup, process_matches
) -> None:
    configured_lab = "swapp-lab-gpu-train-" + "1" * 32 + ".service"
    resolver = SystemdPrincipalResolver(
        {"aos": "swapp-aos-gpu-review.service", "lab": configured_lab}
    )
    identity = ProcessIdentity(31415, 27182, "a" * 36)
    receipt = PrincipalReceipt(owner, identity, unit, "b" * 32)
    expected_cgroup = (
        f"/user.slice/user-1000.slice/user@1000.service/swapp.slice/swapp-gpu.slice/{unit}"
    )

    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._systemctl_show",
        lambda _unit, *, owner: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": invocation,
            "ControlGroup": unit_cgroup or expected_cgroup,
            "MainPID": str(identity.pid),
        },
    )
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_identity_alive", lambda _seen: process_matches
    )
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_cgroup",
        lambda _pid: expected_cgroup if unit_cgroup is None else "/user.slice/other",
    )

    assert resolver.verify(receipt) is False


@pytest.mark.parametrize(
    "changed_identity",
    [
        ProcessIdentity(31416, 27182, "a" * 36),
        ProcessIdentity(31415, 27183, "a" * 36),
        ProcessIdentity(31415, 27182, "c" * 36),
    ],
)
def test_run_bound_lab_verification_rejects_dead_or_reused_process_identity(
    monkeypatch, changed_identity
) -> None:
    run_unit = "swapp-ai-scientist-director-dispatch-" + "2" * 32 + ".service"
    resolver = SystemdPrincipalResolver(
        {"aos": "swapp-aos-gpu-review.service", "lab": "swapp-lab-gpu-maint.service"}
    )
    receipt = PrincipalReceipt("lab", changed_identity, run_unit, "b" * 32)
    monkeypatch.setattr(
        "lab.llm.gpu_scheduler._process_identity_alive",
        lambda seen: seen == ProcessIdentity(31415, 27182, "a" * 36),
    )

    assert resolver.verify(receipt) is False


def test_submission_is_idempotent_and_rejects_changed_terms(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    _at(scheduler, 10)
    first = scheduler.submit("lab", "call-1", b"same", activation_seconds=8)
    _at(scheduler, 20)
    assert scheduler.submit("lab", "call-1", b"same", activation_seconds=8) == first
    with pytest.raises(LeaseConflict, match="different request terms"):
        scheduler.submit("lab", "call-1", b"changed", activation_seconds=8)


def test_round_robin_gives_lab_next_turn_despite_waiting_aos_ticket(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "shared.sqlite")
    _at(scheduler, 1)
    scheduler.submit("aos", "aos-0", b"0")
    scheduler.submit("lab", "lab-0", b"0")
    _at(scheduler, 2)
    aos = scheduler.try_acquire("aos", "aos-0")
    assert aos is not None and aos.owner == "aos"
    _at(scheduler, 3)
    ready = scheduler.mark_ready(aos)
    assert ready is not None
    scheduler.submit("aos", "aos-1", b"1")
    _at(scheduler, 4)
    scheduler.release(ready)
    lab = scheduler.try_acquire("lab", "lab-0")
    assert lab is not None and lab.owner == "lab"
    _finish(scheduler, lab, 5)
    aos_next = scheduler.try_acquire("aos", "aos-1")
    assert aos_next is not None and aos_next.owner == "aos"


def test_owner_queue_cap_and_unknown_principal(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    scheduler.submit("aos", "one", b"payload")
    with pytest.raises(LeaseConflict, match="already has a queued"):
        scheduler.submit("aos", "two", b"payload")
    with pytest.raises(LeaseConflict, match="unknown GPU principal"):
        scheduler.submit("other", "spoof", b"payload")


def test_deadline_phases_are_separate_and_heartbeat_cannot_extend_them(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    _at(scheduler, 100)
    scheduler.submit(
        "lab", "cold", b"model", activation_seconds=6, inference_seconds=3, total_seconds=9
    )
    _at(scheduler, 101)
    lease = scheduler.try_acquire("lab", "cold")
    assert lease is not None
    _at(scheduler, 104)
    renewed = scheduler.heartbeat(lease)
    assert renewed is not None
    assert renewed.activation_deadline == lease.activation_deadline
    assert renewed.total_deadline == lease.total_deadline
    assert renewed.heartbeat_deadline <= lease.activation_deadline
    _at(scheduler, 107)
    assert scheduler.heartbeat(renewed) is None
    assert scheduler.try_acquire("lab", "cold") is None


def test_ready_is_idempotent_only_before_expiry_and_uses_db_budget(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    _at(scheduler, 1)
    scheduler.submit("lab", "bounded", b"payload")
    lease = scheduler.try_acquire("lab", "bounded")
    assert lease is not None
    altered = replace(lease, slice_seconds=600, total_deadline=10000)
    _at(scheduler, 2)
    ready = scheduler.mark_ready(altered)
    assert ready is not None and ready.inference_deadline == 7
    _at(scheduler, 3)
    duplicate = scheduler.mark_ready(lease)
    assert duplicate is not None and duplicate.inference_deadline == 7
    _at(scheduler, 8)
    assert scheduler.mark_ready(ready) is None
    assert scheduler.recover_quarantined()


def test_mutation_requires_authenticated_lease_process(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    lease = _start(scheduler, "aos", "call", 2)
    assert lease is not None
    foreign = replace(lease, owner_identity=ProcessIdentity(1, 1, lease.owner_identity.boot_id))
    with pytest.raises(LeaseConflict, match="different service invocation"):
        scheduler.mark_ready(foreign)


def test_quarantine_blocks_handoff_until_trusted_drain(tmp_path: Path) -> None:
    drained = {"value": False}
    scheduler = _scheduler(tmp_path / "gpu.sqlite", drain=lambda _lease: drained["value"])
    lease = _start(scheduler, "lab", "held", 10)
    assert lease is not None
    _at(scheduler, 21)
    assert scheduler.try_acquire("aos", "next") is None
    assert not scheduler.recover_quarantined()
    drained["value"] = True
    assert scheduler.recover_quarantined()


def test_release_records_expired_if_competing_acquire_quarantines_activation(
    tmp_path: Path,
) -> None:
    clock = FakeClock(100)
    scheduler: SharedGpuScheduler

    def competing_drain(_lease: GpuLease) -> bool:
        clock.value = 111
        scheduler.submit("aos", "next", b"next")
        assert scheduler.try_acquire("aos", "next") is None
        return True

    scheduler = SharedGpuScheduler(
        tmp_path / "quarantined-release.sqlite",
        principal_resolver=FixturePrincipalResolver(),
        drain_verifier=competing_drain,
        max_activation_seconds=10,
        max_inference_seconds=5,
        max_total_seconds=15,
        queue_timeout_seconds=300,
        clock=clock,
    )
    scheduler.submit("lab", "owner", b"owner")
    clock.value = 101
    lease = scheduler.try_acquire("lab", "owner")
    assert lease is not None

    scheduler.release(lease)

    with sqlite3.connect(tmp_path / "quarantined-release.sqlite") as connection:
        state = connection.execute(
            "SELECT state FROM gpu_turn_requests WHERE owner='lab' AND request_id='owner'"
        ).fetchone()[0]
    assert state == "expired"


def test_stale_release_rejected_after_recovery(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path / "gpu.sqlite")
    old = _start(scheduler, "aos", "old", 1)
    assert old is not None
    _at(scheduler, 12)
    assert scheduler.try_acquire("lab", "missing") is None
    assert scheduler.recover_quarantined()
    fresh = _start(scheduler, "lab", "fresh", 13)
    assert fresh is not None and fresh.fencing_token > old.fencing_token
    with pytest.raises(LeaseConflict, match="stale or quarantined"):
        scheduler.release(old)


def test_old_boot_active_row_is_quarantined_for_verified_recovery(tmp_path: Path) -> None:
    path = tmp_path / "reboot.sqlite"
    scheduler = _scheduler(path)
    lease = _start(scheduler, "aos", "before-reboot", 1)
    assert lease is not None
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE gpu_turn_state SET owner_boot_id='old-boot-id' WHERE singleton=1"
        )
    _at(scheduler, 2)
    # A different queued owner cannot inherit directly when a persisted lease
    # belongs to an earlier kernel boot; it first enters trusted quarantine.
    assert scheduler.try_acquire("lab", "no-ticket") is None
    assert scheduler.recover_quarantined()
    fresh = _start(scheduler, "lab", "after-recovery", 3)
    assert fresh is not None


def _fair_owner(database: str, owner: str, turns: int, barrier: object, events: object) -> None:
    scheduler = _scheduler(Path(database))
    for index in range(turns):
        request = f"{owner}-{index}"
        scheduler.submit(owner, request, request.encode())
        if index == 0:
            barrier.wait(timeout=5)  # type: ignore[attr-defined]
        while True:
            lease = scheduler.try_acquire(owner, request)
            if lease is not None:
                ready = scheduler.mark_ready(lease)
                if ready is None:
                    events.put(("failed", owner, index))  # type: ignore[attr-defined]
                    return
                scheduler.release(ready)
                events.put(("acquired", owner, index))  # type: ignore[attr-defined]
                break
            time.sleep(0.002)


def test_two_cpu_processes_bound_starvation_with_aos_continuously_backlogged(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    barrier = context.Barrier(2)
    database = str(tmp_path / "two-process.sqlite")
    aos = context.Process(target=_fair_owner, args=(database, "aos", 31, barrier, events))
    lab = context.Process(target=_fair_owner, args=(database, "lab", 1, barrier, events))
    aos.start()
    lab.start()
    try:
        observed = [events.get(timeout=8) for _ in range(3)]
        assert all(item[0] == "acquired" for item in observed)
        assert next(index for index, item in enumerate(observed) if item[1] == "lab") <= 1
        assert any(item[1:] == ("aos", 1) for item in observed)
    finally:
        aos.join(timeout=3)
        lab.join(timeout=3)
        for process in (aos, lab):
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
    assert lab.exitcode == 0
    assert aos.exitcode == 0


def test_invalid_deadline_budget_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cover activation plus inference"):
        SharedGpuScheduler(
            tmp_path / "invalid.sqlite",
            principal_resolver=FixturePrincipalResolver(),
            drain_verifier=lambda _lease: True,
            max_activation_seconds=10,
            max_inference_seconds=5,
            max_total_seconds=14,
        )
