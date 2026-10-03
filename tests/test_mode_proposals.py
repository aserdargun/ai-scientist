"""Offline contract regressions; runtime replies and usage are explicit fixtures."""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from uuid import UUID

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from test_local_qwen_provider import _context, _FakeRuntime, _reset_fake_runtime  # noqa: F401

from harness.contracts import FitContext
from lab.director.fake_llm import ProviderReceipt
from lab.director.local_llm import (
    LocalQwenProposalProvider,
    provider_config_sha256,
    provider_profile_config,
    provider_profile_set_for_sha256,
    provider_proposal_contract_for_sha256,
)
from lab.director.mode_proposals import MODE_SCHEMA_SHA256, OperatingModeProposal
from lab.operating_modes.candidate import OperatingModeCandidate, candidate_source
from lab.operating_modes.contracts import ModeConfig

CONTRACT = "operating-mode-config.v1"


def _payload(method="lsh", **configuration):
    return {
        "schema": "operating-mode-proposal.v1",
        "hypothesis": "Test tolerance and nearest-neighbor calibration on development tasks",
        "move_type": "hparam",
        "configuration": {"method": method, **configuration},
        "predicted_delta": 0.15,
    }


def _provider(profile_set="research"):
    return LocalQwenProposalProvider(
        run_id=UUID("7089275f-d7d2-4f46-b04b-27e809d15326"),
        owner="lab",
        principal_resolver=object(),
        runtime_database=Path("unused-no-database-created.sqlite3"),
        registry_entry_sha256="c" * 64,
        profile_set=profile_set,
        proposal_contract=CONTRACT,
    )


@pytest.mark.parametrize("method", ["lsh", "optics", "som"])
def test_strict_mode_proposal_compiles_only_trusted_source(method):
    payload = _payload(method, k=5, tolerance_multiplier=1.1, dwell=2)
    parsed = LocalQwenProposalProvider._parse_proposal(json.dumps(payload), "hparam", CONTRACT)
    config = ModeConfig.model_validate(payload["configuration"], strict=True)
    assert parsed.candidate_source.encode() == candidate_source(config)
    ast.parse(parsed.candidate_source)
    assert parsed.predicted_delta == 0.15


@pytest.mark.parametrize(
    "field,value",
    [("candidate_source", "evil()"), ("score", 1.0), ("verdict", "KEEP")],
)
def test_model_cannot_submit_code_measurements_or_verdict(field, value):
    payload = _payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        OperatingModeProposal.model_validate(payload, strict=True)


@pytest.mark.parametrize(
    "configuration",
    [
        {"method": "dbscan"},
        {"k": 0},
        {"k": True},
        {"k": 129},
        {"tolerance_multiplier": 0.0},
        {"tolerance_quantile": 1.1},
        {"alarm_quantile": float("nan")},
        {"dwell": 1001},
        {"release_ratio": 1.01},
        {"som_iterations": 10001},
        {"lsh_tables": 1, "lsh_merge_tables": 2},
        {"optics_xi": 1.0},
        {"code": "exec('evil')"},
    ],
)
def test_mode_bounds_and_no_configuration_escape(configuration):
    payload = _payload()
    payload["configuration"] = configuration
    with pytest.raises(ValidationError):
        LocalQwenProposalProvider._parse_proposal(json.dumps(payload), "hparam", CONTRACT)


def test_mode_move_schema_prediction_and_duplicates_are_checked():
    for key, value in [
        ("move_type", "detector"),
        ("schema", "operating-mode-proposal.v2"),
        ("predicted_delta", 4.01),
        ("predicted_delta", True),
    ]:
        payload = _payload()
        payload[key] = value
        with pytest.raises(ValueError):
            LocalQwenProposalProvider._parse_proposal(json.dumps(payload), "hparam", CONTRACT)
    raw = json.dumps(_payload())
    raw = raw.replace('"method": "lsh"', '"method": "lsh", "method": "som"')
    with pytest.raises(ValueError, match="repeats"):
        LocalQwenProposalProvider._parse_proposal(raw, "hparam", CONTRACT)


