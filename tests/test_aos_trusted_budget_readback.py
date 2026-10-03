"""Trusted in-process source read; synthetic authority, no physical GPU proof."""

from dataclasses import asdict

import pytest
import test_aos_evidence_integration as legacy
from test_aos_control_store import DEPLOYMENT, PROFILE, TARGET
from test_aos_retained_evidence_integration import _request

from lab.llm.aos_gpu_control import ControlError

cleanup_fixture = legacy.cleanup_fixture


def setup_readback(f, *, physical=False):
    control, bootstrap = legacy._enable(f, retained=True, physical=physical)
    f.call(control, "cancel", target=TARGET, capability=bootstrap["capability_sha256"])
    discovery = control.handle(f.peer, _request("capability", "7"))
    return control, bootstrap, discovery


def read(control, f, fingerprint):
    return control.read_original_budget(
        f.peer, dict(TARGET), PROFILE, DEPLOYMENT, expected_capability_sha256=fingerprint
    )


def test_trusted_readback_matches_retained_columns_without_consuming_control_id(cleanup_fixture):
    f = cleanup_fixture
    control, _, discovery = setup_readback(f)
    count = legacy._count(f)
    expected = discovery["capability_sha256"]
    actual = read(control, f, expected)
    authority = control._control_authority(f.peer, expected, PROFILE, DEPLOYMENT, dict(TARGET))
    assert actual == f.rig.store.read_original_budget(
        asdict(f.peer), TARGET, PROFILE, DEPLOYMENT, authority=authority
    )
    assert read(control, f, expected) == actual
    assert legacy._count(f) == count


def test_bootstrap_infer_capability_cannot_read_trusted_budget(cleanup_fixture):
    f = cleanup_fixture
    control, bootstrap, _ = setup_readback(f)
    before = legacy._count(f)
    with pytest.raises(ControlError, match="capability_mismatch"):
        read(control, f, bootstrap["capability_sha256"])
    assert legacy._count(f) == before


def test_revocation_after_original_read_cannot_return_witness(cleanup_fixture, monkeypatch):
    f = cleanup_fixture
    control, _, discovery = setup_readback(f)
    original = f.rig.store.read_original_budget

    def revoke_after_read(*args, **kwargs):
        witness = original(*args, **kwargs)
        control.authenticator.still_current = lambda _peer: False
        return witness

    monkeypatch.setattr(f.rig.store, "read_original_budget", revoke_after_read)
    with pytest.raises(ControlError):
        read(control, f, discovery["capability_sha256"])


@pytest.mark.parametrize("fault", ["unpinned", "drift", "bootstrap"])
def test_physical_facade_rejects_before_observation(cleanup_fixture, fault):
    f = cleanup_fixture
    control, bootstrap, discovery = setup_readback(f, physical=fault != "unpinned")
    if fault == "drift":
        source = control.policy.source_root / "lab/llm/aos_physical_readback.py"
        source.write_bytes(source.read_bytes() + b"\n# changed source\n")
    fingerprint = (bootstrap if fault == "bootstrap" else discovery)["capability_sha256"]
    # These objects cannot observe or stop anything. Admission must fail first.
    with pytest.raises(ControlError):
        control.verify_original_physical_cleanup(
            f.peer,
            dict(TARGET),
            PROFILE,
            DEPLOYMENT,
            expected_capability_sha256=fingerprint,
            expected_evidence={},
            units=None,
            gpu=None,
        )
