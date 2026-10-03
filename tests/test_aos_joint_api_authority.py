"""Original API context limits on the real callback; API/AOS runtime hooks stay inert."""

import hashlib
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from lab.api.aos_capability import LabCapability
from scripts import aos_joint_lab_hooks as hooks


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def authority_rig(monkeypatch, tmp_path):
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    clocks = SimpleNamespace(boottime=100.0, monotonic=500.0, wall=1000.0)
    monkeypatch.setattr(hooks.time, "monotonic", lambda: clocks.monotonic)
    monkeypatch.setattr(hooks.time, "time", lambda: clocks.wall)
    monkeypatch.setattr(hooks.time, "clock_gettime", lambda _clock: clocks.boottime)
    token = "inert-fixture-token"
    roots = {"scientist": str(tmp_path / "scientist"), "aos": str(tmp_path / "aos")}
    paths = {
        "scientist": [
            "lab/api/app.py",
            "lab/api/registry.py",
            "lab/api/aos_capability.py",
            "scripts/aos_joint_lab_hooks.py",
            "scripts/aos_joint_api_authority.py",
            "scripts/aos_native_launch.py",
        ],
        "aos": [
            "src/aos/scientist_lab.py",
            "src/aos/scientist_lab_service.py",
            "src/aos/scientist_lab_journal.py",
            "src/aos/scientist_lab_readbacks.py",
            "src/aos/scientist_protocol.py",
        ],
    }
    sources = {}
    for name, relatives in paths.items():
        for relative in relatives:
            path = Path(roots[name]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# inert independently pinned fixture\n")
            sources[str(path)] = checksum(path.read_bytes())
    unit = "lab-owned-api.service"
    capability = LabCapability(
        schema="scientist.lab-capability.v1",
        owner_id="owner",
        origin="aos",
        suite_id="suite",
        track="mode",
        program_version="v1",
        provider="local-qwen",
        suite_manifest_sha256="a" * 64,
        suite_entry_sha256="b" * 64,
        provider_config_sha256="c" * 64,
        proposal_limit=3,
        proposal_contract="operating-mode-config.v1",
        allowed_purpose="research",
        gpu_release_verified=False,
    )
    startup = dict(
        authority_url="http://127.0.0.1:8767",
        token_file=str(tmp_path / "token"),
        principal_id="owner",
        allowed_suites=["suite"],
        program_version="v1",
        authorization_context_sha256="d" * 64,
        timeout_seconds=3,
    )
    context = dict(
        schema="scientist.joint-api-authorization-context.v1",
        owner_id="owner",
        origin="aos",
        authority_url=startup["authority_url"],
        suite_id="suite",
        program_version="v1",
        token_sha256=checksum(token.encode()),
        api_unit=unit,
        boot_id=boot,
        issued_boottime=90.0,
        expires_boottime=110.0,
        budget=dict(experiments=3, wall_seconds=900, model_tokens=30000),
        automatic_dispatch=False,
        native_inference_authorized=False,
        gpu_release_authorized=False,
    )
    review = dict(
        schema="scientist.joint-lab-review.v1",
        enabled=True,
        startup=startup,
        api_generation=dict(
            pid=os.getpid(),
            start_ticks=123,
            boot_id=boot,
            unit=unit,
            control_group="/user.slice/" + unit,
            invocation_id="e" * 32,
        ),
        expected_capabilities={"suite": capability.model_dump(mode="json", by_alias=True)},
        token_sha256=context["token_sha256"],
        source_inputs=sources,
        source_roots=roots,
        max_experiments=3,
        max_wall_seconds=900,
        max_model_tokens=30000,
    )
    client = SimpleNamespace(
        _token=Mock(return_value=token),
        _request=Mock(return_value=capability.model_dump_json(by_alias=True).encode()),
        authority_url=startup["authority_url"],
        principal_id="owner",
        allowed_suites=frozenset({"suite"}),
    )
    prepare = Mock(return_value=(None, client))
    service = ModuleType("aos.scientist_lab_service")

    def parse_startup(raw, **_kwargs):
        value = json.loads(raw)
        value["allowed_suites"] = frozenset(value["allowed_suites"])
        return SimpleNamespace(**value)

    service.ScientistLabStartup = SimpleNamespace(model_validate_json=parse_startup)
    service.prepare_scientist_lab_startup = prepare
    monkeypatch.setitem(sys.modules, "aos.scientist_lab_service", service)
    monkeypatch.setattr(
        hooks, "_read_process_identity", lambda _pid: SimpleNamespace(start_ticks=123, boot_id=boot)
    )
    monkeypatch.setattr(hooks, "_process_cgroup", lambda _pid: "/user.slice/" + unit)
    original_open = Path.open

    def private_environment(path, *args, **kwargs):
        if str(path) == f"/proc/{os.getpid()}/environ":
            return io.BytesIO(b"INVOCATION_ID=" + b"e" * 32 + b"\0")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", private_environment)
    context_path, review_path = tmp_path / "context.json", tmp_path / "review.json"

    def publish():
        raw = json.dumps(context).encode()
        context_path.write_bytes(raw)
        context_path.chmod(0o600)
        startup["authorization_context_sha256"] = checksum(raw)
        sources[str(context_path)] = checksum(raw)
        review_raw = json.dumps(review).encode()
        review_path.write_bytes(review_raw)
        return checksum(review_raw)

    def construct():
        return hooks.JointLabCapability(review_path, publish())

    return SimpleNamespace(
        context=context,
        context_path=context_path,
        review=review,
        startup=startup,
        clocks=clocks,
        client=client,
        prepare=prepare,
        sources=sources,
        publish=publish,
        construct=construct,
    )


def test_expired_context_denies_constructor_before_token_or_http(authority_rig):
    rig = authority_rig
    rig.context["expires_boottime"] = rig.clocks.boottime
    with pytest.raises(ValueError, match="[Oo]riginal API authority"):
        rig.construct()
    rig.prepare.assert_not_called()
    rig.client._request.assert_not_called()


def test_live_context_clamps_original_deadline_without_renewal(authority_rig):
    rig = authority_rig
    rig.context["expires_boottime"] = 100.5
    capability = rig.construct()
    assert capability._current(503.0) == 500.5
    assert capability._current(500.25) == 500.25
    rig.clocks.boottime = 100.5
    with pytest.raises(ValueError, match="expired"):
        capability._current(503.0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "other"),
        ("origin", "lab"),
        ("authority_url", "http://127.0.0.1:8768"),
        ("api_unit", "lab-other-api.service"),
        ("suite_id", "other"),
        ("program_version", "v2"),
        ("token_sha256", "f" * 64),
        ("boot_id", "wrong-boot"),
        ("issued_boottime", True),
        ("issued_boottime", 101.0),
        ("expires_boottime", float("inf")),
        ("expires_boottime", 3690.01),
        ("native_inference_authorized", True),
        ("gpu_release_authorized", True),
        ("automatic_dispatch", True),
    ],
)
def test_original_scope_and_lifetime_cannot_be_rebound(authority_rig, field, value):
    rig = authority_rig
    rig.context[field] = value
    with pytest.raises(ValueError, match="[Oo]riginal API authority"):
        rig.construct()
    rig.prepare.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing-clock", "budget", "extra-suite", "helper-pin"])
