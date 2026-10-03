from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

import numpy as np
import pandas as pd
import pytest

from harness.contracts import ADPipeline, AlarmPolicy, FitContext
from lab.director import runner
from lab.director.fake_llm import AgentContext, ProviderReceipt
from lab.director.local_llm import (
    _CANDIDATE_CONTRACT_EXAMPLE,
    _CANDIDATE_PROPOSAL_SCHEMA,
    _CANDIDATE_SCHEMA_NAME,
    _LOCAL_CANDIDATE_SOURCE_MAX_LENGTH,
    _LOCAL_HYPOTHESIS_MAX_LENGTH,
    _PROMPT_TEMPLATE,
    LocalQwenProposalProvider,
    provider_config_sha256,
    provider_profile_config,
    provider_profile_set_for_sha256,
)
from lab.llm import native_runtime


def _context(*, system: str = "S1", move_type: str = "hparam") -> AgentContext:
    return AgentContext(
        phase="proposal",
        experiment_number=1,
        system=system,
        move_type=move_type,
        task_cards=("task=t1; family=EVT; measured_dev_vus=0.1",),
        champion_source="def build_candidate():\n    return None\n",
        recent_feedback=(),
    )


def _proposal_json(move_type: str = "hparam") -> str:
    return (
        '{"hypothesis":"Adjust threshold scaling",'
        f'"move_type":"{move_type}",'
        '"candidate_source":"def build_candidate():\\n    return None\\n",'
        '"predicted_delta":0.1}'
    )


class _FakeRuntime:
    calls: list[tuple[object, str]] = []
    pins: list[object] = []
    request_schemas: list[tuple[str | None, object | None]] = []
    request_output_limits: list[int | None] = []
    truncate_first = False
    response_text = _proposal_json()
    response_queue: list[str] = []
    completion_queue: list[int] = []
    fail_systems: set[str] = set()

    def __init__(
        self,
        database: Path,
        *,
        principal_resolver,
        pin,
        profile,
        activation_seconds,
        inference_seconds,
        total_seconds,
    ) -> None:
        self.profile = profile
        self.pin = pin
        self._last_response_metadata = None
        type(self).calls.append((profile, "constructed"))
        type(self).pins.append(pin)

    def run_turn(
        self,
        owner,
        request_id,
        messages,
        *,
        enable_thinking,
        profile,
        response_schema_name=None,
        response_schema=None,
        output_token_limit=None,
    ):
        assert owner == "lab"
        assert profile == self.profile
        assert messages[0]["role"] == "system"
        assert "measured_dev_vus" in messages[1]["content"]
        type(self).request_schemas.append((response_schema_name, response_schema))
        type(self).request_output_limits.append(output_token_limit)
        if profile.system in type(self).fail_systems:
            raise TimeoutError("fixture model timeout")
        if type(self).truncate_first and profile.system == "S2":
            type(self).truncate_first = False
            self._last_response_metadata = {
                "prompt_tokens": 90,
                "completion_tokens": profile.max_output_tokens,
            }
            raise native_runtime.ModelOutputBudgetExceeded("bounded output exhausted")
        response = (
            type(self).response_queue.pop(0)
            if type(self).response_queue
            else type(self).response_text
        )
        completion_tokens = (
            type(self).completion_queue.pop(0) if type(self).completion_queue else 50
        )
        return native_runtime.ModelReply(
            response,
            100,
            completion_tokens,
            native_runtime.ModelMeasurements(1.0, 0.5, 0.1, 1000, 900, 62, 62, 1000),
        )

    def unit_identity_receipt(self, owner, request_id):
        return {
            "unit": f"swapp-lab-gpu-turn-{request_id}.service",
            "invocation_id": "a" * 32,
            "main_pid": 1234,
            "main_start_ticks": 99,
            "control_group": "/user.slice/swapp-gpu.slice/test.service",
            "model_sha256": "b" * 64,
        }


