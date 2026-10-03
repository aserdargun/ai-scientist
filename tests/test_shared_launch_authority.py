"""CPU authority fixtures with real local proc identity, no unit or GPU effects."""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
from aos_admission_fixture import binding_for, output_pin
from test_shared_launch_ledger import binding

from lab.llm.aos_gpu_control import ControlError, ControlPolicy
from lab.llm.aos_gpu_control_store import canonical, digest
from lab.llm.aos_gpu_executor import SystemdSocketPeerAuthenticator
from lab.llm.gpu_scheduler import SharedGpuScheduler, _process_cgroup
from lab.llm.shared_launch_authority import (
    LaunchPolicyDocument,
    SharedLaunchAuthority,
    SharedLaunchPolicy,
    broker_generation_current,
    process_generation,
)
from lab.llm.shared_launch_ledger import LaunchLedgerError, ServiceGeneration


def write(path, value):
    raw = canonical(value).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def directory_identity(path):
    info = path.stat()
    return {
        "version": "descriptor-workspace-v1",
        "path_sha256": digest({"workspace": str(path)}),
        "device": info.st_dev,
        "inode": info.st_ino,
        "owner_uid": info.st_uid,
    }


class Rig:
    def __init__(self, tmp_path, issuer=None, contract_sha256=None, manager=None):
        self.broker_live = True
        self.native_safe = True
        self.base = tmp_path
        self.process = process_generation(os.getpid())
        self.service_main = self.process
        self.service_invocation = "e" * 32
        self.service_members = {self.process.pid}
        self.phases = []
        self.now = time.clock_gettime(time.CLOCK_BOOTTIME)
        self.aos = tmp_path / "aos"
        self.scientist = tmp_path / "scientist"
        self.aos.mkdir()
        self.scientist.mkdir()
        (self.scientist / "scripts").mkdir()
        self.launcher = self.scientist / "scripts/aos_native_launch.py"
        self.launcher.write_text("# CPU fixture launcher\n")
        launcher_sha = hashlib.sha256(self.launcher.read_bytes()).hexdigest()
        self.source = self.aos / "source.py"
        self.source.write_text("# CPU fixture source\n")
        source_sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.launch_input = tmp_path / "reviewed-input.json"
        launch_input_sha = write(self.launch_input, {"fixture": True})
        self.python = tmp_path / "python-fixture"
        self.python.write_text("not executable: reviewed CPU bytes")
        python_files = {str(self.python): hashlib.sha256(self.python.read_bytes()).hexdigest()}
        config_files = {str(self.launch_input): launch_input_sha}
        profile = SimpleNamespace(
            profile_id="aos.decider.turn.v1",
            deployment_digest="a" * 64,
            manifest_sha256="b" * 64,
            config_sha256="c" * 64,
            response_schema_sha256="d" * 64,
            output_contract=output_pin(),
            source_root=self.aos,
        )
        self.profile = profile
        config = {
            "enabled": True,
            "source_files": {
                "aos": {"source.py": source_sha},
                "scientist": {"scripts/aos_native_launch.py": launcher_sha},
            },
            "profile_pins": {profile.profile_id: ControlPolicy._pin(profile)},
        }
        self.control_path = tmp_path / "control-policy.json"
        write(self.control_path, config)
        # Exercise actual ControlPolicy.verify/hash methods on an explicit fixture
        # object; this is not a deployable profile or an accepted live policy.
        self.control = ControlPolicy(
            None,
            profiles=SimpleNamespace(get=lambda *_: profile),
            source_root=self.scientist,
        )
        self.control.path = self.control_path
        self.control.config = config
        self.control.sha256 = digest(config)
        self.manager_base = tmp_path / "manager"
        self.manager_base.mkdir(mode=0o700)
        self.session = self.manager_base / ("app-" + "3" * 32)
        self.session.mkdir(mode=0o700)
        self.workspace = self.session / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.plan = {
            "version": "1",
            "app_session": self.session.name,
            "workspace": str(self.workspace),
            "session_directory": str(self.session),
            "database": str(self.session / "trajectory.sqlite3"),
            "predecessor_identity_sha256": None,
            "template": {"fixture": True},
            "template_sha256": digest({"fixture": True}),
        }
        self.plan_path = tmp_path / "plan.json"
        plan_sha = write(self.plan_path, self.plan)
        self.provision = {
            "version": "1",
            "preparation_only": True,
            "execution_authorized": False,
            "runtime_started": False,
            "runtime_authority": False,
            "plan_sha256": plan_sha,
            "template_sha256": self.plan["template_sha256"],
            "app_session": self.session.name,
            "boot_id": self.process.boot_id,
            "manager_base": str(self.manager_base),
            "session_directory": str(self.session),
            "workspace": str(self.workspace),
            "database": self.plan["database"],
            "directory_mode": "0700",
            "manager_identity": directory_identity(self.manager_base),
            "session_identity": directory_identity(self.session),
            "workspace_identity": directory_identity(self.workspace),
            "database_absent": True,
            "token_absent": True,
            "socket_absent": True,
            "lifecycle_absent": True,
            "workspace_empty": True,
        }
        self.provision_path = self.session / "shared-provision.json"
        provision_sha = write(self.provision_path, self.provision)
        self.activation = {
            "version": "1",
            "plan_sha256": plan_sha,
            "boot_id": self.process.boot_id,
            "clock": "CLOCK_BOOTTIME",
            "issued_monotonic": self.now - 1,
            "expires_monotonic": self.now + 60,
            "reviewed_launch_input_path": str(self.launch_input),
            "reviewed_launch_input_sha256": launch_input_sha,
        }
        self.activation_path = tmp_path / "activation.json"
        activation_sha = write(self.activation_path, self.activation)
        data = binding().model_dump(by_alias=True)
        self.grant = binding(
            **{
                **data,
                "issuer": (issuer or self.process).model_dump(),
                "manager": (manager or self.process).model_dump(),
                "boot_id": self.process.boot_id,
                "broker": {
                    **self.process.model_dump(),
                    "unit": "swapp-lab-gpu-broker.service",
                    "invocation_id": "a" * 32,
                    "control_group": _process_cgroup(os.getpid()),
                },
                "workspace": str(self.workspace),
                "workspace_device": self.workspace.stat().st_dev,
                "workspace_inode": self.workspace.stat().st_ino,
                "plan_sha256": plan_sha,
                "provision_sha256": provision_sha,
                "issued_boottime": self.now - 1,
                "expires_boottime": self.now + 60,
                "prerequisite_deadline_boottime": self.now + 60,
                "pins": {
                    **data["pins"],
                    "aos_root": str(self.aos),
                    "scientist_root": str(self.scientist),
                    "aos_import_closure_sha256": digest(config["source_files"]["aos"]),
                    "scientist_import_closure_sha256": digest(config["source_files"]["scientist"]),
                    "python_identity_sha256": digest(python_files),
                    "launcher_sha256": launcher_sha,
                    "config_map_sha256": digest(config_files),
                    "reviewed_launch_input_sha256": launch_input_sha,
                    "policy_revision_sha256": self.control.sha256,
                    "model_profile_sha256": digest(config["profile_pins"]),
                    "contract_sha256": contract_sha256 or data["pins"]["contract_sha256"],
                },
            }
        )
        self.policy_path = tmp_path / "launch-policy.json"
        self.policy_document = {
            "schema": "swapp-shared-launch-policy.v1-proposal2.draft",
            "enabled": True,
            "contract_sha256": self.grant.pins.contract_sha256,
            "entries": [
                {
                    "binding": self.grant.model_dump(mode="json", by_alias=True),
                    "plan_path": str(self.plan_path),
                    "activation_path": str(self.activation_path),
                    "activation_raw_sha256": activation_sha,
                    "reviewed_launch_input_path": str(self.launch_input),
                    "config_files": config_files,
                    "python_files": python_files,
                    "control_issued_boottime": self.now - 1,
                    "control_expires_boottime": self.now + 120,
                }
            ],
        }
        self.policy = SharedLaunchPolicy(
            self.policy_path,
            expected_raw_sha256=write(self.policy_path, self.policy_document),
        )
        self.database = tmp_path / "arbiter.sqlite3"
        SharedGpuScheduler(self.database, principal_resolver=self, drain_verifier=lambda _: False)
        self.database.chmod(0o600)
        self.authority = SharedLaunchAuthority(
            self.database,
            policy=self.policy,
            control_policy=self.control,
            contract_sha256=self.grant.pins.contract_sha256,
            broker_current=lambda *_: self.broker_live,
            prerequisites=self.prerequisite,
            authenticator_factory=self.authenticator,
            draft_enabled=True,
        )
        self.authority.ledger.initialize_draft_schema()
        self.intent = {
            "version": "1",
            "preparation_only": True,
            "execution_authorized": False,
            "runtime_started": False,
            "runtime_authority": False,
            "plan_sha256": plan_sha,
            "app_session": self.session.name,
            "boot_id": self.process.boot_id,
            "provision_sha256": provision_sha,
            "activation_sha256": activation_sha,
        }
        self.intent_path = self.session / "shared-launch-intent.json"

    def prerequisite(self, *_):
        self.phases.append(_[1])
        if not self.native_safe:
            raise LaunchLedgerError("fixture_native_unsafe")

    def authenticator(self, unit):
        """Real proc/peer checks, with explicit CPU systemd MainPID/cgroup observations."""
        authenticator = SystemdSocketPeerAuthenticator(
            unit, units=SimpleNamespace(cgroup_pids=lambda _: self.service_members)
        )
        authenticator._parent = lambda: (
            _process_cgroup(self.service_main.pid),
            self.service_invocation,
            self.service_main.pid,
            self.service_main.start_ticks,
        )
        return authenticator

    def consume(self):
        self.call("issue", peer=self.grant.issuer)
        sha = write(self.intent_path, self.intent)
        assert self.call("claim", intent_sha256=sha, peer=self.grant.manager)["consumed_now"]
        return sha

    def admission(self, pid=None):
        peer = self.authenticator(self.grant.unit).authenticate(
            pid or self.process.pid, os.getuid()
        )
        value = binding_for(
            asdict(peer),
            self.profile.profile_id,
            self.profile.deployment_digest,
            self.profile.config_sha256,
            self.profile.response_schema_sha256,
        )
        value.update(
            server_generation=self.grant.broker.model_dump(mode="json"),
            policy_sha256=self.control.sha256,
            source_fingerprints={
                key: digest(files) for key, files in self.control.config["source_files"].items()
            },
            profile_pin=ControlPolicy._pin(self.profile),
        )
        return value

    def call(self, operation, *, intent_sha256=None, peer=None):
        peer = peer or self.process
        return self.authority.handle(
            operation,
            self.grant.request_id,
            self.grant.sha256(),
            intent_sha256,
            peer_pid=peer.pid,
            peer_uid=peer.uid,
            deadline=time.clock_gettime(time.CLOCK_BOOTTIME) + 4,
        )


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def test_real_proc_identity_and_unreadable_failure(monkeypatch):
    actual = process_generation(os.getpid())
    assert actual.uid == os.getuid() and actual.pid == os.getpid()
    monkeypatch.setattr("lab.llm.shared_launch_authority._read_process_identity", lambda _: None)
    with pytest.raises(LaunchLedgerError, match="stale_generation"):
        process_generation(os.getpid())


