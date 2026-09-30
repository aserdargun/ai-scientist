"""Closed-source family proofs and replayable ten-turn EXPLORE episodes.

Only exact calls to trusted factories are classified. This is deliberately not
a semantic classifier for arbitrary Python: an unused import, a class name, or
a model's family declaration cannot authorize an EXPLORE measurement.
"""

from __future__ import annotations

import ast
import hashlib
import json
from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

Family = Literal["robust_z", "iforest", "ecod_train_frozen", "lsh", "optics", "som"]
FAMILIES: tuple[Family, ...] = (
    "robust_z", "iforest", "ecod_train_frozen", "lsh", "optics", "som"
)
CLASSIFIER_VERSION: Literal["trusted-factory-ast.v1"] = "trusted-factory-ast.v1"
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Outcome = Literal[
    "KEEP", "KEEP_SIMPLER", "DISCARD", "REJECT", "CANDIDATE_FAILURE", "ABANDONED"
]


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


class FamilyProof(BaseModel):
    """Trusted classification bound to all candidate source bytes."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    classifier_version: Literal["trusted-factory-ast.v1"] = CLASSIFIER_VERSION
    source_sha256: Digest
    family: Family


def _canonical_ast(source: str) -> str:
    tree = ast.parse(source)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def classify_source(source: bytes) -> FamilyProof | None:
    """Prove one supported fit/score factory; all extra executable syntax fails closed."""
    if not isinstance(source, bytes) or not 1 <= len(source) <= 65_536:
        return None
    try:
        decoded = source.decode("utf-8")
        tree = ast.parse(decoded)
        observed = ast.dump(tree, annotate_fields=True, include_attributes=False)
        for family in FAMILIES[:3]:
            expected = (
                "from harness.baselines import build_baseline\n"
                f"def build_candidate():\n    return build_baseline({family!r})\n"
            )
            if observed == _canonical_ast(expected):
                return FamilyProof(source_sha256=hashlib.sha256(source).hexdigest(), family=family)
        # The exact trusted factory template also pins the validated configuration.
        if len(tree.body) != 2 or not isinstance(tree.body[1], ast.FunctionDef):
            return None
        function = tree.body[1]
        if len(function.body) != 1 or not isinstance(function.body[0], ast.Return):
            return None
        call = function.body[0].value
        if not isinstance(call, ast.Call) or len(call.args) != 1:
            return None
        config_call = call.args[0]
        if not isinstance(config_call, ast.Call) or len(config_call.args) != 1:
            return None
        literal = config_call.args[0]
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            return None
        from lab.operating_modes import ModeConfig

        config = ModeConfig.model_validate_json(literal.value, strict=True)
        expected = (
            "from lab.operating_modes import ModeConfig, OperatingModeCandidate\n"
            "def build_candidate():\n"
            "    return OperatingModeCandidate(ModeConfig.model_validate_json(\n"
            f"        {literal.value!r}))\n"
        )
        if observed != _canonical_ast(expected):
            return None
        return FamilyProof(source_sha256=hashlib.sha256(source).hexdigest(), family=config.method)
    except (SyntaxError, UnicodeError, ValueError, RecursionError, MemoryError):
        return None


class ExploreIntent(BaseModel):
    """Persist this complete intent before selecting a move or asking the provider."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    schema_version: Literal["director-explore-intent.v1"] = Field(
        default="director-explore-intent.v1", alias="schema"
    )
    episode_id: Digest
    run_id: UUID
    start_ordinal: Annotated[StrictInt, Field(ge=26, le=35)]
    champion_experiment_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    champion: FamilyProof
    target_family: Family
    maximum_attempts: Literal[10] = 10
    required_system: Literal["S2"] = "S2"

    @model_validator(mode="after")
    def verify_identity(self) -> ExploreIntent:
        if self.target_family == self.champion.family:
            raise ValueError("EXPLORE must change the proven algorithm family")
        fields = self.model_dump(mode="json", by_alias=True, exclude={"episode_id"})
        if self.episode_id != _digest(fields):
            raise ValueError("EXPLORE episode identity differs from its immutable intent")
        return self


