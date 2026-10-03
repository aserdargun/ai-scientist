"""Reject unsupported AOS service identities before any configuration publication."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_aos_native_configuration as configuration


@pytest.mark.parametrize(
    "unit", ["swapp-aos-joint-acceptance.service", "swapp-lab-gpu-joint.service"]
)
def test_preparer_rejects_caller_outside_canonical_gpu_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: str
) -> None:
    aos = tmp_path / "aos"
    aos.mkdir()
    output = tmp_path / "scientist-config"
    monkeypatch.setattr(
        configuration,
        "verified_source_receipt",
        lambda *_: {"source_root": str(aos)},
    )
    args = SimpleNamespace(
        source_receipt=tmp_path / "source.json",
        expected_source_receipt_sha256="a" * 64,
        aos_python=Path(sys.executable),
        decider_python=Path(sys.executable),
        output=output,
        caller_unit=unit,
    )
    with pytest.raises(ValueError, match="canonical AOS GPU principal"):
        configuration.prepare(args)
    assert not output.exists()