def test_reviewed_issue_read_only_verify_durable_claim_and_duplicate(rig):
    assert not rig.call("issue")["status"]["consumed"]
    for _ in range(2):
        assert not rig.call("verify")["consumed_now"]
    sha = write(rig.intent_path, rig.intent)
    assert rig.call("claim", intent_sha256=sha)["consumed_now"]
    assert not rig.call("claim", intent_sha256=sha)["consumed_now"]
    assert rig.call("revoke")["status"]["cleanup_required"]
    with sqlite3.connect(rig.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gpu_turn_requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "change", ["source", "config", "policy", "base_policy", "broker", "native"]
)
def test_fresh_revocation_checks_prevent_consumption(rig, change):
    rig.call("issue")
    sha = write(rig.intent_path, rig.intent)
    if change == "source":
        rig.source.write_text("# changed source\n")
    elif change == "config":
        write(rig.launch_input, {"fixture": False})
    elif change == "policy":
        write(rig.policy_path, {**rig.policy_document, "enabled": False})
    elif change == "base_policy":
        write(rig.control_path, {**rig.control.config, "enabled": False})
    elif change == "broker":
        rig.broker_live = False
    else:
        rig.native_safe = False
    with pytest.raises((LaunchLedgerError, ControlError)):
        rig.call("claim", intent_sha256=sha)
    with sqlite3.connect(rig.database) as connection:
        assert (
            connection.execute(
                "SELECT consumed_boottime FROM aos_shared_launch_draft_v1"
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("change", ["missing", "extra_artifact", "provision", "intent"])
def test_exact_durable_scope_required(rig, change):
    rig.call("issue")
    sha = digest(rig.intent)
    if change != "missing":
        write(rig.intent_path, rig.intent)
    if change == "extra_artifact":
        (rig.workspace / "unexpected").write_text("artifact")
    elif change == "provision":
        write(rig.provision_path, {**rig.provision, "workspace_empty": False})
    elif change == "intent":
        write(rig.intent_path, {**rig.intent, "runtime_started": True})
    with pytest.raises((LaunchLedgerError, OSError)):
        rig.call("claim", intent_sha256=sha)


def test_absent_native_verifier_and_unauthorized_requester_deny(rig):
    rig.authority._prerequisites = None
    with pytest.raises(LaunchLedgerError, match="prerequisite_verifier_required"):
        rig.call("issue")
    with pytest.raises(LaunchLedgerError, match="unauthorized_principal"):
        rig.authority.handle(
            "issue",
            rig.grant.request_id,
            rig.grant.sha256(),
            None,
            peer_pid=os.getpid() + 1,
            peer_uid=os.getuid(),
            deadline=time.clock_gettime(time.CLOCK_BOOTTIME) + 4,
        )


def test_exited_issuer_does_not_block_manager_verify(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        issuer = process_generation(child.pid)
        rig = Rig(tmp_path, issuer=issuer)
        rig.call("issue", peer=issuer)
    finally:
        child.terminate()
        child.wait(timeout=2)
    assert not rig.call("verify")["status"]["consumed"]
    with pytest.raises(LaunchLedgerError, match="stale_generation"):
        rig.call("revoke", peer=issuer)


def test_status_and_revoke_use_original_control_scope_after_launch_expiry(rig, monkeypatch):
    rig.call("issue")
    rig.authority.ledger._clock = lambda: rig.grant.expires_boottime + 1
    assert rig.call("status")["status"]["expired"]
    assert rig.call("revoke")["status"]["state"] == "revoked"
    monkeypatch.setattr(
        "lab.llm.shared_launch_authority._boottime",
        lambda: rig.now + 121,
    )
    with pytest.raises(LaunchLedgerError, match="deadline_exceeded"):
        rig.call("status")
    with pytest.raises(LaunchLedgerError, match="original_control_expired"):
        rig.authority.handle(
            "status",
            rig.grant.request_id,
            rig.grant.sha256(),
            None,
            peer_pid=rig.process.pid,
            peer_uid=rig.process.uid,
            deadline=rig.now + 125,
        )


def test_remote_broker_helper_checks_actual_proc_and_replaced_invocation(rig, monkeypatch):
    expected = rig.grant.broker
    values = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "MainPID": str(expected.pid),
        "InvocationID": expected.invocation_id,
        "ControlGroup": expected.control_group,
    }
    monkeypatch.setattr("lab.llm.shared_launch_authority._systemctl_show", lambda *_, **__: values)
    end = time.clock_gettime(time.CLOCK_BOOTTIME) + 4
    assert broker_generation_current(expected, end)
    values["InvocationID"] = "f" * 32
    assert not broker_generation_current(expected, end)
    changed = ServiceGeneration.model_validate(
        {**expected.model_dump(), "start_ticks": expected.start_ticks + 1}
    )
    assert not broker_generation_current(changed, end)


def test_policy_closed_and_default_disabled(rig):
    value = {**rig.policy_document, "unexpected": True}
    with pytest.raises(ValueError):
        LaunchPolicyDocument.model_validate(value)
    disabled = SharedLaunchPolicy(None, expected_raw_sha256="0" * 64)
    with pytest.raises(LaunchLedgerError, match="launch_policy_disabled"):
        disabled.verify_current(time.clock_gettime(time.CLOCK_BOOTTIME) + 4)


def test_control_revocation_during_final_broker_read_rolls_back_claim(rig):
    rig.call("issue")
    sha = write(rig.intent_path, rig.intent)
    calls = []

    def broker_read(*_):
        calls.append(True)
        if len(calls) == 2:
            write(rig.control_path, {**rig.control.config, "enabled": False})
        return True

    rig.authority._broker_current = broker_read
    with pytest.raises(ControlError, match="capability_mismatch"):
        rig.call("claim", intent_sha256=sha)
    with sqlite3.connect(rig.database) as connection:
        assert (
            connection.execute(
                "SELECT consumed_boottime FROM aos_shared_launch_draft_v1"
            ).fetchone()[0]
            is None
        )


def test_actual_socket_authority_policy_and_ledger_composition(tmp_path):
    from lab.llm import shared_launch_transport as wire

    rig = Rig(tmp_path, contract_sha256=wire.WIRE_SCHEMA_SHA256)
    workers, errors = [], []

    def current(expected, deadline):
        # External systemd broker observation is a fixture; peer/process auth,
        # Unix credentials, private policy, file reads and ledger are real CPU.
        return expected == rig.grant.broker and time.clock_gettime(time.CLOCK_BOOTTIME) < deadline

    server = wire.SharedLaunchServer(
        rig.authority,
        expected_broker=rig.grant.broker,
        broker_current=current,
    )

    def factory(deadline):
        producer, consumer = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        for endpoint in (producer, consumer):
            endpoint.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)

        def run():
            try:
                with producer:
                    server.serve_once(producer, deadline=deadline)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        workers.append(worker)
        worker.start()
        return consumer

    client = wire.SharedLaunchClient(
        factory,
        expected_broker=rig.grant.broker,
        broker_current=current,
    )

    def end():
        return time.clock_gettime(time.CLOCK_BOOTTIME) + 4

    request_id, sha = rig.grant.request_id, rig.grant.sha256()
    assert not client.request("issue", request_id, sha, deadline=end()).status.consumed
    assert not client.request("verify", request_id, sha, deadline=end()).consumed_now
    intent_sha = write(rig.intent_path, rig.intent)
    assert client.require_fresh_claim(request_id, sha, intent_sha, deadline=end()).consumed_now
    with pytest.raises(wire.SharedLaunchTransportError):
        client.require_fresh_claim(request_id, sha, intent_sha, deadline=end())
    assert client.request("status", request_id, sha, deadline=end()).status.consumed
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert not errors


def test_actual_mainpid_entry_and_runtime_allow_runtime_files_but_keep_original_facts(rig):
    sha = rig.consume()
    with sqlite3.connect(rig.database) as connection:
        original = connection.execute("SELECT * FROM aos_shared_launch_draft_v1").fetchall()
    (rig.workspace / "runtime-file").write_text("CPU fixture runtime artifact")
    (rig.session / "trajectory.sqlite3").write_bytes(b"CPU fixture database")
    result = rig.call("enter", intent_sha256=sha)
    assert result["consumed_now"] is False
    assert set(result["status"]) == {
        "request_id",
        "binding_sha256",
        "state",
        "consumed",
        "cleanup_required",
        "expired",
    }
    assert result["status"]["state"] == "consumed"
    assert result["status"]["consumed"] and result["status"]["cleanup_required"]
    assert not result["status"]["expired"]
    assert rig.call("enter", intent_sha256=sha) == result
    assert rig.call("verify_runtime", intent_sha256=sha) == result
    assert {"enter", "verify_runtime"} <= set(rig.phases)
    with sqlite3.connect(rig.database) as connection:
        assert connection.execute("SELECT * FROM aos_shared_launch_draft_v1").fetchall() == original
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM aos_shared_launch_entries_draft_v1"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT active_token FROM gpu_turn_state").fetchone()[0] == 0


