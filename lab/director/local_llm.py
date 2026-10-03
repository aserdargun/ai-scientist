"""Strict proposal-only adapter for the trusted local Qwen runtime.

Its only subprocess invokes a fixed offline tokenizer executable without a shell
or user-controlled arguments; bounded request JSON is passed on stdin.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import UUID, uuid5

from pydantic import ValidationError

from lab.director.budget import MAX_EPISODE_CONTEXT_TOKENS, MAX_EPISODE_OUTPUT_TOKENS
from lab.director.contracts import CandidateProposal
from lab.director.fake_llm import (
    AgentContext,
    ProposalTurn,
    ProviderAttemptTranscript,
    ProviderPromptMessage,
    ProviderReceipt,
    ProviderRuntimeReceipt,
    prompt_context_sha256,
    prompt_messages_sha256,
)
from lab.director.journal import canonical_bytes
from lab.director.mode_proposals import (
    MODE_COMPILER_SHA256,
    MODE_PROMPT_TEMPLATE,
    MODE_PROPOSAL_SCHEMA,
    MODE_SCHEMA_NAME,
    MODE_SCHEMA_SHA256,
    MODE_SYSTEM_PROMPT,
    OperatingModeProposal,
    ProposalContract,
    compile_mode_proposal,
    validate_proposal_contract,
)
from lab.llm.gpu_scheduler import PrincipalResolver
from lab.llm.native_runtime import (
    LOCAL_RESEARCH_S1_PROFILE,
    LOCAL_RESEARCH_S2_PROFILE,
    LOCAL_SMOKE_S1_PROFILE,
    LOCAL_SMOKE_S2_PROFILE,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    ModelMeasurements,
    ModelOutputBudgetExceeded,
    ModelPin,
    ModelReply,
    ModelTurnProfile,
    OwnedVllmRuntime,
)

_RECEIPT_NAMESPACE = UUID("65c75b81-e98b-480f-a608-5389bb6bf8bf")
_PROMPT_TEMPLATE: Literal["director.candidate-contract.metadata-only.v6"] = (
    "director.candidate-contract.metadata-only.v6"
)
_PROVIDER_ID: Literal["local-qwen.v1"] = "local-qwen.v1"
_CANDIDATE_CONTRACT_EXAMPLE = """import numpy as np
import pandas as pd
from harness.contracts import ADPipeline, FitContext, AlarmPolicy

class Candidate:
    def fit(self, train: pd.DataFrame, ctx: FitContext) -> None:
        self.signals = list(ctx.signals)
        values = train.loc[:, self.signals].to_numpy(dtype=float)
        self.center = values.mean(axis=0)
        self.scale = np.maximum(values.std(axis=0), 1e-8)

    def score(self, data: pd.DataFrame) -> np.ndarray:
        values = data.loc[:, self.signals].to_numpy(dtype=float)
        residuals = np.abs((values - self.center) / self.scale)
        return residuals.mean(axis=1)

    def alarm_policy(self, train_scores: np.ndarray) -> AlarmPolicy:
        threshold = float(np.percentile(train_scores, 95))
        return AlarmPolicy(threshold, threshold * 0.8, 1)

def build_candidate() -> ADPipeline:
    return Candidate()
"""
_SYSTEM_PROMPT = (
    # Static model instructions; the text below is never passed to a SQL engine.
    "You propose source code only; trusted Docker and Scorer code measures it, and the "  # nosec B608
    "Referee decides. Return exactly one compact JSON object with hypothesis (1..384 "
    "characters), move_type (the preselected move), candidate_source (complete runnable "
    "Python source, 1..6000 characters), and predicted_delta (finite number in [-4,4]). "
    "candidate_source must contain the complete Python source as a JSON string. Encode "
    "source line breaks as escaped JSON \\n sequences so decoding yields real newlines; "
    "use normal indented multi-line Python with imports and method bodies on separate lines. "
    "A one-line semicolon/comma approximation is not a complete implementation. "
    "Keep the source concise: no markdown, code fences, prose, examples, docstrings, or "
    "unused helpers. Do not omit executable behavior or truncate the implementation to fit. "
    "Do not include scores, metric claims, verdicts, labels, or raw series. The source must "
    "define build_candidate() and return an object implementing this contract:\n"
    "from harness.contracts import ADPipeline, FitContext, AlarmPolicy\n"
    "class Candidate:\n"
    "  def fit(self, train: pandas.DataFrame, ctx: FitContext) -> None: ...\n"
    "  def score(self, data: pandas.DataFrame) -> numpy.ndarray: ...\n"
    "  def alarm_policy(self, train_scores: numpy.ndarray) -> AlarmPolicy: ...\n"
    "def build_candidate() -> ADPipeline: return Candidate()\n"
    "FitContext has seed:int, signals:tuple[str,...], regime_signals:tuple[str,...], "
    "sampling_s:int|None, time_budget_s:float. sampling_s may be None only when the "
    "trusted task is EVT and its source cadence is undocumented; never infer seconds or "
    "claim physical-time rates in that case. PDM/NRM and physical-time metrics require a "
    "known positive sampling_s. fit sees only the training frame. score must "
    "return one finite float per input row in order; larger means more anomalous, and a "
    "prefix's scores must not depend on future rows. alarm_policy uses training scores "
    "only and returns AlarmPolicy(threshold:float, release:float, dwell:int), with "
    "release<=threshold and dwell>=1. Inputs can contain multiple sensor columns: "
    "select them with train.loc[:, list(ctx.signals)], not train[ctx.signals]. "
    "ctx exists only as fit's argument; store required settings and learned parameters "
    "on self in fit, then use that fitted state in score. DataFrame.mean()/std() return "
    "per-column Series, not scalar floats. Convert sensor values to an array, learn "
    "per-column parameters in fit, and reduce sensor residuals across axis=1 to return "
    "shape (len(data),), never (len(data), number_of_signals). Never calculate batch "
    "mean, std, quantiles, or normalization from evaluation data in score: that makes "
    "earlier scores depend on future rows. score must work repeatedly from the same "
    "frozen fitted state and never rely on a module-global ctx. "
    "The following complete example illustrates the interface, fitted state, row "
    "reduction, and causality only; it is not a required detector or research method. "
    "Choose and implement the preselected move using the trusted context; preserve "
    "these interface properties for any method you choose:\n"
    + _CANDIDATE_CONTRACT_EXAMPLE
    + "\nUse only supplied signal columns and installed "
    "numpy/pandas plus harness.contracts. Do not access files, network, labels, or hidden "
    "evaluation values. Use only supplied metadata, development feedback, and champion. "
    "If a complete implementation cannot fit the source limit, choose a smaller valid "
    "method change rather than returning partial code."
)

_LOCAL_HYPOTHESIS_MAX_LENGTH = 384
_LOCAL_CANDIDATE_SOURCE_MAX_LENGTH = 6_000


def _local_candidate_schema() -> dict[str, object]:
    """Derive the local constrained schema from the shared contract with tighter text caps."""
    decoded = json.loads(json.dumps(CandidateProposal.model_json_schema()))
    if not isinstance(decoded, dict):
        raise RuntimeError("CandidateProposal schema is not an object")
    schema = cast(dict[str, object], decoded)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise RuntimeError("CandidateProposal schema has no properties object")
    hypothesis = properties.get("hypothesis")
    if not isinstance(hypothesis, dict):
        raise RuntimeError("CandidateProposal schema has no hypothesis field")
    hypothesis["minLength"] = 1
    hypothesis["maxLength"] = _LOCAL_HYPOTHESIS_MAX_LENGTH
    source = properties.get("candidate_source")
    if not isinstance(source, dict):
        raise RuntimeError("CandidateProposal schema has no candidate_source field")
    # Pinned xgrammar rejects escaped JSON newlines when a string has min/max
    # length constraints. Enforce the stricter local source cap in _parse_proposal.
    source.pop("minLength", None)
    source.pop("maxLength", None)
    source["description"] = (
        "Complete runnable Python source. Encode line breaks as escaped JSON \\n sequences; "
        "after decoding, the source must be normal indented multiline Python."
    )
    return schema


_CANDIDATE_PROPOSAL_SCHEMA = _local_candidate_schema()
_CANDIDATE_SCHEMA_BYTES = json.dumps(
    _CANDIDATE_PROPOSAL_SCHEMA,
    sort_keys=True,
    separators=(",", ":"),
    ensure_ascii=False,
    allow_nan=False,
).encode("utf-8")
_CANDIDATE_SCHEMA_SHA256 = hashlib.sha256(_CANDIDATE_SCHEMA_BYTES).hexdigest()
_CANDIDATE_SCHEMA_NAME = "candidate-proposal-local-multiline-python.v3"
_MAX_TOKENIZER_INPUT_BYTES = 4 * 1024 * 1024
_MAX_TOKENIZER_TIMEOUT_SECONDS = 10
_MAX_PROMPT_BYTES_PER_TOKEN = 8
_TOKENIZER_SOURCE_NAMES = frozenset(
    {
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "chat_template.jinja",
        "vocab.json",
        "merges.txt",
    }
)
_TOKENIZER_PROGRAM = r"""
import json, sys
from transformers import AutoTokenizer
source = json.load(sys.stdin)
tokenizer = AutoTokenizer.from_pretrained(
    source["model_directory"], local_files_only=True, trust_remote_code=False
)
counts = []
for messages in source["variants"]:
    ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        return_dict=False,
        add_generation_prompt=True,
        enable_thinking=source["enable_thinking"],
    )
    if not isinstance(ids, list) or not all(isinstance(item, int) for item in ids):
        raise TypeError("pinned chat tokenizer returned an invalid token sequence")
    counts.append(len(ids))