def test_original_authority_cannot_gain_budget_suite_or_unpinned_verifier(authority_rig, mutation):
    rig = authority_rig
    if mutation == "missing-clock":
        del rig.context["issued_boottime"]
    elif mutation == "budget":
        rig.context["budget"]["experiments"] = 2
    elif mutation == "extra-suite":
        rig.startup["allowed_suites"].append("other")
        rig.review["expected_capabilities"]["other"] = dict(
            rig.review["expected_capabilities"]["suite"], suite_id="other"
        )
    else:
        del rig.sources[
            str(
                Path(rig.review["source_roots"]["scientist"]) / "scripts/aos_joint_api_authority.py"
            )
        ]
    with pytest.raises(ValueError):
        rig.construct()
    rig.prepare.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing", "ambiguous", "tampered"])
def test_bound_context_requires_unique_pinned_unrevoked_document(authority_rig, mutation):
    rig = authority_rig
    capability = rig.construct()
    if mutation == "tampered":
        rig.context_path.write_bytes(b"{}")
    else:
        # An independently repinned review with a missing or ambiguous context is still invalid.
        data = rig.review
        if mutation == "missing":
            del rig.sources[str(rig.context_path)]
        else:
            rig.sources[str(rig.context_path.with_name("same-hash.json"))] = rig.startup[
                "authorization_context_sha256"
            ]
        raw = json.dumps(data).encode()
        capability.path.write_bytes(raw)
        capability.expected_hash = checksum(raw)
        capability.review = capability._review()
    with pytest.raises(ValueError, match="original API authority"):
        capability._current(503.0)
    rig.client._request.assert_not_called()


