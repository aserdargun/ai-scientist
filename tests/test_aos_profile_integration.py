"""Profile-output.v2 wiring with synthetic SQLite and no native/model execution."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from aos_admission_fixture import BOOT, fixture_current_admission, grant_for
from test_aos_profile_output import DIRECTORY, GENERATION, bonsai, decider

from lab.llm import aos_gpu_executor
from lab.llm.aos_gpu_broker import PeerGeneration, TurnReceipt
from lab.llm.aos_gpu_control_store import ControlStore
from lab.llm.aos_gpu_executor import (
    AOSProfile,
    BrokerExecutionError,
    BrokerOwnedTurnExecutor,
    ProfileRegistry,
    SystemdAOSProfileRuntime,
    TurnBudgets,
    authenticated_peer,
)
from lab.llm.aos_gpu_service import _private_json, load_profiles
from lab.llm.aos_profile_output import NAME, ProfileOutputError, canonical, load_contract


@pytest.fixture
def contract():
    bundle = json.loads((DIRECTORY / "bundle.json").read_bytes())
    return load_contract(DIRECTORY, hashlib.sha256(canonical(bundle)).hexdigest())


def make_profile(tmp_path, contract, *, bonsai_profile=False):
    kind = "bonsai-recovery" if bonsai_profile else "decider"
    profile_id = "aos.bonsai.recovery.v1" if bonsai_profile else "aos.decider.turn.v1"
    manifest = tmp_path / (kind + ".json")
    manifest.write_bytes(
        canonical(
            {"temperature": 0.0, "max_output_tokens": 512, "context_tokens": 16384, "parallel": 1}
        )
    )
    return AOSProfile(
        profile_id=profile_id,
        kind=kind,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        deployment_digest="a" * 64,
        python=Path("/usr/bin/python"),
        source_root=tmp_path,
        model_paths=(tmp_path,),
        budgets=TurnBudgets(10, 5, 15, 60),
        response_schema_sha256=contract.content_pin(profile_id),
        output_contract={"name": NAME, "version": 2, "bundle_sha256": contract.bundle_sha256},
    )


@pytest.fixture
def wired(tmp_path, monkeypatch, contract):
    profile = make_profile(tmp_path, contract)
    peer = PeerGeneration(
        1000, 123, 55, BOOT, "swapp-aos-gpu-fixture.service", "e" * 32, "/fixture/aos", 122, 54
    )
    monkeypatch.setattr("lab.llm.gpu_scheduler._boot_id", lambda: BOOT)
    monkeypatch.setattr("lab.llm.gpu_scheduler._process_identity_alive", lambda _identity: True)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    database = private / "arbiter.sqlite"
    store = ControlStore(
        database,
        clock=lambda: 100.0,
        boot_id=lambda: BOOT,
        peer_verifier=lambda value: value == asdict(peer),
        current_admission_verifier=fixture_current_admission,
    )
    executor = BrokerOwnedTurnExecutor(
        database,
        authenticator=SimpleNamespace(still_current=lambda actual: actual == peer),
        profiles=SimpleNamespace(get=lambda *_args: profile),
        lab_units={"aos": peer.unit, "lab": "swapp-lab-gpu-fixture.service"},
        runtime=SimpleNamespace(verify_drained=lambda _lease: False),
        clock=lambda: 100.0,
        control_store=store,
    )
    raw, response = decider()
    value = json.loads(raw)
    request_id = value["request_id"]
    request_hash = hashlib.sha256(raw).hexdigest()
    admission = grant_for(
        asdict(peer),
        profile.profile_id,
        profile.deployment_digest,
        profile.config_sha256,
        profile.response_schema_sha256,
    )

    def register(deadline=160.0):
        return store.register_intent(
            asdict(peer),
            request_id,
            request_hash,
            profile.profile_id,
            profile.deployment_digest,
            profile.config_sha256,
            profile.response_schema_sha256,
            deadline,
            {
                **asdict(profile.budgets),
                "max_output_tokens": profile.max_output_tokens,
                "context_tokens": profile.context_tokens,
            },
            admission=admission,
        )

    receipt = TurnReceipt(response, response["metrics"], **GENERATION)
    SystemdAOSProfileRuntime(
        database,
        units=SimpleNamespace(),
        gpu=SimpleNamespace(),
        work_root=private / "work",
        control_store=store,
    )

    def prepare():
        executor._intent(request_id, request_hash, profile, peer)
        with authenticated_peer(peer):
            executor.scheduler.submit(
                "aos",
                request_id,
                raw,
                activation_seconds=10,
                inference_seconds=5,
                total_seconds=15,
                queue_timeout_seconds=60,
            )
            executor._set_state(request_id, "queued")
            lease = executor.scheduler.try_acquire("aos", request_id)
            assert lease is not None
        executor._set_state(request_id, "running")
        with closing(executor._connect()) as connection:
            connection.execute(
                "INSERT INTO aos_gpu_child_bindings(owner,request_id,fencing_token,profile_id,"
                "deployment_digest,request_sha256,unit,nonce,workdir,launch_state,"
                "created_boottime,total_seconds,invocation_id,main_pid,main_start_ticks,boot_id,"
                "control_group) VALUES('aos',?,?,?,?,?,?,?,?, 'bound',100,15,?,?,?,?,?)",
                (
                    request_id,
                    lease.fencing_token,
                    profile.profile_id,
                    profile.deployment_digest,
                    hashlib.sha256(
                        aos_gpu_executor._profile_payload(profile, value["payload"])
                    ).hexdigest(),
                    receipt.unit,
                    "9" * 64,
                    str(private / "work"),
                    receipt.invocation_id,
                    receipt.main_pid,
                    77,
                    BOOT,
                    receipt.control_group,
                ),
            )
            connection.commit()

    return SimpleNamespace(
        profile=profile,
        peer=peer,
        executor=executor,
        store=store,
        raw=raw,
        receipt=receipt,
        request_id=request_id,
        request_hash=request_hash,
        register=register,
        admission=admission,
        prepare=prepare,
    )


def test_config_pin_is_immutable_and_changes_config_identity(tmp_path, contract):
    profile = make_profile(tmp_path, contract)
    legacy = replace(profile, output_contract=None)
    assert profile.config_sha256 != legacy.config_sha256
    with pytest.raises(TypeError):
        profile.output_contract["version"] = 1
    with pytest.raises(ValueError):
        replace(
            profile,
            output_contract={"name": NAME, "version": 2.0, "bundle_sha256": contract.bundle_sha256},
        )
    with pytest.raises(ValueError):
        replace(profile, output_contract={**profile.output_contract, "extra": 1})


def test_registry_denies_wrong_bundle_and_inner_pin(tmp_path, contract):
    profile = make_profile(tmp_path, contract)
    registry = ProfileRegistry(
        {
            "aos.decider.turn.v1": profile,
            "aos.bonsai.recovery.v1": profile,
            "aos.bonsai.vision.v1": profile,
        }
    )
    assert registry.get(profile.profile_id, profile.deployment_digest) == profile
    for changed in (
        replace(profile, response_schema_sha256="0" * 64),
        replace(profile, output_contract={**profile.output_contract, "bundle_sha256": "0" * 64}),
    ):
        registry._profiles[profile.profile_id] = changed
        with pytest.raises(BrokerExecutionError):
            registry.get(profile.profile_id, profile.deployment_digest)


def test_controlled_missing_contract_denies_before_registration_or_scheduler(wired):
    p = wired
    profile = replace(p.profile, output_contract=None)
    p.executor._profiles = SimpleNamespace(get=lambda *_args: profile)
    with pytest.raises(BrokerExecutionError, match="profile-output.v2"):
        p.executor.run_turn(
            peer=p.peer,
            request_id=p.request_id,
            request_bytes=p.raw,
            request_sha256=p.request_hash,
            profile_id=profile.profile_id,
            deployment_digest=profile.deployment_digest,
            payload=json.loads(p.raw)["payload"],
            deadline=time.monotonic() + 30,
            admission=p.admission,
        )
    with closing(p.executor._connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM aos_control_requests").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM gpu_turn_requests").fetchone()[0] == 0


def test_derived_schema_binding_requires_authorized_intent_and_never_resets_retry(wired):
    p = wired
    with pytest.raises(BrokerExecutionError, match="authorized intent"):
        p.executor._bind_output_request(p.raw, p.profile, p.peer)
    assert p.register() == 160.0
    p.executor._bind_output_request(p.raw, p.profile, p.peer)
    with closing(p.executor._connect()) as connection:
        before = dict(connection.execute("SELECT * FROM aos_gpu_output_bindings").fetchone())
        assert connection.execute("SELECT COUNT(*) FROM gpu_turn_requests").fetchone()[0] == 0
    assert p.register(300.0) == 160.0
    p.executor._bind_output_request(p.raw, p.profile, p.peer)
    with closing(p.executor._connect()) as connection:
        assert (
            dict(connection.execute("SELECT * FROM aos_gpu_output_bindings").fetchone()) == before
        )
    altered = json.loads(p.raw)
    altered["payload"]["request"]["options"][0]["id"] = "different"
    with pytest.raises(BrokerExecutionError):
        p.executor._bind_output_request(canonical(altered), p.profile, p.peer)


def test_validation_precedes_first_result_write_and_rechecks_replay(wired):
    p = wired
    p.register()
    p.executor._bind_output_request(p.raw, p.profile, p.peer)
    p.prepare()
    malformed = replace(p.receipt, usage={"unexpected": 0})
    with pytest.raises(ProfileOutputError):
        p.executor._store_result_ready(p.request_id, malformed)
    with closing(p.executor._connect()) as connection:
        row = connection.execute("SELECT * FROM aos_gpu_turn_results").fetchone()
        assert row["state"] == "running" and row["response_json"] is None
    with pytest.raises(ProfileOutputError, match="terminal child mismatch"):
        p.executor._store_result_ready(p.request_id, replace(p.receipt, main_pid=999))
    p.executor._store_result_ready(p.request_id, p.receipt)
    # Isolate the post-terminal validator; the actual terminal authority is covered
    # by publication tests. This synthetic callback never grants real GPU rights.
    p.store.authorize_cached_result = lambda _connection, _row: {"synthetic": True}
    with closing(p.executor._connect()) as connection:
        connection.execute(
            "UPDATE aos_gpu_output_bindings SET derived_result_schema_sha256=?", ("0" * 64,)
        )
        connection.commit()
    with pytest.raises(BrokerExecutionError, match="derived output schema"):
        p.executor._complete(p.request_id)


def test_bonsai_projection_is_applied_before_persistence(tmp_path, contract):
    profile = make_profile(tmp_path, contract, bonsai_profile=True)
    raw, response = bonsai(contract)
    executor = object.__new__(BrokerOwnedTurnExecutor)
    raw_usage = {key: response["usage"][key] for key in ("prompt_tokens", "completion_tokens")}
    receipt = TurnReceipt(response, raw_usage, **GENERATION)
    projected = executor._project_output_receipt(raw, profile, receipt)
    assert projected.response != response
    assert "timings" not in projected.response
    assert projected.usage == raw_usage
    with pytest.raises(BrokerExecutionError, match="runtime usage"):
        executor._project_output_receipt(raw, profile, replace(receipt, usage={"prompt_tokens": 0}))


def test_config_loader_rejects_duplicate_and_unknown_output_pin(tmp_path, contract):
    path = tmp_path / "config.json"
    path.write_text('{"schema":1,"schema":2}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        _private_json(path)
    base = make_profile(tmp_path, contract)
    entry = {
        "kind": base.kind,
        "manifest": str(base.manifest),
        "manifest_sha256": base.manifest_sha256,
        "deployment_digest": base.deployment_digest,
        "python": str(base.python),
        "model_paths": [str(tmp_path)],
        "budgets": asdict(base.budgets),
        "response_schema_sha256": base.response_schema_sha256,
        "temperature": 0.0,
        "max_output_tokens": 512,
        "context_tokens": 16384,
        "output_contract": {**base.output_contract, "unknown": True},
    }
    entries = {
        key: dict(entry)
        for key in ("aos.decider.turn.v1", "aos.bonsai.recovery.v1", "aos.bonsai.vision.v1")
    }
    path.write_bytes(
        canonical(
            {
                "schema": "swapp-aos-gpu-profiles.v1",
                "source_root": str(tmp_path),
                "profiles": entries,
            }
        )
    )
    with pytest.raises(ValueError, match="output contract"):
        load_profiles(path, source_root=tmp_path)
