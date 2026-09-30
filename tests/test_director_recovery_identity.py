"""Unit coverage for recovery owner-generation identity and drain fencing."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from lab.director import recovery
from lab.director.ledger import canonical_json_bytes


class _HistoricalDocument(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: str
    later_optional: str | None = None


def test_historical_canonical_document_keeps_absent_optional_field_absent() -> None:
    payload = b'{"schema_version":"v1"}'

    document = recovery._validate_canonical_document(payload, _HistoricalDocument)

    assert document.schema_version == "v1"
    assert document.later_optional is None
    assert canonical_json_bytes(document) == b'{"later_optional":null,"schema_version":"v1"}'
    assert payload != canonical_json_bytes(document)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"v1", "later_optional":null}',
        b'{"schema_version":"v1","schema_version":"v1"}',
        b'{"schema_version":NaN}',
    ],
)
def test_historical_document_still_requires_unique_canonical_finite_json(payload: bytes) -> None:
    with pytest.raises((ValueError, UnicodeDecodeError)):
        recovery._validate_canonical_document(payload, _HistoricalDocument)


def test_capture_uses_actual_cgroup_when_systemd_unit_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    cgroup = f"/user.slice/user-1000.slice/{unit}"
    monkeypatch.delenv("SYSTEMD_UNIT", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    monkeypatch.setattr(recovery.os, "getpid", lambda: 4321)
    monkeypatch.setattr(recovery, "_current_process_start_ticks", lambda pid: 12345)
    monkeypatch.setattr(recovery, "_current_cgroup", lambda: cgroup)
    monkeypatch.setattr(recovery, "_current_boot_id", lambda: "b" * 36)
    monkeypatch.setattr(
        recovery,
        "_systemctl_show",
        lambda requested: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "a" * 32,
            "ControlGroup": cgroup,
            "MainPID": "4321",
        },
    )

    receipt = recovery.capture_current_owner("c" * 64, run_id)

    assert receipt.worker_pid == 4321
    assert receipt.worker_start_ticks == 12345
    assert receipt.worker_unit == unit
    assert receipt.worker_cgroup == cgroup


def test_transient_dispatch_unit_is_bound_to_the_run() -> None:
    run_id = uuid4()
    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"

    assert recovery._valid_owner_unit(unit, run_id)
    assert not recovery._valid_owner_unit(unit, uuid4())
    assert recovery._valid_owner_unit(recovery.OWNER_DRAIN_UNIT, run_id)
    assert not recovery._valid_owner_unit("arbitrary.service", run_id)


def test_active_replacement_unit_blocks_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    owner = recovery.OwnerGeneration(
        payload_sha256="c" * 64,
        worker_pid=4321,
        worker_start_ticks=12345,
        worker_boot_id="b" * 36,
        worker_unit=recovery.OWNER_DRAIN_UNIT,
        worker_invocation_id="a" * 32,
        worker_cgroup=f"/user.slice/{recovery.OWNER_DRAIN_UNIT}",
    )
    monkeypatch.setattr(recovery, "_current_boot_id", lambda: owner.worker_boot_id)
    monkeypatch.setattr(recovery, "_current_process_start_ticks", lambda pid: None)
    monkeypatch.setattr(
        recovery,
        "_systemctl_show",
        lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "InvocationID": "d" * 32,
            "ControlGroup": owner.worker_cgroup,
            "MainPID": "9876",
        },
    )

    with pytest.raises(recovery.RecoveryPending, match="active generation"):
        recovery._owner_is_proven_dead(owner, run_id)


def test_inactive_unit_requires_recursive_cgroup_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    owner = recovery.OwnerGeneration(
        payload_sha256="c" * 64,
        worker_pid=4321,
        worker_start_ticks=12345,
        worker_boot_id="b" * 36,
        worker_unit=recovery.OWNER_DRAIN_UNIT,
        worker_invocation_id="a" * 32,
        worker_cgroup=f"/user.slice/{recovery.OWNER_DRAIN_UNIT}",
    )
    monkeypatch.setattr(recovery, "_current_boot_id", lambda: owner.worker_boot_id)
    monkeypatch.setattr(recovery, "_current_process_start_ticks", lambda pid: None)
    monkeypatch.setattr(
        recovery,
        "_systemctl_show",
        lambda unit: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "InvocationID": owner.worker_invocation_id,
            "ControlGroup": owner.worker_cgroup,
            "MainPID": "0",
        },
    )
    monkeypatch.setattr(recovery, "_cgroup_proven_empty", lambda group: False)

    with pytest.raises(recovery.RecoveryPending, match="still contains a process"):
        recovery._owner_is_proven_dead(owner, run_id)

    monkeypatch.setattr(recovery, "_cgroup_proven_empty", lambda group: True)
    assert recovery._owner_is_proven_dead(owner, run_id)
