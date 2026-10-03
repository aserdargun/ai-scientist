"""CPU fixtures for the two launch units; no service, model or runtime authority."""

import json
import os
from dataclasses import asdict

import pytest
from test_aos_cleanup_authority import cleanup_rig as cleanup_rig
from test_aos_native_launch import receipt_launch as receipt_launch
from test_aos_native_live_bindings import _write
from test_aos_native_live_bindings import capture as capture
from test_aos_shared_scope_preflight import shared_launch as shared_launch

from lab.llm.aos_gpu_broker import PeerGeneration
from lab.llm.aos_gpu_control import ControlError, ControlPolicy
from lab.llm.aos_gpu_executor import SystemdSocketPeerAuthenticator
from lab.llm.gpu_scheduler import _principal_unit_matches
from scripts import aos_native_live_bindings as live

SHARED = "swapp-aos-gpu-shared-desktop-default.service"
ACCEPTANCE = "swapp-aos-gpu-joint-acceptance.service"


def _shared_receipt(case):
    case.cfg["caller_unit"] = SHARED
    case.review["authority"]["caller_unit"] = SHARED
    for binding in case.bindings.values():
        binding["caller_generation"]["unit"] = SHARED
    case.cfg["artifact_validity_seconds"] = 900
    case.review["authority"]["expires_boottime"] = 600
    case.retain()


@pytest.mark.parametrize("unit", [ACCEPTANCE, SHARED])
def test_exact_launch_units_capture_themselves_and_keep_original_expiry(
    receipt_launch, request, unit
):
    case = request.getfixturevalue("shared_launch") if unit == SHARED else receipt_launch
    if unit == ACCEPTANCE:
        _shared_receipt(case)
    case.cfg["caller_unit"] = unit
    case.review["authority"]["caller_unit"] = unit
    for binding in case.bindings.values():
        binding["caller_generation"]["unit"] = unit
    if unit == ACCEPTANCE:
        case.retain()
    case.prepare()
    assert case.capture.call_args.kwargs == {"aos_unit": unit, "lab_unit": None}
    assert json.loads(case.output.read_bytes())["expires_boottime"] == 600


@pytest.mark.parametrize(
    "unit", ["aos-shared-desktop-default.service", "swapp-aos-gpu-other.service"]
)
def test_other_launch_units_fail_before_capture_and_verification(receipt_launch, unit):
    case = receipt_launch
    case.cfg["caller_unit"] = unit
    with pytest.raises(ValueError, match="two reviewed AOS caller units"):
        case.prepare()
    case.capture.assert_not_called()
    case.native.assert_not_called()
    assert not case.output.exists()


@pytest.mark.parametrize("change", ["child-pid", "caller-unit", "expired", "wrong-boot", "ttl-901"])
def test_shared_launch_cannot_reuse_other_identity_or_renew_authority(shared_launch, change):
    case = shared_launch
    if change == "child-pid":
        case.bindings["profile"]["caller_generation"]["pid"] = os.getpid() + 1
    elif change == "caller-unit":
        case.bindings["profile"]["caller_generation"]["unit"] = ACCEPTANCE
    elif change == "expired":
        case.review["authority"]["expires_boottime"] = case.now
    elif change == "wrong-boot":
        case.review["authority"]["boot_id"] = "another-boot"
    else:
        case.cfg["artifact_validity_seconds"] = 901
    case.retain()
    with pytest.raises(ValueError):
        case.prepare()
    case.native.assert_not_called()
    assert not case.output.exists()


@pytest.mark.parametrize(
    "unit,accepted", [(SHARED, True), ("aos-shared-desktop-default.service", False)]
)
def test_existing_principal_socket_and_policy_guards_accept_only_existing_namespace(
    cleanup_rig, unit, accepted
):
    assert _principal_unit_matches("aos", unit) is accepted
    assert not _principal_unit_matches("lab", unit)
    policy = cleanup_rig.first.policy
    config = dict(policy.config, caller_unit=unit)
    policy.path.write_text(json.dumps(config))
    if accepted:
        assert SystemdSocketPeerAuthenticator(unit).aos_unit == unit
        checked = ControlPolicy(
            policy.path, profiles=policy.profiles, source_root=policy.source_root
        )
        assert checked.config["caller_unit"] == unit
    else:
        with pytest.raises(ValueError):
            SystemdSocketPeerAuthenticator(unit)
        with pytest.raises(ControlError, match="unsupported_schema"):
            ControlPolicy(policy.path, profiles=policy.profiles, source_root=policy.source_root)


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "uid",
        "start_ticks",
        "boot_id",
        "invocation_id",
        "control_group",
        "parent_pid",
        "policy-unit",
    ],
)
def test_shared_capture_keeps_full_reviewed_service_tuple_and_policy(capture, drift):
    ctx = capture
    generation = dict(ctx.generations[ctx.aos_unit], unit=SHARED)
    generation["control_group"] = "/user.slice/swapp-gpu.slice/" + SHARED
    ctx.generations[SHARED] = generation
    ctx.policy.config["caller_unit"] = SHARED
    arguments = (*ctx.arguments[:3], _write(ctx.policy_path, ctx.policy.config))
    peer = {
        **generation,
        "parent_pid": generation["pid"],
        "parent_start_ticks": generation["start_ticks"],
    }
    if drift == "policy-unit":
        ctx.policy.config["caller_unit"] = ACCEPTANCE
    elif drift:
        peer[drift] = peer[drift] + 1 if type(peer[drift]) is int else "different"
    ctx.authenticator.authenticate.return_value = PeerGeneration(**peer)
    if drift:
        with pytest.raises(ValueError):
            live.capture_reviewed_bindings(*arguments, aos_unit=SHARED, lab_unit=None)
    else:
        bindings = live.capture_reviewed_bindings(*arguments, aos_unit=SHARED, lab_unit=None)
        assert all(
            binding["caller_generation"] == asdict(ctx.authenticator.authenticate.return_value)
            for binding in bindings.values()
        )