@pytest.fixture(autouse=True)
def _reset_fake_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRuntime.calls = []
    _FakeRuntime.pins = []
    _FakeRuntime.request_schemas = []
    _FakeRuntime.request_output_limits = []
    _FakeRuntime.truncate_first = False
    _FakeRuntime.response_text = _proposal_json()
    _FakeRuntime.response_queue = []
    _FakeRuntime.completion_queue = []
    _FakeRuntime.fail_systems = set()
    monkeypatch.setattr("lab.director.local_llm.OwnedVllmRuntime", _FakeRuntime)
    monkeypatch.setattr(
        LocalQwenProposalProvider,
        "_count_prompt_variants",
        lambda _self, variants, _profile, **_kwargs: tuple(100 for _ in variants),
    )


def _provider(
    *, profile_set: Literal["smoke", "research"] = "smoke"
) -> LocalQwenProposalProvider:
    return LocalQwenProposalProvider(
        run_id=UUID("7089275f-d7d2-4f46-b04b-27e809d15326"),
        owner="lab",
        principal_resolver=object(),
        runtime_database=Path("private-arbiter.sqlite3"),
        registry_entry_sha256="c" * 64,
        profile_set=profile_set,
    )


def test_local_provider_emits_strict_proposal_and_host_receipt() -> None:
    provider = _provider()
    turn = provider.propose(_context())
    assert turn.proposal.move_type == "hparam"
    assert turn.input_tokens == 100
    assert turn.output_tokens == 50
    assert turn.provider_receipt is not None
    assert turn.provider_receipt.model_sha256 == provider.model_pin.digest
    assert _FakeRuntime.pins == [provider.model_pin]
    assert turn.provider_receipt.provider_registry_entry_sha256 == "c" * 64
    assert turn.provider_receipt.attempts[0].outcome == "completed"
    assert turn.provider_receipt.prompt_sha256 == turn.provider_receipt.attempts[0].prompt_sha256
    assert len(turn.messages) == 3
    assert type(turn.provider_receipt).model_validate_json(
        turn.provider_receipt.model_dump_json(), strict=True
    ) == turn.provider_receipt
    historical_receipt = turn.provider_receipt.model_dump(mode="python")
    historical_receipt["context_template"] = "director.candidate-contract.metadata-only.v2"
    historical_attempts = historical_receipt["attempts"]
    assert isinstance(historical_attempts, tuple)
    historical_attempts[0]["response_schema_sha256"] = "d" * 64
    historical = ProviderReceipt.model_validate(historical_receipt, strict=True)
    assert historical.context_template.endswith(".v2")
    assert historical.attempts[0].response_schema_sha256 == "d" * 64
    assert type(turn).model_validate_json(turn.model_dump_json(), strict=True) == turn
    request = (
        {"role": "system", "content": turn.messages[0]},
        {"role": "user", "content": turn.messages[1]},
    )
    assert "def fit(self, train: pandas.DataFrame, ctx: FitContext)" in request[0]["content"]
    assert "score must return one finite float per input row" in request[0]["content"]
    assert "sampling_s:int|None" in request[0]["content"]
    assert "never infer seconds" in request[0]["content"]
    assert '"move_type":"hparam"' in request[1]["content"]


def test_local_provider_uses_one_repair_then_rejects_duplicate_keys_and_move_spoof() -> None:
    provider = _provider()
    _FakeRuntime.response_text = _proposal_json().replace(
        '"predicted_delta":0.1', '"predicted_delta":0.1,"move_type":"features"'
    )
    _FakeRuntime.response_queue = [_FakeRuntime.response_text, _FakeRuntime.response_text]
    with pytest.raises(ValueError, match="one schema-constrained repair") as caught:
        provider.propose(_context())
    assert tuple(item.attempt_kind for item in caught.value.provider_receipt.attempts) == (
        "proposal",
        "repair",
    )

    _FakeRuntime.response_text = _proposal_json("features")
    _FakeRuntime.response_queue = [_proposal_json("features"), _proposal_json("features")]
    with pytest.raises(ValueError, match="one schema-constrained repair"):
        provider.propose(_context(move_type="hparam"))