sys.stdout.write(json.dumps({"counts": counts}, separators=(",", ":")))
"""


class _RuntimeCancellationOptions(TypedDict, total=False):
    cancellation_observer: Callable[[], None]


class ProviderOutputError(ValueError):
    """Invalid provider output with durable host-measured model usage attached."""

    def __init__(
        self,
        message: str,
        *,
        receipt: ProviderReceipt,
        response_text: str,
        attempt_transcripts: tuple[ProviderAttemptTranscript, ...] = (),
        reason_code: Literal[
            "tool_parse", "model_request", "budget_exhausted", "provider_error"
        ] = "provider_error",
    ) -> None:
        super().__init__(message)
        self.provider_receipt = receipt
        self.response_text = response_text
        self.attempt_transcripts = attempt_transcripts
        self.reason_code = reason_code


def _attempt_failure_reason(error: Exception) -> Literal["budget_exhausted", "provider_error"]:
    if isinstance(error, TimeoutError) or str(error) in {
        "required task metadata and champion exceed the pinned tokenizer context cap",
        "repair prompt exceeds the pinned tokenizer context cap",
        "repair prompt context-token budget is exhausted",
        "proposal episode context-token budget is exhausted",
        "proposal episode has no output-token budget remaining",
    }:
        return "budget_exhausted"
    return "provider_error"


class _ModelAttemptFailure(RuntimeError):
    def __init__(
        self,
        cause: Exception,
        *,
        runtime: OwnedVllmRuntime,
        request_id: str,
        profile: ModelTurnProfile,
        elapsed: float,
        messages: tuple[dict[str, str], ...],
        preflight_prompt_tokens: int,
        attempt_index: int,
        output_token_limit: int,
    ) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause
        self.runtime = runtime
        self.request_id = request_id
        self.profile = profile
        self.elapsed = elapsed
        self.messages = messages
        self.preflight_prompt_tokens = preflight_prompt_tokens
        self.attempt_index = attempt_index
        self.output_token_limit = output_token_limit


class _AttemptRuntimeView:
    """Read-only runtime receipt surface reconstructed from a durable attempt."""

    def __init__(self, receipt: ProviderRuntimeReceipt) -> None:
        self._receipt = receipt
        self._last_response_metadata = {
            "prompt_tokens": receipt.prompt_tokens,
            "completion_tokens": receipt.completion_tokens,
        }

    def unit_identity_receipt(self, owner: str, request_id: str) -> dict[str, object]:
        del owner, request_id
        return {
            "unit": self._receipt.unit,
            "invocation_id": self._receipt.invocation_id,
            "control_group": self._receipt.control_group,
            "main_pid": self._receipt.main_pid,
            "main_start_ticks": self._receipt.main_start_ticks,
        }


def provider_profile_config(
    profile_set: Literal["smoke", "research"] = "smoke",
    proposal_contract: ProposalContract = "candidate-python.v1",
) -> dict[str, object]:
    """Canonical trusted provider configuration selected by immutable registry digest."""
    validate_proposal_contract(proposal_contract)
    pin = ModelPin.from_repository()
    profiles = (
        (LOCAL_SMOKE_S1_PROFILE, LOCAL_SMOKE_S2_PROFILE)
        if profile_set == "smoke"
        else (LOCAL_RESEARCH_S1_PROFILE, LOCAL_RESEARCH_S2_PROFILE)
    )
    config: dict[str, object] = {
        "schema": "local-qwen-provider-config.v1",
        "provider_id": _PROVIDER_ID,
        "model_repository": MODEL_REPOSITORY,
        "model_revision": MODEL_REVISION,
        "model_manifest_sha256": pin.digest,
        "profiles": [asdict(profile) for profile in profiles],
        "prompt_template": _PROMPT_TEMPLATE,
        "prompt_admission": "pinned-qwen-chat-template.v1",
        "prompt_trim_policy": "oldest-feedback-first",
        "output_schema": "candidate-proposal.local-multiline-python.v3",
        "output_schema_sha256": _CANDIDATE_SCHEMA_SHA256,
        "output_schema_enforcement": "vllm-json-schema-strict.v1",
        "output_limits": {
            "hypothesis_characters": _LOCAL_HYPOTHESIS_MAX_LENGTH,
            "candidate_source_characters": _LOCAL_CANDIDATE_SOURCE_MAX_LENGTH,
        },
        "repair_policy": "one-tokenizer-bounded-schema-and-python-syntax-repair.v4",
        "fallback": "one-s2-budget-exhausted-to-s1",
    }
    if profile_set == "research":
        config.update(
            {
                "schema": "local-qwen-provider-config.v2",
                "profile_set": "bounded-research",
                "episode_token_budget": {
                    "input_tokens": MAX_EPISODE_CONTEXT_TOKENS,
                    "output_tokens_by_system": MAX_EPISODE_OUTPUT_TOKENS,
                    "total_model_tokens_by_system": {
                        system: MAX_EPISODE_CONTEXT_TOKENS + output_tokens
                        for system, output_tokens in MAX_EPISODE_OUTPUT_TOKENS.items()
                    },
                    "repair_uses_same_episode_budget": True,
                },
                "request_admission": "pinned-tokenizer-input-plus-output-within-model-max-len.v1",
            }
        )
    elif profile_set != "smoke":
        raise ValueError("unknown trusted local Qwen profile set")
    if proposal_contract == "operating-mode-config.v1":
        config.update(
            {
                "proposal_contract": proposal_contract,
                "prompt_template": MODE_PROMPT_TEMPLATE,
                "output_schema": MODE_SCHEMA_NAME,
                "output_schema_sha256": MODE_SCHEMA_SHA256,
                "output_limits": {"hypothesis_characters": 384},
                "repair_policy": "one-tokenizer-bounded-mode-config-repair.v1",
                "candidate_compiler": "lab.operating_modes.candidate.candidate_source.v1",
                "candidate_compiler_sha256": MODE_COMPILER_SHA256,
                "system_prompt_sha256": hashlib.sha256(MODE_SYSTEM_PROMPT.encode()).hexdigest(),
            }
        )
    return config


def provider_config_sha256(
    profile_set: Literal["smoke", "research"] = "smoke",
    proposal_contract: ProposalContract = "candidate-python.v1",
) -> str:
    return hashlib.sha256(
        canonical_bytes(provider_profile_config(profile_set, proposal_contract))
    ).hexdigest()


def provider_profile_set_for_sha256(
    expected_sha256: str,
    proposal_contract: ProposalContract = "candidate-python.v1",
) -> Literal["smoke", "research"]:
    """Resolve only the exact registered provider config; never accept a caller profile name."""
    validate_proposal_contract(proposal_contract)
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("trusted provider configuration digest is malformed")
    profile_sets: tuple[Literal["smoke", "research"], ...] = ("smoke", "research")
    matches = tuple(
        profile_set
        for profile_set in profile_sets
        if provider_config_sha256(profile_set, proposal_contract) == expected_sha256
    )
    if len(matches) != 1:
        raise ValueError("trusted provider configuration digest is unknown or ambiguous")
    return matches[0]


def provider_proposal_contract_for_sha256(expected_sha256: str) -> ProposalContract:
    """Resolve an immutable registry digest across the two finite output contracts."""
    matches: list[ProposalContract] = []
    contracts: tuple[ProposalContract, ...] = (
        "candidate-python.v1",
        "operating-mode-config.v1",
    )
    for proposal_contract in contracts:
        try:
            provider_profile_set_for_sha256(expected_sha256, proposal_contract)
        except ValueError:
            continue
        matches.append(proposal_contract)
    if len(matches) != 1:
        raise ValueError("trusted provider configuration digest is unknown or ambiguous")
    return matches[0]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("proposal JSON repeats a field")
        value[key] = item
    return value


class LocalQwenProposalProvider:
    """Generate proposals only; trusted Director code owns all measurements/verdicts."""

    provider_id = _PROVIDER_ID

    def __init__(
        self,
        *,
        run_id: UUID,
        owner: str,
        principal_resolver: PrincipalResolver,
        runtime_database: Path,
        registry_entry_sha256: str,
        profile_set: Literal["smoke", "research"] = "smoke",
        proposal_contract: ProposalContract = "candidate-python.v1",
        cancellation_observer: Callable[[], None] | None = None,
    ) -> None:
        if owner != "lab":
            raise ValueError("local Director model calls must use the Lab GPU lane")
        self.run_id = run_id
        self.cancellation_observer = cancellation_observer
        self.owner = owner
        self.principal_resolver = principal_resolver
        self.runtime_database = runtime_database
        self.model_pin = ModelPin.from_repository()
        if len(registry_entry_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in registry_entry_sha256
        ):
            raise ValueError("provider registry entry receipt must be a lowercase SHA-256")
        self.registry_entry_sha256 = registry_entry_sha256
        self.profile_set = profile_set
        self.proposal_contract = validate_proposal_contract(proposal_contract)
        self.configuration_sha256 = provider_config_sha256(profile_set, proposal_contract)
        self.response_schema = (
            MODE_PROPOSAL_SCHEMA
            if proposal_contract == "operating-mode-config.v1"
            else _CANDIDATE_PROPOSAL_SCHEMA
        )
        self.response_schema_sha256 = (
            MODE_SCHEMA_SHA256
            if proposal_contract == "operating-mode-config.v1"
            else _CANDIDATE_SCHEMA_SHA256
        )
        self.response_schema_name = (
            MODE_SCHEMA_NAME
            if proposal_contract == "operating-mode-config.v1"
            else _CANDIDATE_SCHEMA_NAME
        )

    def _profile_for_system(self, system: Literal["S1", "S2"]) -> ModelTurnProfile:
        if self.profile_set == "smoke":
            return LOCAL_SMOKE_S1_PROFILE if system == "S1" else LOCAL_SMOKE_S2_PROFILE
        return LOCAL_RESEARCH_S1_PROFILE if system == "S1" else LOCAL_RESEARCH_S2_PROFILE

    @staticmethod
    def _request_id(run_id: UUID, ordinal: int, system: str, attempt_index: int = 0) -> str:
        return uuid5(_RECEIPT_NAMESPACE, f"{run_id}:{ordinal}:{system}:{attempt_index}").hex

    @staticmethod
    def _message_variants(
        context: AgentContext,
        profile: ModelTurnProfile,
        proposal_contract: ProposalContract = "candidate-python.v1",
    ) -> tuple[tuple[dict[str, str], ...], ...]:
        # Keep all task metadata and the full champion source. If necessary,
        # discard the oldest feedback first; never trim candidate contract or
        # trusted task identities to make a prompt fit.
        validate_proposal_contract(proposal_contract)
        system_prompt = (
            MODE_SYSTEM_PROMPT
            if proposal_contract == "operating-mode-config.v1"
            else _SYSTEM_PROMPT
        )
        variants: list[tuple[dict[str, str], ...]] = []
        for feedback_count in range(len(context.recent_feedback), -1, -1):
            selected_feedback = context.recent_feedback[-feedback_count:] if feedback_count else ()
            prompt_context = context.model_copy(update={"recent_feedback": selected_feedback})
            context_json = json.dumps(
                prompt_context.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            user = "Trusted metadata-only run context (no labels or raw series):\n" + context_json
            if context.explore_intent is not None:
                user += "\nRequired trusted EXPLORE directive (not optional):\n" + str(
                    context.explore_directive
                )

            messages = (
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user},
            )
            prompt_bytes = sum(len(item["content"].encode("utf-8")) for item in messages)
            if (
                prompt_bytes <= _MAX_TOKENIZER_INPUT_BYTES
                and prompt_bytes <= profile.max_context_tokens * _MAX_PROMPT_BYTES_PER_TOKEN
            ):
                variants.append(messages)
        if variants:
            return tuple(variants)
        raise ValueError("required task metadata and champion exceed the bounded prompt collector")

    @staticmethod
    def _messages(
        context: AgentContext,
        profile: ModelTurnProfile,
        proposal_contract: ProposalContract = "candidate-python.v1",
    ) -> tuple[dict[str, str], ...]:
        """Return the fullest bounded variant for display and unit fixtures."""
        return LocalQwenProposalProvider._message_variants(context, profile, proposal_contract)[0]

    def _verify_tokenizer_files(self) -> None:
        expected = {name: (size, digest) for name, size, digest in self.model_pin.files}
        required = {"config.json", "tokenizer.json", "tokenizer_config.json"}
        if not required.issubset(expected):
            raise RuntimeError("pinned model record omits required tokenizer files")
        for name in sorted(_TOKENIZER_SOURCE_NAMES & expected.keys()):
            size, expected_digest = expected[name]
            path = self.model_pin.directory / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("pinned tokenizer file is missing or unsafe")
            stat = path.stat(follow_symlinks=False)
            if stat.st_size != size or size > 128 * 1024 * 1024:
                raise RuntimeError("pinned tokenizer file size differs from its source record")
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_digest:
                raise RuntimeError("pinned tokenizer file differs from its source record")

    def _count_prompt_variants(
        self,
        variants: tuple[tuple[dict[str, str], ...], ...],
        profile: ModelTurnProfile,
        *,
        timeout_seconds: float,
    ) -> tuple[int, ...]:
        self._verify_tokenizer_files()
        request = {
            "model_directory": str(self.model_pin.directory),
            "enable_thinking": profile.enable_thinking,
            "variants": [list(messages) for messages in variants],
        }
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > _MAX_TOKENIZER_INPUT_BYTES:
            raise ValueError("tokenizer preflight input exceeds its fixed byte bound")
        executable = Path(__file__).resolve().parents[2] / "data/runtime/vllm/.venv/bin/python"
        try:
            resolved_executable = executable.resolve(strict=True)
            resolved_executable.relative_to(Path.home() / ".local/share/uv/python")
            executable_stat = resolved_executable.stat(follow_symlinks=False)
        except (OSError, ValueError) as exc:
            raise RuntimeError("pinned offline tokenizer interpreter is unavailable") from exc
        if (
            not executable.is_symlink()
            or not resolved_executable.is_file()
            or not os.access(executable, os.X_OK)
            or executable_stat.st_uid != os.getuid()
            or executable_stat.st_mode & 0o022
        ):
            raise RuntimeError("pinned offline tokenizer interpreter is unavailable")
        env = {
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "USE_TORCH": "0",
            "USE_TF": "0",
            "CUDA_VISIBLE_DEVICES": "",
            "TOKENIZERS_PARALLELISM": "false",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        try:
            # Fixed tokenizer program; bounded JSON is supplied only through stdin.
            completed = subprocess.run(  # nosec B603
                [str(executable), "-c", _TOKENIZER_PROGRAM],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=min(_MAX_TOKENIZER_TIMEOUT_SECONDS, timeout_seconds),
                check=False,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "pinned local tokenizer exceeded its CPU preflight deadline"
            ) from exc
        if completed.returncode != 0 or len(completed.stdout) > 4096:
            raise RuntimeError("pinned local tokenizer preflight failed")
        try:
            decoded = json.loads(completed.stdout)
            counts = decoded["counts"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("pinned local tokenizer returned malformed output") from exc
        if (
            not isinstance(counts, list)
            or len(counts) != len(variants)
            or any(
                isinstance(count, bool) or not isinstance(count, int) or count < 1
                for count in counts
            )
        ):
            raise RuntimeError("pinned local tokenizer returned invalid token counts")
        return tuple(counts)

    def _select_messages(
        self,
        context: AgentContext,
        profile: ModelTurnProfile,
        *,
        timeout_seconds: float,
    ) -> tuple[tuple[dict[str, str], ...], int]:
        variants = self._message_variants(
            context, profile, proposal_contract=self.proposal_contract
        )
        counts = self._count_prompt_variants(variants, profile, timeout_seconds=timeout_seconds)
        for messages, token_count in zip(variants, counts, strict=True):
            if (
                token_count <= profile.max_context_tokens
                and token_count + profile.max_output_tokens <= profile.model_max_len
            ):
                return messages, token_count
        raise ValueError(
            "required task metadata and champion exceed the pinned tokenizer context cap"
        )

    def _call(
        self,
        context: AgentContext,
        profile: ModelTurnProfile,
        *,
        episode_deadline: float,
        messages_override: tuple[dict[str, str], ...] | None = None,
        messages_override_prompt_tokens: int | None = None,
        attempt_index: int = 0,
        attempt_kind: Literal["proposal", "repair"] = "proposal",
        output_token_limit: int | None = None,
        remaining_context_tokens: int = MAX_EPISODE_CONTEXT_TOKENS,
        remaining_output_tokens: int | None = None,
        remaining_model_tokens: int | None = None,
        attempt_load: Callable[[int], dict[str, object] | None] | None = None,
        attempt_save: Callable[[int, str, dict[str, object]], None] | None = None,
    ) -> tuple[
        ModelReply | None,
        OwnedVllmRuntime,
        str,
        float,
        tuple[dict[str, str], ...],
        int,
        int,
    ]:
        request_id = self._request_id(
            self.run_id, context.experiment_number, profile.system, attempt_index
        )
        saved = attempt_load(attempt_index) if attempt_load is not None else None
        if saved is not None:
            saved_messages = saved.get("messages")
            if not isinstance(saved_messages, list) or not all(
                isinstance(item, dict)
                and isinstance(item.get("role"), str)
                and isinstance(item.get("content"), str)
                for item in saved_messages
            ):
                raise ValueError("durable attempt lacks its exact request messages")
            messages = tuple(
                {"role": item["role"], "content": item["content"]} for item in saved_messages
            )
            receipt = ProviderRuntimeReceipt.model_validate(saved.get("receipt"), strict=True)
            if (
                saved.get("profile_id") != profile.profile_id
                or saved.get("prompt_sha256") != prompt_messages_sha256(messages)
                or saved.get("response_schema_sha256") != self.response_schema_sha256
                or (
                    self.proposal_contract == "operating-mode-config.v1"
                    and saved.get("proposal_contract") != self.proposal_contract
                )
                or saved.get("context_sha256") != prompt_context_sha256(context)
                or saved.get("request_id") != request_id
                or saved.get("provider_config_sha256") != self.configuration_sha256
                or saved.get("provider_registry_entry_sha256") != self.registry_entry_sha256
                or saved.get("model_sha256") != self.model_pin.digest
                or saved.get("profile_sha256")
                != hashlib.sha256(canonical_bytes(asdict(profile))).hexdigest()
            ):
                raise RuntimeError("durable model attempt identity differs from this request")
            output_limit = receipt.output_token_limit
            view = _AttemptRuntimeView(receipt)
            if saved.get("state") == "started":
                raise _ModelAttemptFailure(
                    RuntimeError("model attempt started without a durable final response"),
                    runtime=view,  # type: ignore[arg-type]
                    request_id=request_id,
                    profile=profile,
                    elapsed=receipt.wall_seconds,
                    messages=messages,
                    preflight_prompt_tokens=receipt.preflight_prompt_tokens or 1,
                    attempt_index=attempt_index,
                    output_token_limit=output_limit,
                )
            if saved.get("state") != "completed":
                raise RuntimeError("durable model attempt has an unknown state")
            if saved.get("outcome") == "failed":
                raise _ModelAttemptFailure(
                    RuntimeError(receipt.failure_type or "ModelRequestFailed"),
                    runtime=view,  # type: ignore[arg-type]
                    request_id=request_id,
                    profile=profile,
                    elapsed=receipt.wall_seconds,
                    messages=messages,
                    preflight_prompt_tokens=receipt.preflight_prompt_tokens or 1,
                    attempt_index=attempt_index,
                    output_token_limit=output_limit,
                )
            if saved.get("outcome") == "output_budget_exhausted":
                return (
                    None,
                    view,  # type: ignore[return-value]
                    request_id,
                    receipt.wall_seconds,
                    messages,
                    receipt.preflight_prompt_tokens or 1,
                    output_limit,
                )
            if saved.get("outcome") != "completed":
                raise RuntimeError("durable model attempt ended without a usable response")
            measurements = saved.get("measurements")
            if not isinstance(measurements, dict):
                raise ValueError("durable model attempt lacks runtime measurements")
            prompt_tokens = saved.get("prompt_tokens")
            completion_tokens = saved.get("completion_tokens")
            if (
                isinstance(prompt_tokens, bool)
                or not isinstance(prompt_tokens, int)
                or prompt_tokens < 0
                or isinstance(completion_tokens, bool)
                or not isinstance(completion_tokens, int)
                or completion_tokens < 0
            ):
                raise ValueError("durable model attempt has invalid token usage")
            reply = ModelReply(
                str(saved["response_text"]),
                prompt_tokens,
                completion_tokens,
                ModelMeasurements(**measurements),
            )
            return (
                reply,
                view,  # type: ignore[return-value]
                request_id,
                receipt.wall_seconds,
                messages,
                receipt.preflight_prompt_tokens or 1,
                output_limit,
            )
        preflight_remaining = episode_deadline - time.monotonic()
        if preflight_remaining <= 0:
            raise TimeoutError("proposal episode expired before tokenizer preflight")
        if messages_override is None:
            messages, preflight_prompt_tokens = self._select_messages(
                context, profile, timeout_seconds=preflight_remaining
            )
        else:
            messages = messages_override
            if messages_override_prompt_tokens is None:
                counts = self._count_prompt_variants(
                    (messages,), profile, timeout_seconds=preflight_remaining
                )
                if len(counts) != 1:
                    raise RuntimeError("pinned tokenizer returned an invalid repair count")
                preflight_prompt_tokens = counts[0]
            else:
                preflight_prompt_tokens = messages_override_prompt_tokens
            if (
                isinstance(preflight_prompt_tokens, bool)
                or not isinstance(preflight_prompt_tokens, int)
                or not 1 <= preflight_prompt_tokens <= profile.max_context_tokens
            ):
                raise ValueError("repair prompt exceeds the pinned tokenizer context cap")
        if preflight_prompt_tokens > remaining_context_tokens:
            raise ValueError("proposal episode context-token budget is exhausted")
        output_remaining = (
            profile.max_output_tokens
            if remaining_output_tokens is None
            else remaining_output_tokens
        )
        model_remaining = (
            remaining_context_tokens + output_remaining
            if remaining_model_tokens is None
            else remaining_model_tokens
        )
        bounded_output = min(
            profile.max_output_tokens,
            output_remaining,
            model_remaining - preflight_prompt_tokens,
        )
        if output_token_limit is not None:
            bounded_output = min(bounded_output, output_token_limit)
        if bounded_output < 1:
            raise ValueError("proposal episode has no output-token budget remaining")
        remaining = min(profile.total_seconds, int(episode_deadline - time.monotonic()))
        if remaining <= profile.activation_seconds:
            raise TimeoutError("proposal episode has no bounded time left for model activation")
        activation_seconds = profile.activation_seconds
        inference_seconds = min(profile.inference_seconds, remaining - activation_seconds)
        total_seconds = activation_seconds + inference_seconds
        cancellation_options: _RuntimeCancellationOptions = {}
        if self.cancellation_observer is not None:
            cancellation_options["cancellation_observer"] = self.cancellation_observer
        runtime = OwnedVllmRuntime(
            self.runtime_database,
            principal_resolver=self.principal_resolver,
            pin=self.model_pin,
            profile=profile,
            activation_seconds=activation_seconds,
            inference_seconds=inference_seconds,
            total_seconds=total_seconds,
            **cancellation_options,
        )
        started_payload = {
            "state": "started",
            "profile_id": profile.profile_id,
            "attempt_kind": attempt_kind,
            "prompt_sha256": prompt_messages_sha256(messages),
            "response_schema_sha256": self.response_schema_sha256,
            "output_token_limit": bounded_output,
            "preflight_prompt_tokens": preflight_prompt_tokens,
            "request_id": request_id,
            "context_sha256": prompt_context_sha256(context),
            "messages": list(messages),
            "provider_config_sha256": self.configuration_sha256,
            "provider_registry_entry_sha256": self.registry_entry_sha256,
            "model_sha256": self.model_pin.digest,
            "profile_sha256": hashlib.sha256(canonical_bytes(asdict(profile))).hexdigest(),
        }
        if self.proposal_contract == "operating-mode-config.v1":
            started_payload["proposal_contract"] = self.proposal_contract
        started_payload["receipt"] = self._attempt_receipt(
            runtime,
            request_id,
            profile,
            None,
            wall_seconds=0.0,
            messages=messages,
            preflight_prompt_tokens=preflight_prompt_tokens,
            attempt_kind=attempt_kind,
            output_token_limit=bounded_output,
            failure_type="AttemptOutcomeUnknown",
            proposal_contract=self.proposal_contract,
        ).model_dump(mode="json")
        if attempt_save is not None:
            attempt_save(attempt_index, "started", started_payload)
        started = time.monotonic()
        try:
            reply = runtime.run_turn(
                self.owner,
                request_id,
                messages,
                enable_thinking=profile.enable_thinking,
                profile=profile,
                response_schema_name=self.response_schema_name,
                response_schema=self.response_schema,
                output_token_limit=bounded_output,
            )
        except ModelOutputBudgetExceeded:
            elapsed = max(0.0, time.monotonic() - started)
            attempt_receipt = self._attempt_receipt(
                runtime,
                request_id,
                profile,
                None,
                wall_seconds=elapsed,
                messages=messages,
                preflight_prompt_tokens=preflight_prompt_tokens,
                attempt_kind=attempt_kind,
                output_token_limit=bounded_output,
                proposal_contract=self.proposal_contract,
            )
            if attempt_save is not None:
                attempt_save(
                    attempt_index,
                    "completed",
                    {
                        **started_payload,
                        "state": "completed",
                        "outcome": "output_budget_exhausted",
                        "receipt": attempt_receipt.model_dump(mode="json"),
                    },
                )
            return (
                None,
                runtime,
                request_id,
                elapsed,
                messages,
                preflight_prompt_tokens,
                bounded_output,
            )
        except Exception as exc:
            elapsed = max(0.0, time.monotonic() - started)
            failure_receipt = self._attempt_receipt(
                runtime,
                request_id,
                profile,
                None,
                wall_seconds=elapsed,
                messages=messages,
                preflight_prompt_tokens=preflight_prompt_tokens,
                attempt_kind=attempt_kind,
                output_token_limit=bounded_output,
                failure_type=type(exc).__name__,
                proposal_contract=self.proposal_contract,
            )
            if attempt_save is not None:
                attempt_save(
                    attempt_index,
                    "completed",
                    {
                        **started_payload,
                        "state": "completed",
                        "outcome": "failed",
                        "receipt": failure_receipt.model_dump(mode="json"),
                    },
                )
            raise _ModelAttemptFailure(
                exc,
                runtime=runtime,
                request_id=request_id,
                profile=profile,
                elapsed=elapsed,
                messages=messages,
                preflight_prompt_tokens=preflight_prompt_tokens,
                attempt_index=attempt_index,
                output_token_limit=bounded_output,
            ) from exc
        elapsed = max(0.0, time.monotonic() - started)
        success_receipt = self._attempt_receipt(
            runtime,
            request_id,
            profile,
            reply,
            wall_seconds=elapsed,
            messages=messages,
            preflight_prompt_tokens=preflight_prompt_tokens,
            attempt_kind=attempt_kind,
            output_token_limit=bounded_output,
            proposal_contract=self.proposal_contract,
        )
        if attempt_save is not None:
            attempt_save(
                attempt_index,
                "completed",
                {
                    **started_payload,
                    "state": "completed",
                    "outcome": "completed",
                    "receipt": success_receipt.model_dump(mode="json"),
                    "response_text": reply.text,
                    "prompt_tokens": reply.prompt_tokens,
                    "completion_tokens": reply.completion_tokens,
                    "measurements": asdict(reply.measurements),
                },
            )
        return (
            reply,
            runtime,
            request_id,
            elapsed,
            messages,
            preflight_prompt_tokens,
            bounded_output,
        )

    @staticmethod
    def _attempt_receipt(
        runtime: OwnedVllmRuntime,
        request_id: str,
        profile: ModelTurnProfile,
        reply: ModelReply | None,
        *,
        wall_seconds: float,
        messages: tuple[dict[str, str], ...],
        preflight_prompt_tokens: int,
        attempt_kind: Literal["proposal", "repair"] = "proposal",
        output_token_limit: int,
        response_text: str | None = None,
        failure_type: str | None = None,
        proposal_contract: ProposalContract = "candidate-python.v1",
    ) -> ProviderRuntimeReceipt:
        validate_proposal_contract(proposal_contract)
        schema_sha256 = (
            MODE_SCHEMA_SHA256
            if proposal_contract == "operating-mode-config.v1"
            else _CANDIDATE_SCHEMA_SHA256
        )
        identity: dict[str, object]
        try:
            identity = runtime.unit_identity_receipt("lab", request_id)
        except (RuntimeError, ValueError):
            if failure_type is None:
                raise
            identity = {}
        prompt_bytes = canonical_bytes({"messages": list(messages)})
        metadata = runtime._last_response_metadata or {}
        if failure_type is not None:
            prompt_tokens = metadata.get("prompt_tokens")
            completion_tokens = metadata.get("completion_tokens")
            if isinstance(prompt_tokens, bool) or not isinstance(prompt_tokens, int):
                prompt_tokens = None
            if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int):
                completion_tokens = None
            return ProviderRuntimeReceipt(
                unit=cast(str | None, identity.get("unit")),
                invocation_id=cast(str | None, identity.get("invocation_id")),
                control_group=cast(str | None, identity.get("control_group")),
                main_pid=cast(int | None, identity.get("main_pid")),
                main_start_ticks=cast(int | None, identity.get("main_start_ticks")),
                profile_id=profile.profile_id,
                attempt_kind=attempt_kind,
                sampling_top_k=profile.top_k,
                prompt_sha256=prompt_messages_sha256(messages),
                response_schema_sha256=schema_sha256,
                response_sha256=(
                    hashlib.sha256(response_text.encode("utf-8")).hexdigest()
                    if response_text is not None
                    else None
                ),
                prompt_utf8_bytes=len(prompt_bytes),
                preflight_prompt_tokens=preflight_prompt_tokens,
                output_token_limit=output_token_limit,
                outcome="failed",
                failure_type=failure_type[:64],
                wall_seconds=wall_seconds,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        if reply is None:
            prompt_tokens = metadata.get("prompt_tokens")
            completion_tokens = metadata.get("completion_tokens")
            if (
                isinstance(prompt_tokens, bool)
                or not isinstance(prompt_tokens, int)
                or isinstance(completion_tokens, bool)
                or not isinstance(completion_tokens, int)
            ):
                raise RuntimeError("truncated S2 response omitted measured token usage")
            return ProviderRuntimeReceipt(
                unit=str(identity["unit"]),
                invocation_id=str(identity["invocation_id"]),
                control_group=str(identity["control_group"]),
                main_pid=cast(int, identity["main_pid"]),
                main_start_ticks=cast(int, identity["main_start_ticks"]),
                profile_id=profile.profile_id,
                attempt_kind=attempt_kind,
                sampling_top_k=profile.top_k,
                prompt_sha256=prompt_messages_sha256(messages),
                response_schema_sha256=schema_sha256,
                response_sha256=None,
                prompt_utf8_bytes=len(prompt_bytes),
                preflight_prompt_tokens=preflight_prompt_tokens,
                output_token_limit=output_token_limit,
                outcome="output_budget_exhausted",
                wall_seconds=wall_seconds,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        return ProviderRuntimeReceipt(
            unit=str(identity["unit"]),
            invocation_id=str(identity["invocation_id"]),
            control_group=str(identity["control_group"]),
            main_pid=cast(int, identity["main_pid"]),
            main_start_ticks=cast(int, identity["main_start_ticks"]),
            profile_id=profile.profile_id,
            attempt_kind=attempt_kind,
            sampling_top_k=profile.top_k,
            prompt_sha256=prompt_messages_sha256(messages),
            response_schema_sha256=schema_sha256,
            response_sha256=hashlib.sha256(reply.text.encode("utf-8")).hexdigest(),
            prompt_utf8_bytes=len(prompt_bytes),
            preflight_prompt_tokens=preflight_prompt_tokens,
            output_token_limit=output_token_limit,
            outcome="completed",
            wall_seconds=wall_seconds,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
            startup_seconds=reply.measurements.startup_seconds,
            inference_seconds=reply.measurements.inference_seconds,
            drain_seconds=reply.measurements.drain_seconds,
            peak_host_memory_bytes=reply.measurements.peak_host_memory_bytes,
            peak_gpu_memory_mib=reply.measurements.peak_gpu_memory_mib,
            cpu_usage_usec=reply.measurements.cpu_usage_usec,
        )

    def _provider_receipt(
        self,
        context: AgentContext,
        profile: ModelTurnProfile,
        attempts: list[ProviderRuntimeReceipt],
        attempted_profiles: list[str],
        fallback_from: str | None,
    ) -> ProviderReceipt:
        return ProviderReceipt(
            schema="local-qwen-provider-receipt.v1",
            provider_id=_PROVIDER_ID,
            model_id=MODEL_REPOSITORY,
            model_revision=MODEL_REVISION,
            model_sha256=self.model_pin.digest,
            profile_id=profile.profile_id,
            profile_sha256=hashlib.sha256(canonical_bytes(asdict(profile))).hexdigest(),
            requested_system=context.system,
            actual_system=profile.system,
            fallback_from=cast(Literal["S2"] | None, fallback_from),
            context_template=(
                "director.operating-mode-contract.metadata-only.v1"
                if self.proposal_contract == "operating-mode-config.v1"
                else _PROMPT_TEMPLATE
            ),
            context_sha256=prompt_context_sha256(context),
            prompt_sha256=attempts[-1].prompt_sha256,
            input_tokens=sum(item.prompt_tokens or 0 for item in attempts),
            output_tokens=sum(item.completion_tokens or 0 for item in attempts),
            attempts=tuple(attempts),
            attempted_profile_ids=tuple(attempted_profiles),
            provider_config_sha256=self.configuration_sha256,
            provider_registry_entry_sha256=self.registry_entry_sha256,
            sampling_temperature=profile.temperature,
            sampling_top_p=profile.top_p,
            sampling_top_k=profile.top_k,
            enable_thinking=profile.enable_thinking,
            thinking_token_budget=profile.thinking_token_budget,
        )

    @staticmethod
    def _attempt_transcript(
        profile: ModelTurnProfile,
        messages: tuple[dict[str, str], ...],
        reply: ModelReply,
        *,
        attempt_kind: Literal["proposal", "repair"],
        preflight_prompt_tokens: int,
        proposal_contract: ProposalContract = "candidate-python.v1",
    ) -> ProviderAttemptTranscript:
        validate_proposal_contract(proposal_contract)
        response_bytes = reply.text.encode("utf-8")
        if len(response_bytes) > 256 * 1024:
            raise ValueError("model final content exceeds its transcript bound")
        return ProviderAttemptTranscript(
            attempt_kind=attempt_kind,
            profile_id=profile.profile_id,
            prompt_messages=tuple(
                ProviderPromptMessage.model_validate(item, strict=True) for item in messages
            ),
            prompt_sha256=prompt_messages_sha256(messages),
            response_schema_sha256=(
                MODE_SCHEMA_SHA256
                if proposal_contract == "operating-mode-config.v1"
                else _CANDIDATE_SCHEMA_SHA256
            ),
            response_sha256=hashlib.sha256(response_bytes).hexdigest(),
            response_text=reply.text,
            preflight_prompt_tokens=preflight_prompt_tokens,
        )

    @staticmethod
    def _parse_proposal(
        raw_text: str,
        expected_move: str | None,
        proposal_contract: ProposalContract = "candidate-python.v1",
    ) -> CandidateProposal:
        validate_proposal_contract(proposal_contract)
        decoded = json.loads(raw_text.strip(), object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(decoded, dict):
            raise ValueError("proposal must be one JSON object")
        if proposal_contract == "operating-mode-config.v1":
            return compile_mode_proposal(
                OperatingModeProposal.model_validate(decoded, strict=True), expected_move
            )
        hypothesis = decoded.get("hypothesis")
        candidate_source = decoded.get("candidate_source")
        if not isinstance(hypothesis, str) or not (
            1 <= len(hypothesis) <= _LOCAL_HYPOTHESIS_MAX_LENGTH
        ):
            raise ValueError("proposal hypothesis exceeds the local compact schema bound")
        if not isinstance(candidate_source, str) or not (
            1 <= len(candidate_source) <= _LOCAL_CANDIDATE_SOURCE_MAX_LENGTH
        ):
            raise ValueError("candidate source exceeds the local compact schema bound")
        parsed = CandidateProposal.model_validate(decoded, strict=True)
        if expected_move is not None and parsed.move_type != expected_move:
            raise ValueError("model proposal changed the trusted selected move intent")
        try:
            ast.parse(parsed.candidate_source)
        except SyntaxError as exc:
            # Keep the repair reason useful while excluding generated source text.
            line = exc.lineno if exc.lineno is not None else 0
            column = exc.offset if exc.offset is not None else 0
            raise ValueError(
                f"candidate source has invalid Python syntax at line {line}, column {column}"
            ) from None
        return parsed

    @staticmethod
    def _repair_error_code(error: Exception) -> str:
        """Describe validation failure without echoing candidate or parser input."""
        if isinstance(error, json.JSONDecodeError):
            return "invalid_json"
        if isinstance(error, ValidationError):
            return "proposal_schema_validation_failed"
        if isinstance(error, ValueError):
            message = str(error)
            syntax_prefix = "candidate source has invalid Python syntax at line "
            if message.startswith(syntax_prefix):
                location = message[len(syntax_prefix) :]
                match = re.fullmatch(r"([0-9]{1,8}), column ([0-9]{1,8})", location)
                if match is not None:
                    return f"python_syntax_error_line_{match.group(1)}_column_{match.group(2)}"
            if message == "model proposal changed the trusted selected move intent":
                return "selected_move_intent_mismatch"
            if message == "proposal must be one JSON object":
                return "proposal_not_single_json_object"
        return "proposal_contract_validation_failed"

    def _select_repair_messages(
        self,
        context: AgentContext,
        *,
        raw_response: str,
        validation_code: str,
        profile: ModelTurnProfile,
        remaining_context_tokens: int,
        timeout_seconds: float,
    ) -> tuple[tuple[dict[str, str], ...], int]:
        """Prefer a full-response repair only when pinned tokenizer admits it."""
        intro = (
            "One repair is available. Validation category: "
            + validation_code
            + ". Return one complete, corrected proposal JSON object."
        )
        bases = self._message_variants(context, profile, proposal_contract=self.proposal_contract)
        compact_variants = tuple(
            (
                base[0],
                {
                    "role": "user",
                    "content": (
                        base[1]["content"]
                        + "\n\n"
                        + intro
                        + " Previous response text was omitted because it exceeded the pinned "
                        "tokenizer context cap. Generate a fresh complete candidate from the "
                        "trusted task context and champion; do not assume any prior source text."
                    ),
                },
            )
            for base in bases
        )
        variants: tuple[tuple[dict[str, str], ...], ...]
        if len(raw_response.encode("utf-8")) <= 65_536:
            full_variants = tuple(
                (
                    base[0],
                    {
                        "role": "user",
                        "content": (
                            base[1]["content"]
                            + "\n\n"
                            + intro
                            + " The previous final response is untrusted input to correct:\n"
                            + raw_response
                        ),
                    },
                )
                for base in bases
            )
            variants = (*full_variants, *compact_variants)
        else:
            variants = compact_variants
        counts = self._count_prompt_variants(variants, profile, timeout_seconds=timeout_seconds)
        if len(counts) != len(variants):
            raise RuntimeError("pinned tokenizer returned invalid repair prompt counts")
        for messages, token_count in zip(variants, counts, strict=True):
            if (
                token_count <= profile.max_context_tokens
                and token_count <= remaining_context_tokens
            ):
                return messages, token_count
        raise ValueError("repair prompt context-token budget is exhausted")

    def propose(self, context: AgentContext) -> ProposalTurn:
        profile = self._profile_for_system(context.system)
        return self.propose_bounded(context, remaining_wall_seconds=profile.total_seconds)

    def propose_bounded(
        self,
        context: AgentContext,
        *,
        remaining_wall_seconds: float,
        remaining_model_tokens: int | None = None,
        attempt_load: Callable[[int], dict[str, object] | None] | None = None,
        attempt_save: Callable[[int, str, dict[str, object]], None] | None = None,
    ) -> ProposalTurn:
        if context.move_type is None:
            raise ValueError("local provider requires a preselected trusted move intent")
        if context.system not in {"S1", "S2"}:
            raise ValueError("local provider requires a trusted S1/S2 route")
        if (
            isinstance(remaining_wall_seconds, bool)
            or not isinstance(remaining_wall_seconds, (int, float))
            or not 1 <= remaining_wall_seconds <= 720
        ):
            raise ValueError("local proposal requires a finite remaining episode budget")
        episode_model_tokens = (
            MAX_EPISODE_CONTEXT_TOKENS + MAX_EPISODE_OUTPUT_TOKENS[context.system]
            if remaining_model_tokens is None
            else remaining_model_tokens
        )
        if (
            isinstance(episode_model_tokens, bool)
            or not isinstance(episode_model_tokens, int)
            or not 1 <= episode_model_tokens <= 24_576
        ):
            raise ValueError("local proposal requires a bounded model-token reservation")
        episode_deadline = time.monotonic() + float(remaining_wall_seconds)
        requested = self._profile_for_system(context.system)
        profiles_attempted = [requested.profile_id]
        attempts: list[ProviderRuntimeReceipt] = []
        fallback_from: str | None = None
        try:
            (
                reply,
                runtime,
                request_id,
                elapsed,
                messages,
                preflight_tokens,
                output_limit,
            ) = self._call(
                context,
                requested,
                episode_deadline=episode_deadline,
                remaining_context_tokens=MAX_EPISODE_CONTEXT_TOKENS,
                remaining_output_tokens=MAX_EPISODE_OUTPUT_TOKENS[context.system],
                remaining_model_tokens=episode_model_tokens,
                attempt_load=attempt_load,
                attempt_save=attempt_save,
            )
        except _ModelAttemptFailure as failure:
            attempts.append(
                self._attempt_receipt(
                    failure.runtime,
                    failure.request_id,
                    requested,
                    None,
                    wall_seconds=failure.elapsed,
                    messages=failure.messages,
                    preflight_prompt_tokens=failure.preflight_prompt_tokens,
                    output_token_limit=failure.output_token_limit,
                    failure_type=type(failure.cause).__name__,
                    proposal_contract=self.proposal_contract,
                )
            )
            receipt = self._provider_receipt(
                context,
                requested,
                attempts,
                profiles_attempted,
                None,
            )
            raise ProviderOutputError(
                "local model request failed after bounded activation",
                receipt=receipt,
                response_text="",
                reason_code="model_request",
            ) from failure.cause
        if reply is None:
            attempts.append(
                self._attempt_receipt(
                    runtime,
                    request_id,
                    requested,
                    None,
                    wall_seconds=elapsed,
                    messages=messages,
                    preflight_prompt_tokens=preflight_tokens,
                    output_token_limit=output_limit,
                    proposal_contract=self.proposal_contract,
                )
            )
            if context.system != "S2" or context.explore_intent is not None:
                receipt = self._provider_receipt(
                    context, requested, attempts, profiles_attempted, None
                )
                raise ProviderOutputError(
                    "Required proposal system exhausted its bounded output",
                    receipt=receipt,
                    response_text="",
                    reason_code="budget_exhausted",
                )
            fallback_from = "S2"
            actual = self._profile_for_system("S1")
            profiles_attempted.append(actual.profile_id)
            if episode_deadline - time.monotonic() <= actual.activation_seconds:
                receipt = self._provider_receipt(
                    context,
                    requested,
                    attempts,
                    profiles_attempted[:1],
                    None,
                )
                receipt = receipt.model_copy(
                    update={
                        "failure_profile_id": actual.profile_id,
                        "failure_type": "EpisodeTimeBudgetExhausted",
                    }
                )
                raise ProviderOutputError(
                    "S2 output budget exhausted and no episode time remains for the S1 fallback",
                    receipt=receipt,
                    response_text="",
                    reason_code="budget_exhausted",
                )
            try:
                (
                    reply,
                    runtime,
                    request_id,
                    elapsed,
                    messages,
                    preflight_tokens,
                    output_limit,
                ) = self._call(
                    context,
                    actual,
                    episode_deadline=episode_deadline,
                    remaining_context_tokens=(
                        MAX_EPISODE_CONTEXT_TOKENS
                        - sum(item.prompt_tokens or 0 for item in attempts)
                    ),
                    remaining_output_tokens=(
                        MAX_EPISODE_OUTPUT_TOKENS[context.system]
                        - sum(item.completion_tokens or 0 for item in attempts)
                    ),
                    attempt_load=attempt_load,
                    attempt_save=attempt_save,
                    remaining_model_tokens=episode_model_tokens
                    - sum(
                        (item.prompt_tokens or 0) + (item.completion_tokens or 0)
                        for item in attempts
                    ),
                )
            except _ModelAttemptFailure as failure:
                profiles_attempted.append(actual.profile_id)
                attempts.append(
                    self._attempt_receipt(
                        failure.runtime,
                        failure.request_id,
                        actual,
                        None,
                        wall_seconds=failure.elapsed,
                        messages=failure.messages,
                        preflight_prompt_tokens=failure.preflight_prompt_tokens,
                        attempt_kind="proposal",
                        output_token_limit=failure.output_token_limit,
                        failure_type=type(failure.cause).__name__,
                        proposal_contract=self.proposal_contract,
                    )
                )
                receipt = self._provider_receipt(
                    context,
                    actual,
                    attempts,
                    profiles_attempted,
                    fallback_from,
                )
                raise ProviderOutputError(
                    "S1 fallback request failed after bounded activation",
                    receipt=receipt,
                    response_text="",
                    reason_code="model_request",
                ) from failure.cause
            except Exception as exc:
                receipt = self._provider_receipt(
                    context,
                    requested,
                    attempts,
                    profiles_attempted,
                    fallback_from,
                ).model_copy(
                    update={
                        "failure_profile_id": actual.profile_id,
                        "failure_type": type(exc).__name__,
                    }
                )
                raise ProviderOutputError(
                    "S1 fallback failed before model activation",
                    receipt=receipt,
                    response_text="",
                    reason_code=_attempt_failure_reason(exc),
                ) from exc
            if reply is None:
                attempts.append(
                    self._attempt_receipt(
                        runtime,
                        request_id,
                        actual,
                        None,
                        wall_seconds=elapsed,
                        messages=messages,
                        preflight_prompt_tokens=preflight_tokens,
                        output_token_limit=output_limit,
                        proposal_contract=self.proposal_contract,
                    )
                )
                receipt = self._provider_receipt(
                    context,
                    actual,
                    attempts,
                    profiles_attempted,
                    fallback_from,
                )
                raise ProviderOutputError(
                    "S1 fallback exhausted its bounded output",
                    receipt=receipt,
                    response_text="",
                    reason_code="budget_exhausted",
                )
        else:
            actual = requested
        attempts.append(
            self._attempt_receipt(
                runtime,
                request_id,
                actual,
                reply,
                wall_seconds=elapsed,
                messages=messages,
                preflight_prompt_tokens=preflight_tokens,
                output_token_limit=output_limit,
                proposal_contract=self.proposal_contract,
            )
        )
        receipt = self._provider_receipt(
            context, actual, attempts, profiles_attempted, fallback_from
        )
        if (
            reply.prompt_tokens > actual.max_context_tokens
            or reply.prompt_tokens != preflight_tokens
        ):
            raise ProviderOutputError(
                "model prompt token count differs from bounded tokenizer preflight",
                receipt=receipt,
                response_text=reply.text[:65_536],
                reason_code="provider_error",
            )
        raw = reply.text
        transcripts: list[ProviderAttemptTranscript] = [
            self._attempt_transcript(
                actual,
                messages,
                reply,
                attempt_kind="proposal",
                preflight_prompt_tokens=preflight_tokens,
                proposal_contract=self.proposal_contract,
            )
        ]
        try:
            parsed = self._parse_proposal(
                raw, context.move_type, proposal_contract=self.proposal_contract
            )
        except (json.JSONDecodeError, ValidationError, ValueError) as first_error:
            # One repair is allowed. Include full untrusted output only when the
            # pinned tokenizer admits the whole repair prompt; otherwise use the
            # trusted context with a sanitized error category and no prior output.
            remaining_repair_context = MAX_EPISODE_CONTEXT_TOKENS - sum(
                item.prompt_tokens or 0 for item in attempts
            )
            saved_repair = attempt_load(1) if attempt_load is not None else None
            if saved_repair is not None:
                # _call validates and returns the durable exact request without
                # repeating tokenizer selection or activating the model again.
                repair_messages, repair_preflight_tokens = messages, preflight_tokens
            else:
                try:
                    repair_messages, repair_preflight_tokens = self._select_repair_messages(
                        context,
                        raw_response=raw,
                        validation_code=self._repair_error_code(first_error),
                        profile=actual,
                        remaining_context_tokens=remaining_repair_context,
                        timeout_seconds=max(0.001, episode_deadline - time.monotonic()),
                    )
                except Exception as failure:
                    failure_receipt = self._provider_receipt(
                        context, actual, attempts, profiles_attempted, fallback_from
                    ).model_copy(
                        update={
                            "failure_profile_id": actual.profile_id,
                            "failure_type": (
                                "RepairContextBudgetExceeded"
                                if isinstance(failure, ValueError)
                                else "TokenizerPreflightError"
                            ),
                        }
                    )
                    raise ProviderOutputError(
                        "schema repair prompt failed bounded tokenizer preflight",
                        receipt=failure_receipt,
                        response_text="",
                        attempt_transcripts=tuple(transcripts),
                        reason_code=_attempt_failure_reason(failure),
                    ) from None
            try:
                (
                    repair_reply,
                    repair_runtime,
                    repair_id,
                    repair_elapsed,
                    repair_prompt,
                    repair_tokens,
                    repair_output_limit,
                ) = self._call(
                    context,
                    actual,
                    episode_deadline=episode_deadline,
                    messages_override=repair_messages,
                    messages_override_prompt_tokens=repair_preflight_tokens,
                    attempt_index=1,
                    attempt_kind="repair",
                    remaining_context_tokens=(
                        MAX_EPISODE_CONTEXT_TOKENS
                        - sum(item.prompt_tokens or 0 for item in attempts)
                    ),
                    remaining_output_tokens=(
                        MAX_EPISODE_OUTPUT_TOKENS[context.system]
                        - sum(item.completion_tokens or 0 for item in attempts)
                    ),
                    remaining_model_tokens=episode_model_tokens
                    - sum(
                        (item.prompt_tokens or 0) + (item.completion_tokens or 0)
                        for item in attempts
                    ),
                    attempt_load=attempt_load,
                    attempt_save=attempt_save,
                )
            except _ModelAttemptFailure as failure:
                attempts.append(
                    self._attempt_receipt(
                        failure.runtime,
                        failure.request_id,
                        actual,
                        None,
                        wall_seconds=failure.elapsed,
                        messages=failure.messages,
                        preflight_prompt_tokens=failure.preflight_prompt_tokens,
                        attempt_kind="repair",
                        output_token_limit=failure.output_token_limit,
                        failure_type=type(failure.cause).__name__,
                        proposal_contract=self.proposal_contract,
                    )
                )
                failure_receipt = self._provider_receipt(
                    context, actual, attempts, profiles_attempted, fallback_from
                )
                raise ProviderOutputError(
                    "schema repair request failed",
                    receipt=failure_receipt,
                    response_text=raw,
                    attempt_transcripts=tuple(transcripts),
                    reason_code="model_request",
                ) from failure.cause
            except Exception as failure:
                failure_receipt = self._provider_receipt(
                    context, actual, attempts, profiles_attempted, fallback_from
                ).model_copy(
                    update={
                        "failure_profile_id": actual.profile_id,
                        "failure_type": type(failure).__name__,
                    }
                )
                raise ProviderOutputError(
                    "schema repair failed before model activation",
                    receipt=failure_receipt,
                    response_text=raw,
                    attempt_transcripts=tuple(transcripts),
                    reason_code=_attempt_failure_reason(failure),
                ) from failure
            if repair_reply is None:
                profiles_attempted.append(actual.profile_id)
                attempts.append(
                    self._attempt_receipt(
                        repair_runtime,
                        repair_id,
                        actual,
                        None,
                        wall_seconds=repair_elapsed,
                        messages=repair_prompt,
                        preflight_prompt_tokens=repair_tokens,
                        attempt_kind="repair",
                        output_token_limit=repair_output_limit,
                        proposal_contract=self.proposal_contract,
                    )
                )
                failure_receipt = self._provider_receipt(
                    context, actual, attempts, profiles_attempted, fallback_from
                )
                raise ProviderOutputError(
                    "schema repair exhausted its bounded output",
                    receipt=failure_receipt,
                    response_text=raw,
                    attempt_transcripts=tuple(transcripts),
                    reason_code="budget_exhausted",
                ) from None
            attempts.append(
                self._attempt_receipt(
                    repair_runtime,
                    repair_id,
                    actual,
                    repair_reply,
                    wall_seconds=repair_elapsed,
                    messages=repair_prompt,
                    preflight_prompt_tokens=repair_tokens,
                    attempt_kind="repair",
                    output_token_limit=repair_output_limit,
                    proposal_contract=self.proposal_contract,
                )
            )
            profiles_attempted.append(actual.profile_id)
            transcripts.append(
                self._attempt_transcript(
                    actual,
                    repair_prompt,
                    repair_reply,
                    attempt_kind="repair",
                    preflight_prompt_tokens=repair_tokens,
                    proposal_contract=self.proposal_contract,
                )
            )
            if repair_reply.prompt_tokens != repair_tokens:
                raise ProviderOutputError(
                    "repair prompt token count differs from tokenizer preflight",
                    receipt=self._provider_receipt(
                        context, actual, attempts, profiles_attempted, fallback_from
                    ),
                    response_text=repair_reply.text,
                    attempt_transcripts=tuple(transcripts),
                    reason_code="provider_error",
                ) from None
            raw = repair_reply.text
            receipt = self._provider_receipt(
                context, actual, attempts, profiles_attempted, fallback_from
            )
            try:
                parsed = self._parse_proposal(
                    raw, context.move_type, proposal_contract=self.proposal_contract
                )
            except (json.JSONDecodeError, ValidationError, ValueError) as repair_error:
                raise ProviderOutputError(
                    "one schema-constrained repair did not produce a strict CandidateProposal",
                    receipt=receipt,
                    response_text=raw,
                    attempt_transcripts=tuple(transcripts),
                    reason_code="tool_parse",
                ) from repair_error
        # The hidden reasoning field is deliberately excluded from the transcript.
        return ProposalTurn(
            proposal=parsed,
            messages=tuple([repair_prompt[0]["content"], repair_prompt[1]["content"], raw])
            if len(transcripts) > 1
            else tuple([messages[0]["content"], messages[1]["content"], raw]),
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            provider_receipt=receipt,
            provider_attempts=tuple(transcripts),
        )