def test_default_research_config_pin_and_explicit_mode_pins():
    assert provider_config_sha256("research") == (
        "2177cc74fa94930fe33e4ed1855eaaf3b0321bedcdedb2e07222d6b8614338fd"
    )
    assert "proposal_contract" not in provider_profile_config("research")
    for profile_set in ("smoke", "research"):
        digest = provider_config_sha256(profile_set, CONTRACT)
        assert digest != provider_config_sha256(profile_set)
        assert provider_profile_set_for_sha256(digest, CONTRACT) == profile_set
        assert provider_proposal_contract_for_sha256(digest) == CONTRACT
        with pytest.raises(ValueError, match="unknown or ambiguous"):
            provider_profile_set_for_sha256(digest)
    assert provider_proposal_contract_for_sha256(provider_config_sha256()) == "candidate-python.v1"
    with pytest.raises(ValueError, match="unknown trusted proposal contract"):
        provider_profile_config("research", "unrecognized")


def test_mode_adapter_keeps_unavailable_omr_and_alarm_calibration_explicit():
    proposal = OperatingModeProposal.model_validate(_payload(), strict=True)
    candidate = OperatingModeCandidate(proposal.configuration)
    frame = pd.DataFrame({"constant_sensor": [1.0] * 8})
    candidate.fit(frame, FitContext(0, ("constant_sensor",), (), 1, 5.0))
    assert np.isnan(candidate.score(frame)).all()
    with pytest.raises(ValueError, match="calibration unavailable"):
        candidate.alarm_policy(candidate.score(frame))


def test_mode_s2_fallback_preserves_contract_and_measured_attempt_receipts():
    _FakeRuntime.truncate_first = True
    _FakeRuntime.response_text = json.dumps(_payload("optics"))
    turn = _provider("smoke").propose(_context(system="S2"))
    assert turn.provider_receipt.fallback_from == "S2"
    assert turn.provider_receipt.actual_system == "S1"
    assert len(turn.provider_receipt.attempts) == 2
    assert all(
        item.response_schema_sha256 == MODE_SCHEMA_SHA256 for item in turn.provider_receipt.attempts
    )
    assert turn.proposal.candidate_source.encode() == candidate_source(ModeConfig(method="optics"))


@pytest.mark.parametrize("profile_set", ["smoke", "research"])
@pytest.mark.parametrize("system", ["S1", "S2"])
def test_mode_repair_and_durable_replay_bind_all_attempts(monkeypatch, profile_set, system):
    _FakeRuntime.response_queue = [json.dumps({"score": 100}), json.dumps(_payload("som"))]
    saved = {}
    provider = _provider(profile_set)
    context = _context(system=system)
    turn = provider.propose_bounded(
        context,
        remaining_wall_seconds=200,
        attempt_save=lambda index, _request_id, payload: saved.__setitem__(index, payload),
    )
    assert turn.proposal.candidate_source.encode() == candidate_source(ModeConfig(method="som"))
    receipt = turn.provider_receipt
    assert receipt.context_template == "director.operating-mode-contract.metadata-only.v1"
    ProviderReceipt.model_validate_json(receipt.model_dump_json(by_alias=True), strict=True)
    assert all(item.response_schema_sha256 == MODE_SCHEMA_SHA256 for item in receipt.attempts)
    assert all(item.response_schema_sha256 == MODE_SCHEMA_SHA256 for item in turn.provider_attempts)
    assert all(
        "parameters only" in item.prompt_messages[0].content for item in turn.provider_attempts
    )
    assert all(
        schema_name == "operating-mode-proposal.v1"
        for schema_name, _ in _FakeRuntime.request_schemas
    )
    assert all(item["proposal_contract"] == CONTRACT for item in saved.values())
    calls = len(_FakeRuntime.calls)

    def forbid_tokenizer(*_args, **_kwargs):
        raise AssertionError("replay must not activate model or tokenizer")

    monkeypatch.setattr(provider, "_count_prompt_variants", forbid_tokenizer)
    replay = provider.propose_bounded(
        context, remaining_wall_seconds=200, attempt_load=lambda index: saved.get(index)
    )
    assert replay == turn
    assert len(_FakeRuntime.calls) == calls
    corrupted = copy.deepcopy(saved)
    corrupted[0]["proposal_contract"] = "candidate-python.v1"
    with pytest.raises(RuntimeError, match="identity differs"):
        provider.propose_bounded(
            context, remaining_wall_seconds=200, attempt_load=lambda index: corrupted.get(index)
        )
    with pytest.raises(RuntimeError, match="identity differs"):
        LocalQwenProposalProvider(
            run_id=provider.run_id,
            owner="lab",
            principal_resolver=object(),
            runtime_database=provider.runtime_database,
            registry_entry_sha256="c" * 64,
            profile_set=profile_set,
        ).propose_bounded(
            context, remaining_wall_seconds=200, attempt_load=lambda index: saved.get(index)
        )
