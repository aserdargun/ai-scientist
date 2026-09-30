"""Suite weights follow Appendix C's tier then independent-family cap policy."""

from __future__ import annotations

import pytest

from lab.director.suite_weights import SuiteWeightInput, suite_weights


def test_spec_appendix_c_weight_example_and_waterfill() -> None:
    types = ["EVT"] * 7 + ["PDM"] * 2 + ["NRM"]
    tiers = ["gold"] * 4 + ["silver"] * 2 + ["bronze"] + ["silver"] * 2 + ["gold"]
    families = ["A"] * 4 + ["B"] * 2 + ["C"] + ["D"] * 2 + ["E"]
    tasks = [
        SuiteWeightInput(task_id=f"task-{i}", task_type=kind, label_tier=tier, family=family)
        for i, (kind, tier, family) in enumerate(zip(types, tiers, families, strict=True))
    ]
    result = suite_weights(tasks)
    assert sum(result.weights) == pytest.approx(1.0)
    assert max(result.family_shares.values()) <= 0.25 + 1e-12
    assert result.family_shares == pytest.approx(
        {"A": 0.25, "B": 0.2, "C": 0.05, "D": 0.25, "E": 0.25}
    )
    assert result.type_shares == pytest.approx({"EVT": 0.5, "PDM": 0.25, "NRM": 0.25})


def test_family_identity_is_independent_of_extra_source_files() -> None:
    # Repeating rows in one source×signal×task family changes the within-family
    # distribution, never that family's post-cap share.
    rows = [
        SuiteWeightInput(task_id=f"{family}-{i}", task_type="EVT", label_tier="gold", family=family)
        for family in ("A", "B", "C", "D")
        for i in range(2 if family == "A" else 1)
    ]
    result = suite_weights(rows, type_share={"EVT": 1.0})
    assert result.family_shares["A"] == pytest.approx(0.25)
    assert all(result.family_shares[name] == pytest.approx(0.25) for name in "ABCD")
    assert sum(result.weights[:2]) == pytest.approx(0.25)


def test_suite_requires_four_independent_families() -> None:
    rows = [
        SuiteWeightInput(task_id=f"t-{i}", task_type="PDM", label_tier="gold", family="CARE-A-PDM")
        for i in range(22)
    ]
    with pytest.raises(ValueError, match="fewer than four"):
        suite_weights(rows)
