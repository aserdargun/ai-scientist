"""CPU-only guards for the unexecuted calibration031 SQL runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_RUNNER = (
    Path(__file__).parents[1]
    / "docs/ai-scientist/review-evidence/review_care_calibration031_pg.py"
)
_SPEC = importlib.util.spec_from_file_location("care_calibration031_pg_runner", _RUNNER)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)


class _Connection:
    def __init__(self, address: str):
        self.address = address

    def execute(self, _statement):
        class Result:
            def fetchone(self):
                return (
                    runner.DB_PREFIX + "fixture",
                    "swapp_lab_scorer",
                    self.address,
                    5432,
                    "2026-09-27 10:00:00+00",
                )

        result = Result()
        result.address = self.address
        return result


def test_server_identity_uses_plain_container_ip_and_expected_internal_port() -> None:
    identity = runner._server_identity(
        _Connection("172.18.0.2"),
        runner.DB_PREFIX + "fixture",
        "scorer",
        {"172.18.0.2"},
    )
    assert identity == (
        runner.DB_PREFIX + "fixture",
        "172.18.0.2",
        5432,
        "2026-09-27 10:00:00+00",
    )


def test_server_identity_rejects_postgresql_inet_text_mask() -> None:
    with pytest.raises(RuntimeError, match="exact private PostgreSQL instance"):
        runner._server_identity(
            _Connection("172.18.0.2/32"),
            runner.DB_PREFIX + "fixture",
            "scorer",
            {"172.18.0.2"},
        )


def test_database_prefix_and_migration_head_are_fixed_for_calibration031() -> None:
    assert runner.DB_PREFIX == "swapp_lab_m0_calibration_031_"
    assert runner.EXPECTED_HEAD == "0023_care_calibration_execution"
