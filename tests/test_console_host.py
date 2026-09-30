from __future__ import annotations

from console.host import system_info


def test_host_diagnostics_are_read_only_and_return_required_fields() -> None:
    result = system_info()

    assert set(result) == {"memory", "disk", "cpu", "gpu"}
    assert result["memory"]["total_bytes"] >= result["memory"]["available_bytes"] >= 0
    assert result["disk"]["total_bytes"] >= result["disk"]["free_bytes"] >= 0
    assert result["cpu"]["logical_count"] > 0
    assert isinstance(result["gpu"]["available"], bool)
    if not result["gpu"]["available"]:
        assert result["gpu"]["reason"]