def test_local_provider_repairs_fenced_json_once_with_full_usage_and_transcripts() -> None:
    _FakeRuntime.response_queue = [
        "```json\n" + _proposal_json() + "\n```",
        _proposal_json(),
    ]
    turn = _provider().propose(_context())
    assert turn.proposal.move_type == "hparam"
    assert turn.input_tokens == 200
    assert turn.output_tokens == 100
    assert tuple(item.attempt_kind for item in turn.provider_receipt.attempts) == (
        "proposal",
        "repair",
    )
    assert tuple(item.attempt_kind for item in turn.provider_attempts) == (
        "proposal",
        "repair",
    )
    assert "```json" in turn.provider_attempts[0].response_text
    assert turn.provider_attempts[1].response_text == _proposal_json()
    assert all(
        item.response_schema_sha256 == turn.provider_receipt.attempts[index].response_schema_sha256
        for index, item in enumerate(turn.provider_attempts)
    )
    assert all(name == _CANDIDATE_SCHEMA_NAME for name, _ in _FakeRuntime.request_schemas)


def test_repair_output_limit_subtracts_primary_usage_from_episode_cap() -> None:
    _FakeRuntime.response_queue = [
        "```json\n" + _proposal_json() + "\n```",
        _proposal_json(),
    ]
    _FakeRuntime.completion_queue = [2_044, 4]
    turn = _provider().propose(_context())
    assert turn.provider_receipt is not None
    assert _FakeRuntime.request_output_limits == [2_048, 4]
    assert turn.output_tokens == sum(
        item.completion_tokens or 0 for item in turn.provider_receipt.attempts
    ) == 2_048


def test_s2_output_exhaustion_falls_back_once_and_records_both_usages() -> None:
    _FakeRuntime.truncate_first = True
    _FakeRuntime.response_text = _proposal_json("features")
    turn = _provider().propose(_context(system="S2", move_type="features"))
    receipt = turn.provider_receipt
    assert receipt is not None
    assert receipt.fallback_from == "S2"
    assert receipt.attempted_profile_ids == (
        "qwen3.5-9b-fp8-per-tensor.s2-bounded-smoke.v2",
        "qwen3.5-9b-fp8-per-tensor.s1-bounded-smoke.v1",
    )
    assert tuple(item.outcome for item in receipt.attempts) == (
        "output_budget_exhausted",
        "completed",
    )
    assert turn.input_tokens == 190
    assert turn.output_tokens == 4_146
    assert receipt.sampling_top_k is None
    assert tuple(item.sampling_top_k for item in receipt.attempts) == (20, None)


def test_fallback_failure_preserves_first_attempt_and_marks_usage_incomplete() -> None:
    _FakeRuntime.truncate_first = True
    _FakeRuntime.fail_systems = {"S1"}
    with pytest.raises(ValueError, match="fallback request failed") as caught:
        _provider().propose(_context(system="S2", move_type="features"))
    receipt = caught.value.provider_receipt
    assert tuple(item.outcome for item in receipt.attempts) == (
        "output_budget_exhausted",
        "failed",
    )
    assert receipt.attempts[1].failure_type == "TimeoutError"
    assert receipt.input_tokens == 90
    assert receipt.output_tokens == native_runtime.LOCAL_SMOKE_S2_PROFILE.max_output_tokens


