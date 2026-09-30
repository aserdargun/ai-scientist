"""CPU-only regressions for cleanup behavior in the unexecuted lifecycle fixture."""

from __future__ import annotations

import importlib.util
import signal
import subprocess
from pathlib import Path

import pytest

from harness.fingerprint import compute_harness_hash

_FIXTURE = (
    Path(__file__).parents[1]
    / "docs/ai-scientist/review-evidence/holdout-030-lifecycle-fixture.py"
)
_SPEC = importlib.util.spec_from_file_location("holdout_030_lifecycle_fixture", _FIXTURE)
assert _SPEC is not None and _SPEC.loader is not None
fixture = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fixture)


def _completed(
    argv: list[str], returncode: int = 0, stdout: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def test_container_cleanup_uses_reserved_window_for_all_docker_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "inspect":
            return _completed(argv, stdout=f"{'a' * 64} fixture-id")
        return _completed(argv)

    monkeypatch.setattr(fixture, "_command", command)
    assert fixture._owned_container_cleanup(
        {"container_id": "a" * 64, "container_label_value": "fixture-id"}
    )
    assert [call[0][1] for call in calls] == ["inspect", "stop", "rm"]
    assert all(call[1].get("cleanup_window") is True for call in calls)


def test_container_inspect_error_requires_successful_absence_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def unavailable(argv, **kwargs):
        calls.append((argv, kwargs))
        return _completed(argv, returncode=1, stdout="")

    monkeypatch.setattr(fixture, "_command", unavailable)
    assert not fixture._owned_container_cleanup(
        {"container_id": "b" * 64, "container_label_value": "fixture-id"}
    )
    assert [call[0][1] for call in calls] == ["inspect", "ps"]
    assert all(call[1].get("cleanup_window") is True for call in calls)


def test_container_inspect_error_accepts_only_exact_successful_absence_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def absent(argv, **kwargs):
        calls.append(argv)
        return _completed(argv, returncode=1 if argv[1] == "inspect" else 0)

    monkeypatch.setattr(fixture, "_command", absent)
    assert fixture._owned_container_cleanup(
        {"container_id": "c" * 64, "container_label_value": "fixture-id"}
    )
    assert [call[1] for call in calls] == ["inspect", "ps"]


def test_generated_cleanup_scripts_compile_without_running_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    setup = private / "setup.json"
    setup.write_text('{"run_id":"00000000-0000-0000-0000-000000000001",'
                     '"score_job_ids":[]}', encoding="ascii")
    monkeypatch.setattr(
        fixture,
        "_command",
        lambda argv, **kwargs: _completed(argv),
    )
    assert fixture._cleanup_setup_workers(tmp_path, private, private / "dsns", setup) == 0
    generated = private / "cleanup-setup-workers.py"
    compile(generated.read_text(encoding="utf-8"), str(generated), "exec")
    setup_code = generated.read_text(encoding="utf-8")
    assert "_owned_unit_cgroup" in setup_code
    assert "if row is not None and row[0] != run_id" in setup_code
    assert "properties.get(\"LoadState\") == \"not-found\"" in setup_code
    assert "_cgroup_is_absent_or_empty(_expected_unit_cgroup(unit))" in setup_code
    assert fixture._cleanup_reserved_run(
        tmp_path,
        private,
        private / "dsns",
        "00000000-0000-0000-0000-000000000001",
        None,
    ) == 0
    generated = private / "cleanup-owned-run.py"
    run_cleanup_code = generated.read_text(encoding="utf-8")
    compile(run_cleanup_code, str(generated), "exec")
    assert ").first()" in run_cleanup_code
    assert "row is None and reservation_id is not None" in run_cleanup_code


def test_fixture_plugin_bounds_every_scorer_launch_by_aggregate_deadline(
    tmp_path: Path,
) -> None:
    plugin = Path(fixture._plugin_text(tmp_path)).read_text(encoding="utf-8")
    compile(plugin, "fixture_plugin.py", "exec")
    assert "HOLDOUT_FIXTURE_DEADLINE_MONOTONIC" in plugin
    assert "deadline - time.monotonic() - 45" in plugin
    assert "min(180, available)" in plugin
    assert '"worker_results": []' in plugin
    assert '"exit_code": None' in plugin
    assert 'result_document.get("state")' in plugin


def test_owned_child_reap_signals_only_its_created_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[int, int]] = []

    class Child:
        pid = 1234
        returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout):
            if self.returncode is None:
                self.returncode = -int(signal.SIGTERM)
            return "", ""

    child = Child()
    monkeypatch.setattr(fixture.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    assert fixture._stop_owned_child_group(child) is True
    assert sent == [(1234, signal.SIGCONT), (1234, signal.SIGTERM)]


def test_failure_diagnostics_are_fixed_codes_and_never_copy_exception_text() -> None:
    assert (
        fixture._safe_failure_code(
            RuntimeError("isolated 0.30 schema migration failed")
        )
        == "migration_command_failed"
    )
    secret = "password=must-not-appear postgresql://user:secret@host/db"
    assert fixture._safe_failure_code(RuntimeError(secret)) == "unclassified_RuntimeError"
    assert secret not in fixture._safe_failure_code(RuntimeError(secret))
    assert fixture._safe_failure_code(TimeoutError("anything")) == "bounded_phase_timeout"


def test_phase_only_database_ownership_does_not_trigger_container_cleanup() -> None:
    phase_only = {"stage": "postgres_image_check"}
    assert fixture._owned_database_for_cleanup(None, phase_only) == {}
    attempted = {
        "stage": "postgres_container_create",
        "creation_attempted": True,
        "container_name": "review-owned-name",
        "container_label_value": "fixture-identity",
    }
    assert fixture._owned_database_for_cleanup(None, attempted) is attempted


def test_failure_receipt_captures_bounded_pytest_output_without_credentials() -> None:
    record: dict[str, object] = {}
    fixture._record_pytest_output(
        record,
        "traceback mentions password secret-value",
        "postgresql+psycopg://scorer:secret-value@127.0.0.1/db",
        {"admin_password": "secret-value", "role_passwords": {"scorer": "other-secret"}},
    )
    assert record["fixture_pytest_stdout_truncated"] is False
    assert record["fixture_pytest_stderr_truncated"] is False
    assert "secret-value" not in str(record["fixture_pytest_stdout"])
    assert "secret-value" not in str(record["fixture_pytest_stderr"])
    assert "[REDACTED]" in str(record["fixture_pytest_stderr"])
    fixture._record_pytest_output(record, "x" * (fixture.MAX_OUTPUT + 1), "", None)
    assert record["fixture_pytest_stdout_truncated"] is True
    assert len(str(record["fixture_pytest_stdout"])) == fixture.MAX_OUTPUT


def test_pytest_log_reader_returns_only_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fixture, "MAX_OUTPUT", 16)
    log = tmp_path / "pytest.stderr"
    log.write_text("run\npassword=secret-token\npost-boundary\n", encoding="ascii")
    output, truncated = fixture._read_bounded_log(log)
    assert output == "post-boundary\n"
    assert truncated is True
    assert "secret-token" not in output
    record: dict[str, object] = {}
    fixture._record_pytest_output(
        record, output, "", None, stdout_truncated=truncated
    )
    assert record["fixture_pytest_stdout_truncated"] is True
    no_newline = tmp_path / "single-line.stderr"
    no_newline.write_text("password=secret-token-with-long-tail", encoding="ascii")
    output, truncated = fixture._read_bounded_log(no_newline)
    assert output == ""
    assert truncated is True
    link = tmp_path / "pytest-link.stderr"
    link.symlink_to(log)
    assert fixture._read_bounded_log(link) == ("", False)


def test_fixture_snapshot_matches_production_harness_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).resolve().parents[1]
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(mode=0o700)
    expected = compute_harness_hash(source).sha256
    copied = fixture._snapshot(source, snapshot)

    assert "Dockerfile.sandbox" in copied
    assert "docker/sandbox/requirements.txt" in copied
    assert compute_harness_hash(snapshot).sha256 == expected
    monkeypatch.setattr(fixture, "ROOT", source)
    assert fixture._compute_harness_fingerprint(source) == fixture._compute_harness_fingerprint(
        snapshot
    )
