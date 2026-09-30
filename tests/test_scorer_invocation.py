"""Fail-closed unit-generation boundaries that run before database access."""

from __future__ import annotations

from uuid import uuid4

import pytest

from lab.scorer.jobs import claim_score_job
from lab.scorer.supervisor import stop_owned_scorer_unit
from lab.scorer.worker import verify_systemd_invocation


def test_worker_requires_a_systemd_invocation_before_starting(monkeypatch) -> None:
    job_id = uuid4()
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    with pytest.raises(RuntimeError, match="systemd invocation"):
        verify_systemd_invocation(f"swapp-ai-scientist-scorer-{job_id.hex}.service")


def test_worker_rejects_a_unit_outside_the_scorer_name_space(monkeypatch) -> None:
    monkeypatch.setenv("INVOCATION_ID", "a" * 32)
    with pytest.raises(RuntimeError, match="unit name"):
        verify_systemd_invocation("attacker.service")


def test_claim_rejects_wrong_unit_or_generation_before_db_access() -> None:
    job_id = uuid4()
    with pytest.raises(ValueError, match="exact owned systemd"):
        claim_score_job(
            object(),
            job_id=job_id,
            claim_unit="somewhere-else.service",
            claim_invocation_id="a" * 32,
        )
    with pytest.raises(ValueError, match="exact owned systemd"):
        claim_score_job(
            object(),
            job_id=job_id,
            claim_unit=f"swapp-ai-scientist-scorer-{job_id.hex}.service",
            claim_invocation_id="A" * 32,
        )


def test_drain_rejects_malformed_invocation_before_manager_access() -> None:
    with pytest.raises(ValueError, match="lowercase 128-bit"):
        stop_owned_scorer_unit(uuid4(), invocation_id="bad")
