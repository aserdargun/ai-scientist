"""Validated, deterministic type/tier weighting with an independent-family cap."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictStr, model_validator

TYPE_SHARES = {"EVT": 0.5, "PDM": 0.3, "NRM": 0.2}
TIER_WEIGHTS = {"gold": 1.0, "silver": 0.6, "bronze": 0.3}
FAMILY_CAP = 0.25


def suite_weight_task_key(identity: tuple[str, str, str, str]) -> str:
    """Compactly bind a full task identity for the weight-only uniqueness key."""
    encoded = json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class SuiteWeightInput(BaseModel):
    """Trusted per-task dimensions; `family` is source×signal-type×task-type."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_id: StrictStr = Field(min_length=1, max_length=128)
    task_type: StrictStr = Field(pattern=r"^(EVT|PDM|NRM)$")
    label_tier: StrictStr = Field(pattern=r"^(gold|silver|bronze)$")
    family: StrictStr = Field(min_length=1, max_length=256)


class SuiteWeightResult(BaseModel):
    """Canonical task weights and post-cap type/family allocations."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    task_ids: tuple[StrictStr, ...]
    weights: tuple[StrictFloat, ...]
    type_shares: dict[StrictStr, StrictFloat]
    family_shares: dict[StrictStr, StrictFloat]
    capped_families: tuple[StrictStr, ...]
    family_cap: StrictFloat

    @model_validator(mode="after")
    def verify(self) -> SuiteWeightResult:
        if len(self.task_ids) != len(self.weights) or not self.task_ids:
            raise ValueError("suite weight result needs one weight per task")
        if len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("suite weight task IDs must be unique")
        values = np.asarray(self.weights, dtype=np.float64)
        if (
            not np.isfinite(values).all()
            or np.any(values <= 0)
            or not np.isclose(values.sum(), 1.0)
        ):
            raise ValueError("suite weights must be finite, positive, and sum to one")
        if any(share > self.family_cap + 1e-12 for share in self.family_shares.values()):
            raise ValueError("suite family share exceeds its cap")
        return self


def suite_weights(
    tasks: Sequence[SuiteWeightInput],
    *,
    type_share: Mapping[str, float] = TYPE_SHARES,
    tier_weight: Mapping[str, float] = TIER_WEIGHTS,
    family_cap: float = FAMILY_CAP,
) -> SuiteWeightResult:
    """Implement spec §3.2.5 and Appendix C's capped water-fill exactly.

    Type shares are first renormalized over types represented in the suite. Tier
    weights are normalized within each represented type. Independent families
    are the supplied source×signal-type×task-type identity, never source files.
    A suite with fewer than four such families fails closed at a 0.25 cap.
    """
    records = tuple(SuiteWeightInput.model_validate(item, strict=True) for item in tasks)
    if not records:
        raise ValueError("suite cannot be empty")
    ids = tuple(item.task_id for item in records)
    if len(set(ids)) != len(ids):
        raise ValueError("suite task IDs must be unique")
    if not np.isfinite(family_cap) or not 0.0 < family_cap <= 1.0:
        raise ValueError("family cap must be finite and in (0,1]")
    kinds = tuple(item.task_type for item in records)
    tiers = tuple(item.label_tier for item in records)
    families = tuple(item.family for item in records)
    represented_types = sorted(set(kinds))
    represented_families = sorted(set(families))
    if len(represented_families) * family_cap < 1.0 - 1e-12:
        raise ValueError("fewer than four independent families cannot satisfy the 0.25 cap")
    shares = {name: float(type_share[name]) for name in represented_types}
    if any(not np.isfinite(value) or value <= 0 for value in shares.values()):
        raise ValueError("every represented type needs a positive finite configured share")
    share_total = sum(shares.values())
    weights = np.zeros(len(records), dtype=np.float64)
    for kind in represented_types:
        indices = [i for i, value in enumerate(kinds) if value == kind]
        raw = np.asarray([float(tier_weight[tiers[i]]) for i in indices], dtype=np.float64)
        if not np.isfinite(raw).all() or np.any(raw <= 0):
            raise ValueError("every task tier needs a positive finite configured weight")
        weights[indices] = raw / raw.sum() * shares[kind] / share_total

    capped: set[str] = set()
    for _iteration in range(len(represented_families) + 1):
        current = {
            family: float(weights[np.asarray(families) == family].sum())
            for family in represented_families
        }
        over = [family for family in represented_families if current[family] > family_cap + 1e-12]
        if not over:
            break
        for family in over:
            weights[np.asarray(families) == family] *= family_cap / current[family]
            capped.add(family)
        free_families = [family for family in represented_families if family not in capped]
        deficit = 1.0 - float(weights.sum())
        if deficit > 1e-15:
            free_mask = np.asarray([family in free_families for family in families])
            free_mass = float(weights[free_mask].sum())
            if not free_families or free_mass <= 0:
                raise ValueError("family water-fill has no remaining uncapped allocation")
            weights[free_mask] += weights[free_mask] / free_mass * deficit
    else:
        raise ValueError("family water-fill did not converge")

    weights /= weights.sum()
    family_shares = {
        family: float(weights[np.asarray(families) == family].sum())
        for family in represented_families
    }
    type_shares = {
        kind: float(weights[np.asarray(kinds) == kind].sum()) for kind in represented_types
    }
    return SuiteWeightResult(
        task_ids=ids,
        weights=tuple(float(value) for value in weights),
        type_shares=type_shares,
        family_shares=family_shares,
        capped_families=tuple(sorted(capped)),
        family_cap=float(family_cap),
    )