def test_missing_entry_denies_runtime_and_existing_transaction_does_not_open_another(
    rig, monkeypatch
):
    sha = rig.consume()
    admission = rig.admission()
    with sqlite3.connect(rig.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(LaunchLedgerError, match="entry_required"):
            rig.authority.verify_runtime(connection, admission, deadline=rig.now + 4)
        connection.rollback()
    rig.call("enter", intent_sha256=sha)
    with sqlite3.connect(rig.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(
            rig.authority.ledger,
            "_transaction",
            lambda **_: pytest.fail("Nested ledger transaction"),
        )
        before = connection.total_changes
        assert rig.authority.verify_runtime(connection, admission, deadline=rig.now + 4) is None
        assert connection.in_transaction and connection.total_changes == before


def test_runtime_authenticates_service_descendant_but_wire_entry_requires_mainpid(rig):
    sha = rig.consume()
    rig.call("enter", intent_sha256=sha)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        descendant = process_generation(child.pid)
        rig.service_members.add(child.pid)
        with pytest.raises(LaunchLedgerError, match="service_mainpid_required"):
            rig.call("enter", intent_sha256=sha, peer=descendant)
        with sqlite3.connect(rig.database) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rig.authority.verify_runtime(
                connection,
                rig.admission(child.pid),
                deadline=time.clock_gettime(time.CLOCK_BOOTTIME) + 4,
            )
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_exited_manager_does_not_block_service_entry_or_runtime(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        manager = process_generation(child.pid)
        rig = Rig(tmp_path, manager=manager)
        sha = rig.consume()
    finally:
        child.terminate()
        child.wait(timeout=2)
    assert rig.call("enter", intent_sha256=sha)["status"]["consumed"]
    with sqlite3.connect(rig.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        rig.authority.verify_runtime(connection, rig.admission(), deadline=rig.now + 4)


@pytest.mark.parametrize("change", ["namespace", "invocation", "policy", "expired"])
def test_entry_final_identity_policy_and_original_expiry_checks_roll_back(rig, monkeypatch, change):
    sha = rig.consume()
    original = rig.authority._prerequisites

    def slow(binding, operation, deadline):
        original(binding, operation, deadline)
        if operation != "enter":
            return
        if change == "namespace":
            info = Path(f"/proc/{os.getpid()}/ns/pid").stat()
            monkeypatch.setattr(
                "lab.llm.shared_launch_authority._pid_namespace",
                lambda _: (info.st_dev, info.st_ino + 1),
            )
        elif change == "invocation":
            rig.service_invocation = "f" * 32
        elif change == "policy":
            write(rig.control_path, {**rig.control.config, "enabled": False})
        else:
            rig.authority.ledger._clock = lambda: rig.grant.expires_boottime

    rig.authority._prerequisites = slow
    with pytest.raises((LaunchLedgerError, ControlError)):
        rig.call("enter", intent_sha256=sha)
    with sqlite3.connect(rig.database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM aos_shared_launch_entries_draft_v1"
            ).fetchone()[0]
            == 0
        )
        state = connection.execute(
            "SELECT state,cleanup_required FROM aos_shared_launch_draft_v1"
        ).fetchone()
        assert state == ("consumed", 1)


@pytest.mark.parametrize(
    "change",
    [
        "policy",
        "launch-policy",
        "source",
        "workspace",
        "invocation",
        "broker",
        "namespace",
        "revoked",
        "expired",
        "missing-prerequisites",
        "profile",
    ],
)
def test_runtime_rechecks_original_live_authority_and_preserves_cleanup(rig, monkeypatch, change):
    sha = rig.consume()
    rig.call("enter", intent_sha256=sha)
    admission = rig.admission()
    if change == "policy":
        write(rig.control_path, {**rig.control.config, "enabled": False})
    elif change == "launch-policy":
        write(rig.policy_path, {**rig.policy_document, "enabled": False})
    elif change == "source":
        rig.source.write_text("source changed")
    elif change == "workspace":
        rig.workspace.rename(rig.session / "old-workspace")
        rig.workspace.mkdir(mode=0o700)
    elif change == "invocation":
        rig.service_invocation = "f" * 32
    elif change == "broker":
        rig.broker_live = False
    elif change == "namespace":
        info = Path(f"/proc/{os.getpid()}/ns/pid").stat()
        monkeypatch.setattr(
            "lab.llm.shared_launch_authority._pid_namespace",
            lambda _: (info.st_dev, info.st_ino + 1),
        )
    elif change == "revoked":
        rig.call("revoke")
    elif change == "expired":
        rig.authority.ledger._clock = lambda: rig.grant.expires_boottime
    elif change == "missing-prerequisites":
        rig.authority._prerequisites = None
    else:
        admission["profile_pin"]["deployment_digest"] = "f" * 64
    with sqlite3.connect(rig.database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises((LaunchLedgerError, ControlError)):
            rig.authority.verify_runtime(connection, admission, deadline=rig.now + 4)
        assert connection.in_transaction
        assert (
            connection.execute(
                "SELECT cleanup_required FROM aos_shared_launch_draft_v1"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM aos_shared_launch_entries_draft_v1"
            ).fetchone()[0]
            == 1
        )
