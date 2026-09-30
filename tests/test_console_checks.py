from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from console import checks
from console.checks import CheckManager


def test_cpu_check_command_is_fixed_and_systemd_bounded() -> None:
    args = checks.command()
    text = " ".join(args)

    assert "--shell" not in text
    assert "MemoryMax=1G" in text
    assert "MemorySwapMax=0" in text
    assert "CPUQuota=50%" in text
    assert "TasksMax=64" in text
    assert "RuntimeMaxSec=120" in text
    assert all(test_path in args for test_path in checks.TESTS)
    assert "pytest" in args


def test_only_one_check_can_be_active(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = CheckManager()
    monkeypatch.setattr(manager, "_run", lambda check_id: None)
    first = manager.submit(BackgroundTasks())
    with pytest.raises(HTTPException) as error:
        manager.submit(BackgroundTasks())
    assert error.value.status_code == 409
    assert manager.get(first["id"])["state"] == "queued"


def test_smoke_result_preserves_nonzero_exit_and_bounds_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = CheckManager()
    manager.items["check"] = {"id": "check", "state": "queued", "output": ""}
    manager.active_id = "check"
    monkeypatch.setattr(checks, "_desktop_bus_environment", lambda: {})
    monkeypatch.setattr(checks, "command", lambda: ["fixed"])
    monkeypatch.setattr(
        checks.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=7, stdout="out", stderr="err"),
    )

    manager._run("check")
    result = manager.get("check")
    assert result["state"] == "failed"
    assert result["exit_code"] == 7
    assert result["output"] == "outerr"


def test_output_tail_is_limited_with_truncation_marker() -> None:
    result = checks._trim_output("x" * 40_000, "tail")
    assert len(result.encode()) <= checks.MAX_OUTPUT_BYTES + len("[earlier output truncated]\n")
    assert result.startswith("[earlier output truncated]\n")
    assert result.endswith("tail")
