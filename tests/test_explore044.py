"""Closed family authority and crash-prefix episode reduction; no runtime launches."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest
from pydantic import ValidationError

from lab.director.baselines import baseline_candidate_source
from lab.director.explore import (
    FAMILIES,
    ExploreEpisode,
    ExploreIntent,
    ExploreResolution,
    begin_explore,
    classify_source,
    family_directive,
    require_target_family,
)
from lab.operating_modes import ModeConfig, candidate_source


def _intent():
    return begin_explore(
        run_id=UUID(int=1),
        start_ordinal=26,
        champion_experiment_id="baseline-robust-z",
        champion_source=baseline_candidate_source("robust_z"),
    )


@pytest.mark.parametrize("family", FAMILIES)
def test_closed_factories_prove_actual_returned_family(family):
    source = (
        baseline_candidate_source(family)
        if family in FAMILIES[:3]
        else candidate_source(ModeConfig(method=family))
    )
    proof = classify_source(source)
    assert proof is not None and proof.family == family
    assert proof.source_sha256 == hashlib.sha256(source).hexdigest()
    # Comments have no executable effect but still change the bound bytes.
    commented = classify_source(b"# whitespace/comments allowed\n" + source)
    assert commented is not None and commented.family == family
    assert commented.source_sha256 != proof.source_sha256


@pytest.mark.parametrize(
    "source",
    [
        b"from sklearn.ensemble import IsolationForest\ndef build_candidate(): return object()",
        baseline_candidate_source("robust_z") + b"\nimport sklearn.ensemble\n",
        baseline_candidate_source("robust_z").replace(b"return build_baseline", b"return evil"),
        baseline_candidate_source("iforest") + b"\nbuild_baseline = lambda *args: None\n",
        baseline_candidate_source("iforest").replace(b"'iforest'", b"choose_family()"),
        b"# family=iforest\nclass IsolationForest: pass\n",
        b"from harness.baselines import *\ndef build_candidate(): return build_baseline('iforest')",
        b"x" * 65_537,
        b"\xff",
    ],
)
def test_unknown_unused_import_dynamic_and_rebound_sources_have_no_family_authority(source):
    assert classify_source(source) is None
    with pytest.raises(ValueError, match="explore_family_mismatch"):
        require_target_family(source, _intent())


def test_wrong_family_is_rejected_before_admission_and_target_proof_is_bound():
    intent = _intent()
    assert intent.target_family == "iforest"
    with pytest.raises(ValueError, match="explore_family_mismatch"):
        require_target_family(baseline_candidate_source("robust_z"), intent)
    assert require_target_family(baseline_candidate_source("iforest"), intent).family == "iforest"
    assert "required_system=S2" in family_directive(intent)
    assert intent.episode_id in family_directive(intent)


def test_intent_is_replayable_and_rejects_changed_target_or_champion():
    intent = _intent()
    assert _intent() == intent
    assert ExploreIntent.model_validate_json(intent.model_dump_json()) == intent
    for field, value in [("target_family", "som"), ("champion_experiment_id", "other")]:
        payload = intent.model_dump(mode="json", by_alias=True)
        payload[field] = value
        with pytest.raises(ValidationError, match="immutable intent"):
            ExploreIntent.model_validate_json(__import__("json").dumps(payload))
    with pytest.raises(ValueError, match="explore_family_unverified"):
        begin_explore(
            run_id=UUID(int=1), start_ordinal=26,
            champion_experiment_id="unknown", champion_source=b"unknown()",
        )


def test_ten_distinct_terminal_receipts_enter_holdout_once_after_restart_prefixes():
    episode = ExploreEpisode(intent=_intent())
    for ordinal in range(26, 36):
        receipt = ExploreResolution(
            ordinal=ordinal, terminal_sha256=hashlib.sha256(str(ordinal).encode()).hexdigest(),
            outcome="DISCARD",
        )
        prior = episode
        episode = episode.resolve(receipt)
        assert episode.resolve(receipt) == episode
        # Replaying an already committed terminal after restoring the prior state is identical.
        restored = ExploreEpisode.model_validate_json(prior.model_dump_json())
        assert restored.resolve(receipt) == episode
        assert episode.phase == ("HOLDOUT_CHECK" if ordinal == 35 else "EXPLORE")
    with pytest.raises(ValueError, match="another terminal receipt"):
        episode.resolve(ExploreResolution(ordinal=35, terminal_sha256="f" * 64, outcome="REJECT"))


@pytest.mark.parametrize("outcome", ["KEEP", "KEEP_SIMPLER"])
def test_promotion_returns_loop_and_episode_cannot_consume_further_attempt(outcome):
    episode = ExploreEpisode(intent=_intent())
    receipt = ExploreResolution(ordinal=26, terminal_sha256="a" * 64, outcome=outcome)
    promoted = episode.resolve(receipt)
    assert promoted.phase == "LOOP"
    assert promoted.resolve(receipt) == promoted
    with pytest.raises(ValueError, match="outside its pending ordinal"):
        promoted.resolve(ExploreResolution(ordinal=27, terminal_sha256="b" * 64, outcome="DISCARD"))
    # The loop clears the completed episode; a fresh run has its own deterministic intent.
    other = begin_explore(
        run_id=UUID(int=2), start_ordinal=26, champion_experiment_id="promoted-iforest",
        champion_source=baseline_candidate_source("iforest"),
    )
    assert other.target_family != other.champion.family
    assert other.episode_id != promoted.intent.episode_id
    assert ExploreEpisode(intent=other).resolutions == ()


def test_skipped_ordinal_and_forged_prefix_cannot_spend_or_reorder_attempts():
    episode = ExploreEpisode(intent=_intent())
    receipt = ExploreResolution(ordinal=27, terminal_sha256="a" * 64, outcome="ABANDONED")
    with pytest.raises(ValueError, match="pending ordinal"):
        episode.resolve(receipt)
    with pytest.raises(ValidationError, match="contiguous ordinal"):
        ExploreEpisode(intent=episode.intent, resolutions=(receipt,))