def begin_explore(
    *, run_id: UUID, start_ordinal: int, champion_experiment_id: str, champion_source: bytes
) -> ExploreIntent:
    """Select a different family deterministically without consuming Thompson RNG."""
    proof = classify_source(champion_source)
    if proof is None:
        raise ValueError("explore_family_unverified")
    target = FAMILIES[(FAMILIES.index(proof.family) + 1) % len(FAMILIES)]
    payload = {
        "schema": "director-explore-intent.v1",
        "run_id": str(run_id),
        "start_ordinal": start_ordinal,
        "champion_experiment_id": champion_experiment_id,
        "champion": proof.model_dump(mode="json"),
        "target_family": target,
        "maximum_attempts": 10,
        "required_system": "S2",
    }
    return ExploreIntent.model_validate_json(
        json.dumps({**payload, "episode_id": _digest(payload)})
    )


class ExploreResolution(BaseModel):
    """One actual terminal receipt; infrastructure failures do not create resolutions."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    ordinal: Annotated[StrictInt, Field(ge=26, le=35)]
    terminal_sha256: Digest
    outcome: Outcome


class ExploreEpisode(BaseModel):
    """Immutable prefix of one episode, safe across terminal/state checkpoint crashes."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    intent: ExploreIntent
    resolutions: tuple[ExploreResolution, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def verify_prefix(self) -> ExploreEpisode:
        for offset, result in enumerate(self.resolutions):
            if result.ordinal != self.intent.start_ordinal + offset:
                raise ValueError("EXPLORE results are not one contiguous ordinal prefix")
            if offset < len(self.resolutions) - 1 and result.outcome in {"KEEP", "KEEP_SIMPLER"}:
                raise ValueError("EXPLORE cannot continue after promotion")
        return self

    @property
    def phase(self) -> Literal["EXPLORE", "LOOP", "HOLDOUT_CHECK"]:
        """Ten misses request the existing final holdout/report path, never publication here."""
        if self.resolutions and self.resolutions[-1].outcome in {"KEEP", "KEEP_SIMPLER"}:
            return "LOOP"
        return "HOLDOUT_CHECK" if len(self.resolutions) == 10 else "EXPLORE"

    def resolve(self, result: ExploreResolution) -> ExploreEpisode:
        """Replay the same receipt once; conflicting replay never changes a counter."""
        offset = result.ordinal - self.intent.start_ordinal
        if 0 <= offset < len(self.resolutions):
            if self.resolutions[offset] != result:
                raise ValueError("EXPLORE ordinal already has another terminal receipt")
            return self
        if self.phase != "EXPLORE" or offset != len(self.resolutions):
            raise ValueError("EXPLORE terminal receipt is outside its pending ordinal")
        return ExploreEpisode(intent=self.intent, resolutions=(*self.resolutions, result))


def require_target_family(source: bytes, intent: ExploreIntent) -> FamilyProof:
    """Call before experiment/measurement admission, including restored proposal bytes."""
    proof = classify_source(source)
    if proof is None or proof.family != intent.target_family:
        raise ValueError("explore_family_mismatch")
    return proof


def family_directive(intent: ExploreIntent) -> str:
    """Metadata-only instruction included in exact context/prompt hashes."""
    if intent.target_family in FAMILIES[:3]:
        example = (
            "from harness.baselines import build_baseline\n"
            f"def build_candidate():\n    return build_baseline({intent.target_family!r})\n"
        )
    else:
        from lab.operating_modes import ModeConfig, candidate_source

        method = cast(Literal["lsh", "optics", "som"], intent.target_family)
        example = candidate_source(ModeConfig(method=method)).decode()
    return (
        f"EXPLORE episode={intent.episode_id}; required_system=S2; "
        f"starting_family={intent.champion.family}; target_family={intent.target_family}. "
        "Return only this trusted factory structure; operating-mode configuration values "
        "may change within the validated ModeConfig contract. Extra imports, wrappers or "
        "self-declared family labels do not prove family change. Template:\n" + example
    )