def test_fallback_preflight_failure_keeps_the_observed_s2_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeRuntime.truncate_first = True
    original = LocalQwenProposalProvider._count_prompt_variants
    calls = 0

    def fail_on_s1_preflight(self, variants, profile, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise TimeoutError("fallback tokenizer deadline")
        return original(self, variants, profile, **kwargs)

    monkeypatch.setattr(
        LocalQwenProposalProvider, "_count_prompt_variants", fail_on_s1_preflight
    )
    with pytest.raises(ValueError, match="fallback failed before model activation") as caught:
        _provider().propose(_context(system="S2", move_type="features"))
    receipt = caught.value.provider_receipt
    assert len(receipt.attempts) == 1
    assert receipt.attempts[0].outcome == "output_budget_exhausted"
    assert receipt.failure_profile_id == "qwen3.5-9b-fp8-per-tensor.s1-bounded-smoke.v1"
    assert receipt.failure_type == "TimeoutError"
    assert receipt.input_tokens == 90
    assert receipt.output_tokens == native_runtime.LOCAL_SMOKE_S2_PROFILE.max_output_tokens
    assert caught.value.reason_code == "budget_exhausted"
    assert len(_FakeRuntime.calls) == 1


def test_prompt_collector_discards_old_feedback_before_required_context() -> None:
    context = _context().model_copy(
        update={"recent_feedback": tuple(f"feedback-{i}-" + "x" * 1_000 for i in range(30))}
    )
    messages = LocalQwenProposalProvider._messages(
        context, native_runtime.LOCAL_SMOKE_S1_PROFILE
    )
    payload = messages[1]["content"]
    assert "feedback-29-" in payload
    assert "feedback-0-" not in payload
    assert "champion_source" in payload
    assert sum(len(item["content"].encode("utf-8")) for item in messages) <= (
        native_runtime.LOCAL_SMOKE_S1_PROFILE.max_context_tokens * 8
    )


def test_compact_local_schema_is_versioned_and_applied_to_primary_and_repair() -> None:
    config = provider_profile_config()
    properties = _CANDIDATE_PROPOSAL_SCHEMA["properties"]
    assert _PROMPT_TEMPLATE == "director.candidate-contract.metadata-only.v6"
    assert _CANDIDATE_SCHEMA_NAME == "candidate-proposal-local-multiline-python.v3"
    assert properties["hypothesis"]["maxLength"] == _LOCAL_HYPOTHESIS_MAX_LENGTH == 384
    assert "minLength" not in properties["candidate_source"]
    assert "maxLength" not in properties["candidate_source"]
    assert _LOCAL_CANDIDATE_SOURCE_MAX_LENGTH == 6_000
    assert config["prompt_template"] == _PROMPT_TEMPLATE
    assert config["output_schema"] == "candidate-proposal.local-multiline-python.v3"
    assert config["output_schema_sha256"] != ""
    assert config["output_limits"] == {
        "hypothesis_characters": 384,
        "candidate_source_characters": 6_000,
    }
    assert config["repair_policy"] == "one-tokenizer-bounded-schema-and-python-syntax-repair.v4"
    _FakeRuntime.response_queue = [
        "```json\n" + _proposal_json() + "\n```",
        _proposal_json(),
    ]
    turn = _provider().propose(_context())
    assert len(_FakeRuntime.request_schemas) == 2
    assert all(schema == _CANDIDATE_PROPOSAL_SCHEMA for _, schema in _FakeRuntime.request_schemas)
    assert all(name == _CANDIDATE_SCHEMA_NAME for name, _ in _FakeRuntime.request_schemas)
    assert turn.provider_receipt.context_template == _PROMPT_TEMPLATE
    prompt = turn.provider_attempts[0].prompt_messages[0].content
    assert "1..6000 characters" in prompt
    assert "escaped JSON \\n sequences" in prompt
    assert "multi-line Python" in prompt
    assert "no markdown, code fences" in prompt


def test_prompt_example_preserves_multisensor_fit_state_and_causal_row_scores() -> None:
    # This executes the fixed, trusted prompt example, never generated candidate text.
    namespace: dict[str, object] = {}
    exec(compile(_CANDIDATE_CONTRACT_EXAMPLE, "<trusted-contract-example>", "exec"), namespace)
    factory = namespace["build_candidate"]
    assert callable(factory)
    pipeline = factory()
    assert isinstance(pipeline, ADPipeline)
    train = pd.DataFrame({"a": [1.0, 3.0, 5.0, 7.0], "b": [9.0, 5.0, 3.0, 1.0]})
    pipeline.fit(train, FitContext(0, ("b", "a"), (), 1, 60.0))
    evaluation = pd.DataFrame({"a": [2.0, 4.0, 20.0], "b": [6.0, 3.0, 50.0]})
    full = pipeline.score(evaluation)
    assert full.shape == (len(evaluation),)
    assert np.isfinite(full).all()
    assert np.unique(full).size > 1
    np.testing.assert_array_equal(pipeline.score(evaluation.iloc[:2]), full[:2])
    changed_future = evaluation.copy()
    changed_future.iloc[2] = [1e6, -1e6]
    np.testing.assert_array_equal(pipeline.score(changed_future)[:2], full[:2])
    np.testing.assert_array_equal(pipeline.score(evaluation), full)
    policy = pipeline.alarm_policy(pipeline.score(train))
    assert isinstance(policy, AlarmPolicy)
    assert policy.release <= policy.threshold and policy.dwell >= 1


def test_fit_context_allows_only_positive_sampling_or_unknown_cadence_none() -> None:
    assert FitContext(0, ("x",), (), None, 60.0).sampling_s is None
    assert FitContext(0, ("x",), (), 1, 60.0).sampling_s == 1
    for invalid in (True, 0, -1):
        with pytest.raises(ValueError, match="positive integer or None"):
            FitContext(0, ("x",), (), invalid, 60.0)  # type: ignore[arg-type]


def test_research_profiles_are_explicitly_digest_selected_and_budgeted() -> None:
    s1 = native_runtime.LOCAL_RESEARCH_S1_PROFILE
    s2 = native_runtime.LOCAL_RESEARCH_S2_PROFILE
    assert (s1.max_context_tokens, s1.max_output_tokens, s1.model_max_len) == (
        8_192,
        2_048,
        10_240,
    )
    assert s1.max_context_tokens + s1.max_output_tokens == s1.model_max_len
    assert (s2.max_context_tokens, s2.max_output_tokens, s2.model_max_len) == (
        8_192,
        8_192,
        16_384,
    )
    assert s2.max_context_tokens + s2.max_output_tokens == s2.model_max_len
    assert native_runtime.MODEL_TURN_PROFILES[s1.profile_id] == s1
    assert native_runtime.MODEL_TURN_PROFILES[s2.profile_id] == s2

    research_config = provider_profile_config("research")
    assert research_config["profile_set"] == "bounded-research"
    assert research_config["episode_token_budget"] == {
        "input_tokens": 16_384,
        "output_tokens_by_system": {"S1": 2_048, "S2": 8_192},
        "total_model_tokens_by_system": {"S1": 18_432, "S2": 24_576},
        "repair_uses_same_episode_budget": True,
    }
    assert provider_profile_set_for_sha256(provider_config_sha256("smoke")) == "smoke"
    assert provider_profile_set_for_sha256(provider_config_sha256("research")) == "research"
    assert provider_config_sha256("smoke") != provider_config_sha256("research")

    _FakeRuntime.response_text = _proposal_json("features")
    turn = _provider(profile_set="research").propose(_context(system="S2", move_type="features"))
    assert turn.provider_receipt is not None
    assert turn.provider_receipt.profile_id == s2.profile_id
    assert turn.provider_receipt.provider_config_sha256 == provider_config_sha256("research")


def test_4259_token_public_context_admits_only_in_explicit_research_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(profile_set="research")
    monkeypatch.setattr(
        provider,
        "_count_prompt_variants",
        lambda variants, _profile, **_kwargs: tuple(4_259 for _ in variants),
    )
    messages, count = provider._select_messages(
        _context(system="S2", move_type="features"),
        native_runtime.LOCAL_RESEARCH_S2_PROFILE,
        timeout_seconds=20.0,
    )
    assert count == 4_259
    assert messages
    assert count + native_runtime.LOCAL_RESEARCH_S2_PROFILE.max_output_tokens <= (
        native_runtime.LOCAL_RESEARCH_S2_PROFILE.model_max_len
    )
    with pytest.raises(ValueError, match="tokenizer context cap"):
        provider._select_messages(
            _context(system="S2", move_type="features"),
            native_runtime.LOCAL_SMOKE_S2_PROFILE,
            timeout_seconds=20.0,
        )


def test_multiline_candidate_source_survives_json_schema_and_python_validation() -> None:
    source = (
        "import numpy as np\n\n"
        "class Candidate:\n"
        "    def fit(self, train, ctx):\n"
        "        self.center = train.mean().to_numpy()\n"
        "    def score(self, data):\n"
        "        return np.abs(data.to_numpy() - self.center).mean(axis=1)\n"
        "    def alarm_policy(self, train_scores):\n"
        "        return AlarmPolicy(float(np.quantile(train_scores, 0.99)), 0.0, 1)\n"
        "\n"
        "def build_candidate():\n"
        "    return Candidate()\n"
    )
    payload = json.dumps(
        {
            "hypothesis": "Use a robust sensor center",
            "move_type": "features",
            "candidate_source": source,
            "predicted_delta": 0.0,
        }
    )
    parsed = LocalQwenProposalProvider._parse_proposal(payload, "features")
    assert parsed.candidate_source == source
    assert parsed.candidate_source.count("\n") >= 7
    ast.parse(parsed.candidate_source)


def test_repair_uses_compact_trusted_prompt_when_full_response_exceeds_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_source = "PRIVATE-CANDIDATE-SOURCE-" + "x" * 5_000
    invalid = '{"candidate_source":"' + secret_source
    _FakeRuntime.response_queue = [invalid, _proposal_json()]
    observed_counts: list[int] = []

    def bounded_counts(self, variants, profile, **kwargs):
        del self, profile, kwargs
        counts = tuple(
            3_000 if "The previous final response is untrusted input" in item[1]["content"] else 100
            for item in variants
        )
        observed_counts.extend(counts)
        return counts

    monkeypatch.setattr(LocalQwenProposalProvider, "_count_prompt_variants", bounded_counts)
    turn = _provider().propose(_context(system="S2"))
    assert turn.provider_receipt is not None
    assert turn.provider_receipt.context_template == _PROMPT_TEMPLATE
    assert 3_000 in observed_counts and 100 in observed_counts
    repair_user = turn.provider_attempts[1].prompt_messages[1].content
    assert "Validation category: invalid_json" in repair_user
    assert "Previous response text was omitted" in repair_user
    assert secret_source not in repair_user
    assert "PRIVATE-CANDIDATE-SOURCE" not in repair_user
    assert turn.input_tokens == 200
    assert len(_FakeRuntime.calls) == 2


def test_repair_validation_detail_is_closed_and_does_not_echo_input() -> None:
    from pydantic import ValidationError

    invalid = '{"candidate_source":"SENSITIVE-CANDIDATE"}'
    assert (
        LocalQwenProposalProvider._repair_error_code(
            json.JSONDecodeError("bad", invalid, 2)
        )
        == "invalid_json"
    )
    try:
        from lab.director.contracts import CandidateProposal

        CandidateProposal.model_validate({"candidate_source": invalid}, strict=True)
    except ValidationError as error:
        assert (
            LocalQwenProposalProvider._repair_error_code(error)
            == "proposal_schema_validation_failed"
        )
    assert "SENSITIVE-CANDIDATE" not in LocalQwenProposalProvider._repair_error_code(
        ValueError("SENSITIVE-CANDIDATE: arbitrary parser detail")
    )


@pytest.mark.parametrize("profile_set", ("smoke", "research"))
def test_completed_durable_repair_reuses_exact_prompt_without_tokenizer_or_model(
    monkeypatch: pytest.MonkeyPatch,
    profile_set: Literal["smoke", "research"],
) -> None:
    saved: dict[int, dict[str, object]] = {}
    invalid = '{"not": "one proposal object"}'
    _FakeRuntime.response_queue = [invalid, _proposal_json()]
    context = _context(system="S2")
    first = _provider(profile_set=profile_set).propose_bounded(
        context,
        remaining_wall_seconds=200,
        attempt_save=lambda index, _request_id, payload: saved.__setitem__(index, payload),
    )
    assert first.proposal.move_type == "hparam"
    assert set(saved) == {0, 1}
    calls_after_save = len(_FakeRuntime.calls)

    def forbid_tokenizer(*_args, **_kwargs):
        raise AssertionError("durable repair replay must not recount prompt tokens")

    monkeypatch.setattr(LocalQwenProposalProvider, "_count_prompt_variants", forbid_tokenizer)
    replayed = _provider(profile_set=profile_set).propose_bounded(
        context,
        remaining_wall_seconds=200,
        attempt_load=lambda index: saved.get(index),
    )
    assert replayed.proposal == first.proposal
    assert replayed.provider_receipt == first.provider_receipt
    assert len(_FakeRuntime.calls) == calls_after_save


def test_compact_local_parser_enforces_hypothesis_and_full_source_limits() -> None:
    base = {
        "hypothesis": "h" * _LOCAL_HYPOTHESIS_MAX_LENGTH,
        "move_type": "hparam",
        "candidate_source": "pass\n#"
        + "#" * (_LOCAL_CANDIDATE_SOURCE_MAX_LENGTH - len("pass\n#")),
        "predicted_delta": 0.0,
    }
    accepted = LocalQwenProposalProvider._parse_proposal(json.dumps(base), "hparam")
    assert len(accepted.hypothesis) == _LOCAL_HYPOTHESIS_MAX_LENGTH
    assert len(accepted.candidate_source) == _LOCAL_CANDIDATE_SOURCE_MAX_LENGTH
    for field, value in (
        ("hypothesis", "h" * (_LOCAL_HYPOTHESIS_MAX_LENGTH + 1)),
        ("candidate_source", "x" * (_LOCAL_CANDIDATE_SOURCE_MAX_LENGTH + 1)),
    ):
        invalid = {**base, field: value}
        with pytest.raises(ValueError, match="local compact schema bound"):
            LocalQwenProposalProvider._parse_proposal(json.dumps(invalid), "hparam")


def test_local_provider_repairs_json_valid_python_syntax_error_once_with_same_budget() -> None:
    invalid_primary = json.loads(_proposal_json())
    invalid_primary["candidate_source"] = "def build_candidate(:\n    return None\n"
    _FakeRuntime.response_queue = [json.dumps(invalid_primary), _proposal_json()]
    _FakeRuntime.completion_queue = [2_044, 4]

    turn = _provider().propose(_context())

    assert turn.proposal.candidate_source == "def build_candidate():\n    return None\n"
    assert turn.input_tokens == 200
    assert turn.output_tokens == 2_048
    assert tuple(item.attempt_kind for item in turn.provider_receipt.attempts) == (
        "proposal",
        "repair",
    )
    assert len(_FakeRuntime.request_schemas) == 2
    assert all(schema == _CANDIDATE_PROPOSAL_SCHEMA for _, schema in _FakeRuntime.request_schemas)
    assert _FakeRuntime.request_output_limits == [2_048, 4]
    assert "python_syntax_error_line_" in turn.messages[1]
    assert all(
        attempt.response_schema_sha256
        == turn.provider_receipt.attempts[index].response_schema_sha256
        for index, attempt in enumerate(turn.provider_attempts)
    )


def test_local_provider_rejects_python_syntax_error_after_one_repair() -> None:
    invalid_source = "def build_candidate(:\n    # private fixture source\n"
    invalid_primary = json.loads(_proposal_json())
    invalid_repair = json.loads(_proposal_json())
    invalid_primary["candidate_source"] = invalid_source
    invalid_repair["candidate_source"] = "def build_candidate(:\n    return None\n"
    _FakeRuntime.response_queue = [json.dumps(invalid_primary), json.dumps(invalid_repair)]

    with pytest.raises(ValueError, match="one schema-constrained repair") as caught:
        _provider().propose(_context())

    assert caught.value.reason_code == "tool_parse"
    assert tuple(item.attempt_kind for item in caught.value.provider_receipt.attempts) == (
        "proposal",
        "repair",
    )
    assert len(_FakeRuntime.request_schemas) == 2
    assert invalid_source not in str(caught.value)
    assert "private fixture source" not in str(caught.value)
    assert _FakeRuntime.response_queue == []


def test_repair_parser_rejects_source_above_compact_bound() -> None:
    oversized = json.dumps(
        {
            "hypothesis": "concise hypothesis",
            "move_type": "hparam",
            "candidate_source": "x" * (_LOCAL_CANDIDATE_SOURCE_MAX_LENGTH + 1),
            "predicted_delta": 0.0,
        }
    )
    _FakeRuntime.response_queue = ["```json\n" + _proposal_json() + "\n```", oversized]

    with pytest.raises(ValueError, match="one schema-constrained repair") as caught:
        _provider().propose(_context())

    assert caught.value.reason_code == "tool_parse"
    assert tuple(item.attempt_kind for item in caught.value.provider_receipt.attempts) == (
        "proposal",
        "repair",
    )
    assert all(name == _CANDIDATE_SCHEMA_NAME for name, _ in _FakeRuntime.request_schemas)
    assert all(
        "maxLength" not in schema["properties"]["candidate_source"]
        and "minLength" not in schema["properties"]["candidate_source"]
        for _, schema in _FakeRuntime.request_schemas
    )


def test_exact_tokenizer_admission_fails_before_model_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        LocalQwenProposalProvider,
        "_count_prompt_variants",
        lambda _self, variants, _profile, **_kwargs: tuple(3_000 for _ in variants),
    )
    with pytest.raises(ValueError, match="pinned tokenizer context cap"):
        _provider().propose(_context())
    assert _FakeRuntime.calls == []


