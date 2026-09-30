"""Source simplification must obey the specified line and dependency criteria."""

from lab.director.runner import _source_is_measurably_simpler


def _source(assignments: int, *, dependency: str = "numpy") -> bytes:
    lines = [f"import {dependency}", "def compute(value):"]
    lines.extend(f"    intermediate_{index} = value" for index in range(assignments))
    lines.append("    return value")
    return ("\n".join(lines) + "\n").encode()


def test_fewer_nodes_on_the_same_lines_is_not_a_line_simplification() -> None:
    parent = b"import numpy\ndef score(x):\n    value = x + x + x + x\n    return value\n"
    child = b"import numpy\ndef score(x):\n    value = x\n    return value\n"
    assert not _source_is_measurably_simpler(child, parent)


def test_less_than_five_percent_line_reduction_does_not_qualify() -> None:
    # 23 occupied AST lines become 22: 4.35%, below the 5% requirement.
    assert not _source_is_measurably_simpler(_source(19), _source(20))


def test_replacing_a_dependency_is_not_a_subset_even_with_fewer_lines() -> None:
    assert not _source_is_measurably_simpler(
        _source(1, dependency="scipy"), _source(20, dependency="numpy")
    )


def test_sufficient_line_reduction_with_same_dependencies_qualifies() -> None:
    assert _source_is_measurably_simpler(_source(18), _source(20))