@pytest.fixture
def authority_action(authority_rig, monkeypatch):
    from pydantic import BaseModel

    class Task(BaseModel):
        binding: object
        request: object
        lab_run_id: str | None = None

    class Action(BaseModel):
        deadline: float
        tool: str

    lab = ModuleType("aos.scientist_lab")
    lab.ScientistLabTask, lab.ScientistLabAction = Task, Action
    lab.ScientistLabPolicy = SimpleNamespace(check=Mock())
    protocol = ModuleType("aos.scientist_protocol")
    protocol.ScientistRunStatus = SimpleNamespace(
        model_validate_json=lambda raw, **_kwargs: SimpleNamespace(**json.loads(raw))
    )
    monkeypatch.setitem(sys.modules, "aos.scientist_lab", lab)
    monkeypatch.setitem(sys.modules, "aos.scientist_protocol", protocol)
    rig = authority_rig
    capability = rig.construct()
    task = Task(
        binding=SimpleNamespace(
            authorization_context_sha256=rig.startup["authorization_context_sha256"]
        ),
        request=SimpleNamespace(
            program_version="v1",
            suite="suite",
            track="mode",
            budget=SimpleNamespace(experiments=1, wall_seconds=10, model_tokens=100),
        ),
    )
    return rig, capability, task, Action(deadline=1030.0, tool="lab.start")


@pytest.mark.parametrize("tool", ["lab.start", "lab.status", "lab.stop", "lab.report"])
def test_expiry_denies_every_control_before_http(authority_action, tool):
    rig, capability, task, action = authority_action
    rig.clocks.boottime = 110.0
    with pytest.raises(ValueError, match="expired"):
        capability(task, action.model_copy(update={"tool": tool}))
    rig.client._request.assert_not_called()


def test_both_http_oracles_use_original_remaining_authority(authority_action):
    rig, capability, task, action = authority_action
    # Already admitted context expires ten seconds from the frozen fixture's original now.
    rig.clocks.boottime = 109.5
    original_response = rig.client._request.return_value
    rig.client._request.side_effect = [
        original_response,
        json.dumps(dict(run_id="run", origin="aos", purpose="research")).encode(),
    ]
    capability(task.model_copy(update={"lab_run_id": "run"}), action)
    assert rig.client._request.call_count == 2
    assert [call.args[3] for call in rig.client._request.call_args_list] == [500.5, 500.5]


@pytest.mark.parametrize("remote_run", [None, "run"])
def test_expiry_during_http_denies_final_bracket_and_next_oracle(authority_action, remote_run):
    rig, capability, task, action = authority_action
    original_response = rig.client._request.return_value

    def expire(*_args, **_kwargs):
        rig.clocks.boottime = 110.0
        return original_response

    rig.client._request.side_effect = expire
    with pytest.raises(ValueError, match="expired"):
        capability(task.model_copy(update={"lab_run_id": remote_run}), action)
    assert rig.client._request.call_count == 1


def test_context_revocation_during_http_denies_final_bracket(authority_action):
    rig, capability, task, action = authority_action
    original_response = rig.client._request.return_value

    def revoke(*_args, **_kwargs):
        rig.context_path.write_bytes(b"{}")
        return original_response

    rig.client._request.side_effect = revoke
    with pytest.raises(ValueError, match="changed/revoked"):
        capability(task, action)


def test_expiry_during_source_read_denies_before_http(authority_action, monkeypatch):
    rig, capability, task, action = authority_action
    original_read = hooks.read_regular
    selected = str(Path(rig.review["source_roots"]["scientist"]) / "lab/api/app.py")

    def expire(path, limit):
        raw = original_read(path, limit)
        if str(path) == selected:
            rig.clocks.boottime = 110.0
        return raw

    monkeypatch.setattr(hooks, "read_regular", expire)
    with pytest.raises(ValueError, match="expired"):
        capability(task, action)
    rig.client._request.assert_not_called()