def test_local_provider_receipt_crosses_production_preregistration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Result:
        @staticmethod
        def scalar_one() -> None:
            return None

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        @staticmethod
        def execute(*_args, **_kwargs):
            return _Result()

    class _Engine:
        @staticmethod
        def connect():
            return _Connection()

    class _Lease:
        payload = None

        @staticmethod
        def require_run_active() -> None:
            return None

        @staticmethod
        def read_checkpoint(*_args, **_kwargs):
            return None

        def append_checkpoint(self, **kwargs) -> None:
            self.payload = kwargs["payload"]

    context = _context()
    provider = _provider()
    lease = _Lease()
    monkeypatch.setattr(runner, "_stored_turn", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "register_experiment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "store_director_artifact",
        lambda payload, **_kwargs: hashlib.sha256(payload).hexdigest(),
    )
    registered = runner.register_proposal_before_execution(
        _Engine(),
        run_id=UUID("7089275f-d7d2-4f46-b04b-27e809d15326"),
        ordinal=1,
        parent_experiment_id="baseline-robust-z",
        parent_tree_sha256="a" * 40,
        suite_id="synthetic.local-qwen.v1",
        suite_version=1,
        calibration_sha256="b" * 64,
        harness_sha256="d" * 64,
        image_sha256="e" * 64,
        system="S1",
        context=context,
        provider=provider,
        lease=lease,
        artifact_root=tmp_path,
        remaining_wall_seconds=200,
    )
    assert registered.provider_receipt is not None
    assert registered.provider_receipt.prompt_sha256 == (
        registered.provider_receipt.attempts[-1].prompt_sha256
    )
    assert lease.payload is not None
    assert lease.payload["provider_receipt"]["prompt_sha256"] == (
        registered.provider_receipt.prompt_sha256
    )
    assert lease.payload["provider_registry_entry_sha256"] == "c" * 64


def test_preregistration_rejects_misrouted_local_context_before_provider_call(
    tmp_path: Path,
) -> None:
    class _Lease:
        @staticmethod
        def require_run_active() -> None:
            return None

    with pytest.raises(ValueError, match="context system differs"):
        runner.register_proposal_before_execution(
            object(),
            run_id=UUID("7089275f-d7d2-4f46-b04b-27e809d15326"),
            ordinal=1,
            parent_experiment_id="baseline-robust-z",
            parent_tree_sha256="a" * 40,
            suite_id="synthetic.local-qwen.v1",
            suite_version=1,
            calibration_sha256="b" * 64,
            harness_sha256="d" * 64,
            image_sha256="e" * 64,
            system="S2",
            context=_context(system="S1"),
            provider=_provider(),
            lease=_Lease(),
            artifact_root=tmp_path,
        )
    assert _FakeRuntime.calls == []
