"""CPU fixtures: retained proof recovery never writes DBs or creates GPU authority."""

from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest
from test_aos_no_admission_providers import ProviderRig, pin

from lab.llm import aos_no_admission_observation as observation
from lab.llm import aos_no_admission_providers as readonly
from lab.llm import aos_no_admission_recovery as recovery
from lab.llm import aos_no_admission_recovery_providers as providers
from lab.llm import gpu_scheduler

_REAL_SYSTEMCTL_SHOW = gpu_scheduler._systemctl_show


class RecoveryRig:
    def __init__(self, tmp_path, monkeypatch):
        self.base = ProviderRig(tmp_path, monkeypatch)
        old = self.base.observation
        old.observer["generation"]["unit"] = readonly.UNIT
        old.observer["generation"]["control_group"] = "/fixture/" + readonly.UNIT
        previous_command = readonly._command
        self.observer_show = {
            "Id": readonly.UNIT,
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
            "InvocationID": "",
            "ControlGroup": "",
        }

        def command(arguments, **kwargs):
            if arguments[0] == "/usr/bin/systemctl":
                assert arguments[-1] == readonly.UNIT
                return subprocess.CompletedProcess(
                    arguments,
                    0,
                    "".join(
                        f"{key}={value}\n" for key, value in self.observer_show.items()
                    ).encode(),
                    b"",
                )
            return previous_command(arguments, **kwargs)

        monkeypatch.setattr(readonly, "_command", command)
        old.store = old.reopen(
            observer_authority=self.base.provider.observer_authority,
            original_reader=self.base.provider.original_reader,
            physical_reader=self.base.provider.physical_reader,
        )
        self.old = old.observe()
        self.raw = observation.canonical(self.old)
        self.old_raw_pin = pin(tmp_path / "old-observation.json", self.old)
        self.old_cfg_pin = pin(tmp_path / "old-config.json", self.base.config)
        snapshots = []
        for number, item in enumerate(self.base.config["sources"]):
            path = tmp_path / f"retained-source-{number}.py"
            path.write_bytes(Path(item["path"]).read_bytes())
            path.chmod(0o600)
            snapshots.append(
                {
                    "original_path": item["path"],
                    "retained_path": str(path),
                    "sha256": item["sha256"],
                }
            )
        sources = [
            Path(providers.__file__),
            Path(readonly.__file__),
            Path(recovery.__file__),
            recovery.SCHEMA_PATH,
            Path(observation.__file__),
            observation.SCHEMA_PATH,
            Path(gpu_scheduler.__file__),
            Path(providers.__file__).with_name("aos_no_admission_store.py"),
            Path(providers.__file__).with_name("aos_gpu_control_store.py"),
            Path(providers.__file__).resolve().parents[2]
            / "scripts/aos_no_admission_recovery_observer.py",
        ]
        current = [
            {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sources
        ]
        self.now = old.base.now + 120
        monkeypatch.setattr(gpu_scheduler, "boottime", lambda: self.now)
        self.generation = {
            **self.old["observer"]["generation"],
            "pid": 999,
            "start_ticks": 888,
            "unit": providers.UNIT,
            "invocation_id": "e" * 32,
            "control_group": "/fixture/" + providers.UNIT,
        }
        monkeypatch.setattr(providers, "_generation", lambda: dict(self.generation))
        self.authorization = {
            "schema": providers.AUTHORIZATION_SCHEMA,
            "version": 1,
            "target": old.target,
            "recovery_request_id": "c" * 32,
            "unit": providers.UNIT,
            "purpose": recovery.PURPOSE,
            "source_sha256": observation.digest(current),
            "cleanup_scope_sha256": observation.digest(self.old["cleanup_scope"]),
            "observation_raw_sha256": self.old_raw_pin["sha256"],
            "freshness": {
                "clock": "CLOCK_BOOTTIME",
                "unit": "microseconds",
                "boot_id": old.base.boot,
                "issued_boottime_us": int(self.now * 1_000_000),
                "expires_boottime_us": int((self.now + 60) * 1_000_000),
                "max_age_us": 60_000_000,
            },
            "inference_allowed": False,
            "gpu_release_allowed": False,
            "observation_reissue_allowed": False,
        }
        with readonly._database(self.base.config["canonical_database"]) as connection:
            canonical_hash = providers.canonical_schema_fingerprint(connection)
        with readonly._database(self.base.config["aos_database"]) as connection:
            aos_hash = providers.schema_fingerprint(connection)
        self.config = {
            "schema": providers.CONFIG_SCHEMA,
            "version": 1,
            "target": old.target,
            "current_sources": current,
            "authorization": pin(tmp_path / "recovery-authorization.json", self.authorization),
            "retained_config": self.old_cfg_pin,
            "retained_observation": self.old_raw_pin,
            "retained_sources": snapshots,
            "cleanup_scope": self.old["cleanup_scope"],
            "canonical_database": {
                **self.base.config["canonical_database"],
                "schema_sha256": canonical_hash,
            },
            "aos_database": {
                key: self.base.config["aos_database"][key] for key in ("path", "identity")
            },
            "cleanup_evidence": self.base.config["cleanup_evidence"],
            "recovery_schema_sha256": recovery.schema_hash(),
        }
        self.config["aos_database"]["schemas"] = {"27": aos_hash}
        self.path = tmp_path / "recovery-config.json"
        self.provider = self.reload()

    def reload(self):
        config_pin = pin(self.path, self.config)
        return providers.ReadOnlyRecoveryProviders(self.path, config_pin["sha256"])

    def reauthorize(self):
        self.config["authorization"] = pin(
            Path(self.config["authorization"]["path"]), self.authorization
        )
        self.provider = self.reload()

    def snapshot(self):
        return (
            self.base.aos.read_bytes(),
            self.base.observation.control.database.read_bytes(),
            Path(self.old_raw_pin["path"]).read_bytes(),
        )


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return RecoveryRig(tmp_path, monkeypatch)


def test_expired_original_can_be_verified_without_reissue_or_mutation(rig):
    before = rig.snapshot()
    raw = rig.provider.context()
    context = observation.decode(raw)
    assert rig.now * 1_000_000 > rig.old["freshness"]["expires_boottime_us"]
    assert context["freshness"] == rig.authorization["freshness"]
    assert context["retained"]["observation_raw_sha256"] == hashlib.sha256(rig.raw).hexdigest()
    assert context["cleanup_scope"] == rig.old["cleanup_scope"]
    assert rig.provider.context() == raw
    assert rig.provider.read_closure(raw) is False
    assert rig.snapshot() == before


def test_cpu_observer_absence_preserves_actual_gpu_namespace_restriction(rig, monkeypatch):
    monkeypatch.setattr(gpu_scheduler, "_systemctl_show", _REAL_SYSTEMCTL_SHOW)

    def systemctl(arguments, **kwargs):
        assert arguments[0] == "/usr/bin/systemctl"
        assert arguments[-1] != readonly.UNIT
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(
            arguments,
            0,
            "LoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n",
            "",
        )

    monkeypatch.setattr(gpu_scheduler.subprocess, "run", systemctl)
    # The actual GPU helper must keep denying the CPU observer namespace.
    with pytest.raises(ValueError, match="outside the fixed deployment namespace"):
        gpu_scheduler._systemctl_show(readonly.UNIT, owner="lab")
    before = rig.snapshot()
    assert observation.decode(rig.provider.context())["purpose"] == recovery.PURPOSE
    assert rig.snapshot() == before


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_loaded_old_observer_requires_exact_retained_invocation(rig, state):
    rig.observer_show.update(
        LoadState="loaded",
        ActiveState=state,
        InvocationID=rig.old["observer"]["generation"]["invocation_id"],
    )
    assert observation.decode(rig.provider.context())["purpose"] == recovery.PURPOSE


@pytest.mark.parametrize(
    "change",
    [
        {"Id": "unrelated.service"},
        {"LoadState": "error"},
        {"LoadState": "masked"},
        {"MainPID": "1234"},
        {"ControlGroup": "/recreated/unit"},
        {"ActiveState": "active"},
        {"ActiveState": "activating"},
        {"ActiveState": "deactivating"},
        {"InvocationID": "f" * 32},
        {"LoadState": "loaded", "InvocationID": "f" * 32},
        {"LoadState": "loaded"},
    ],
)
def test_wrong_recreated_or_unknown_cpu_observer_unit_denies(rig, change):
    rig.observer_show.update(change)
    before = rig.snapshot()
    with pytest.raises(readonly.ProviderDenied, match="old_observer_unit_not_absent"):
        rig.provider.context()
    assert rig.provider._issued is None
    assert rig.snapshot() == before


@pytest.mark.parametrize("kind", ["permission", "duplicate", "missing", "garbage"])
def test_cpu_observer_unknown_systemctl_response_is_not_absence(rig, monkeypatch, kind):
    previous = readonly._command

    def command(arguments, **kwargs):
        result = previous(arguments, **kwargs)
        if arguments[0] != "/usr/bin/systemctl":
            return result
        if kind == "permission":
            return subprocess.CompletedProcess(arguments, 1, b"", b"Access denied")
        output = result.stdout
        if kind == "duplicate":
            output += b"MainPID=0\n"
        elif kind == "missing":
            output = output.replace(b"MainPID=0\n", b"")
        else:
            output += b"unrecognized response\n"
        return subprocess.CompletedProcess(arguments, 0, output, b"")

    monkeypatch.setattr(readonly, "_command", command)
    with pytest.raises(readonly.ProviderDenied, match="old_observer_unit_"):
        rig.provider.context()


@pytest.mark.parametrize(
    "field,value",
    [
        ("unit", "other.service"),
        ("control_group", "/fixture/other.service"),
        ("control_group", "/../" + readonly.UNIT),
        ("start_ticks", 0),
        ("invocation_id", ""),
        ("boot_id", "wrong-boot"),
    ],
)
def test_cpu_observer_checker_is_scoped_to_pinned_generation(rig, field, value):
    generation = {**rig.old["observer"]["generation"], field: value}
    with pytest.raises(readonly.ProviderDenied, match="old_observer_generation_mismatch"):
        providers._old_observer_gone(generation)


def test_cpu_observer_process_permission_failure_denies(rig, monkeypatch):
    pid = rig.old["observer"]["generation"]["pid"]

    def identity(actual):
        if actual == pid:
            raise PermissionError("fixture proc unavailable")
        return None

    monkeypatch.setattr(gpu_scheduler, "_read_process_identity", identity)
    with pytest.raises(readonly.ProviderDenied, match="old_observer_identity_unknown"):
        rig.provider.context()


@pytest.mark.parametrize("present", [True, False])
def test_cpu_observer_cgroup_present_or_unreadable_denies(rig, monkeypatch, present):
    path = "/sys/fs/cgroup" + rig.old["observer"]["generation"]["control_group"]
    lstat = Path.lstat

    def read_stat(self):
        if str(self) == path:
            if present:
                return object()
            raise PermissionError("fixture cgroup unavailable")
        return lstat(self)

    monkeypatch.setattr(Path, "lstat", read_stat)
    with pytest.raises(
        readonly.ProviderDenied, match="old_cgroup_present|old_observer_cgroup_unknown"
    ):
        rig.provider.context()


def test_cpu_observer_recreated_during_physical_check_denies(rig, monkeypatch):
    previous = readonly._command

    def command(arguments, **kwargs):
        result = previous(arguments, **kwargs)
        if arguments[0] == "/usr/bin/docker":
            rig.observer_show.update(LoadState="loaded", InvocationID="f" * 32)
        return result

    monkeypatch.setattr(readonly, "_command", command)
    with pytest.raises(readonly.ProviderDenied, match="old_observer_unit_not_absent"):
        rig.provider.context()


def test_inspect_never_calls_current_principal_authentication(rig, monkeypatch):
    def deny():
        raise readonly.ProviderDenied("shell_is_not_authority")

    monkeypatch.setattr(providers, "_generation", deny)
    assert rig.provider.inspect()["runtime_authorized"] is False
    with pytest.raises(readonly.ProviderDenied, match="shell_is_not_authority"):
        rig.provider.context()
    with pytest.raises(readonly.ProviderDenied, match="unissued_recovery_context"):
        rig.provider.read_closure(b"{}")


@pytest.mark.parametrize("offset", [-1, 60, 120])
def test_only_fixed_authorization_window_is_valid(rig, offset):
    rig.now = rig.authorization["freshness"]["issued_boottime_us"] / 1_000_000 + offset
    with pytest.raises(observation.ObservationError, match="freshness"):
        rig.provider.context()


def test_once_issued_context_is_not_refreshed_at_expiry(rig):
    raw = rig.provider.context()
    rig.now += 60
    with pytest.raises(observation.ObservationError, match="freshness"):
        rig.provider.read_closure(raw)
    assert rig.provider._issued == raw


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "observe_no_admission"),
        ("unit", readonly.UNIT),
        ("inference_allowed", True),
        ("gpu_release_allowed", True),
        ("observation_reissue_allowed", True),
    ],
)
def test_authorization_purpose_unit_and_permissions_are_closed(rig, field, value):
    rig.authorization[field] = value
    rig.reauthorize()
    with pytest.raises(readonly.ProviderDenied, match="recovery_authorization_mismatch"):
        rig.provider.context()


@pytest.mark.parametrize(
    "column,value",
    [("generation", 2), ("lease_id", "changed"), ("status", "running"), ("owner", "AGENT")],
)
def test_same_stopped_paused_generation_and_lease_required(rig, column, value):
    rig.base.update(f"UPDATE desktop_sessions SET {column}=?", (value,))
    with pytest.raises(readonly.ProviderDenied, match="cleanup_scope_changed"):
        rig.provider.context()


@pytest.mark.parametrize("owner", ["aos", "lab"])
@pytest.mark.parametrize("state", ["prepared", "created", "uncertain"])
def test_runtime_dispatch_provenance_blocks_recovery(rig, owner, state):
    rig.base.observation.runtime_binding(owner=owner, state=state, token=97)
    before = rig.snapshot()
    with pytest.raises(RuntimeError, match="request_conflict"):
        rig.provider.context()
    assert rig.snapshot() == before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE deferred_dispatch(request_id TEXT)",
        "DROP TRIGGER aos_observations_no_delete",
        "DROP TABLE gpu_runtime_bindings",
    ],
)
def test_full_canonical_schema_is_pinned(rig, ddl):
    with closing(sqlite3.connect(rig.base.observation.control.database)) as connection:
        connection.execute(ddl)
        connection.commit()
    with pytest.raises(readonly.ProviderDenied, match="canonical_schema_changed"):
        rig.provider.context()


def test_private_historical_snapshot_is_required_even_with_current_sources(rig):
    Path(rig.config["retained_sources"][0]["retained_path"]).write_bytes(b"changed old source")
    with pytest.raises(readonly.ProviderDenied, match="artifact_changed"):
        rig.provider.context()


def test_changed_current_source_pin_cannot_use_old_retained_source(rig):
    rig.config["current_sources"][0]["sha256"] = "0" * 64
    rig.provider = rig.reload()
    with pytest.raises(readonly.ProviderDenied, match="artifact_changed"):
        rig.provider.context()


@pytest.mark.parametrize(
    "stdout,stderr,code",
    [
        (b"[{}]", None, 1),
        (b"[]", b"permission denied", 1),
        (b"[]", b"Cannot connect to daemon", 1),
        (b"[]", None, 0),
        (b"{}", None, 1),
    ],
)
def test_docker_errors_and_nonempty_stdout_never_prove_absence(
    rig, monkeypatch, stdout, stderr, code
):
    previous = readonly._command

    def command(arguments, **kwargs):
        if arguments[0] == "/usr/bin/docker":
            return subprocess.CompletedProcess(
                arguments,
                code,
                stdout,
                stderr or ("Error: No such container: " + "b" * 64).encode(),
            )
        return previous(arguments, **kwargs)

    monkeypatch.setattr(readonly, "_command", command)
    with pytest.raises(readonly.ProviderDenied, match="original_container_not_proven_absent"):
        rig.provider.context()


@pytest.mark.parametrize("which", ["caller", "broker", "observer"])
def test_every_old_process_generation_must_be_absent(rig, monkeypatch, which):
    old = {
        "caller": rig.old["physical"]["caller_generation"],
        "broker": rig.old["original"]["broker_generation"],
        "observer": rig.old["observer"]["generation"],
    }[which]
    monkeypatch.setattr(
        gpu_scheduler, "_read_process_identity", lambda pid: object() if pid == old["pid"] else None
    )
    with pytest.raises(readonly.ProviderDenied, match="old_pid_present_or_reused"):
        rig.provider.context()


def test_scope_change_during_physical_read_denies_context(rig, monkeypatch):
    previous = readonly._command

    def command(arguments, **kwargs):
        result = previous(arguments, **kwargs)
        if arguments[0] == "/usr/bin/docker":
            rig.base.update("UPDATE desktop_sessions SET lease_id='taken-over'")
        return result

    monkeypatch.setattr(readonly, "_command", command)
    with pytest.raises(readonly.ProviderDenied, match="cleanup_scope_changed"):
        rig.provider.context()


def test_target_binding_created_during_physical_read_denies_context(rig, monkeypatch):
    previous = readonly._command

    def command(arguments, **kwargs):
        result = previous(arguments, **kwargs)
        if arguments[0] == "/usr/bin/docker":
            rig.base.observation.runtime_binding(owner="lab", state="uncertain", token=97)
        return result

    monkeypatch.setattr(readonly, "_command", command)
    with pytest.raises(RuntimeError, match="request_conflict"):
        rig.provider.context()
    assert rig.provider._issued is None


def test_retained_child_generation_is_not_omitted_from_physical_checks(rig, monkeypatch):
    old = deepcopy(rig.old)
    child = {**old["observer"]["generation"], "pid": 12345}
    old["physical"]["children"] = [{"generation": child}]
    monkeypatch.setattr(
        gpu_scheduler, "_read_process_identity", lambda pid: object() if pid == 12345 else None
    )
    with pytest.raises(readonly.ProviderDenied, match="old_pid_present_or_reused"):
        rig.provider._physical(old, rig.base.config)


def test_current_generation_change_during_checks_denies_context(rig, monkeypatch):
    calls = []

    def generation():
        calls.append(True)
        return {**rig.generation, "start_ticks": 888 if len(calls) == 1 else 889}

    monkeypatch.setattr(providers, "_generation", generation)
    with pytest.raises(readonly.ProviderDenied, match="recovery_generation_changed"):
        rig.provider.context()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE scientist_turn_intents SET state='done'",
        "UPDATE scientist_turn_intents SET request_json='{}'",
        "UPDATE scientist_admission_history SET record_sha256='changed'",
    ],
)
def test_pending_original_request_and_capture_are_unchanged(rig, sql):
    rig.base.update(sql)
    with pytest.raises(readonly.ProviderDenied, match="original_mismatch"):
        rig.provider.context()


def test_context_source_change_after_construction_denies(rig, tmp_path):
    source = tmp_path / "current-extra-source.py"
    source.write_text("# reviewed current source\n")
    source.chmod(0o600)
    rig.config["current_sources"].append(
        {"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    )
    rig.authorization["source_sha256"] = observation.digest(rig.config["current_sources"])
    rig.reauthorize()
    source.write_text("# changed current source\n")
    with pytest.raises(readonly.ProviderDenied, match="artifact_changed"):
        rig.provider.context()


def test_normal_process_cannot_claim_fixed_recovery_unit(monkeypatch):
    monkeypatch.setattr(
        readonly,
        "_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [],
            0,
            b"LoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n",
            b"",
        ),
    )
    with pytest.raises(readonly.ProviderDenied, match="recovery_not_current_process"):
        providers._generation()


@pytest.mark.parametrize("tamper", [None, "context", "proof_bytes", "scope", "snapshot"])
def test_real_closure_ddl_readback_requires_same_context_proof_and_scope(rig, tamper):
    # Complete dependencies needed by the unmodified reviewed migration, all
    # in the isolated CPU fixture. The production provider never migrates.
    with closing(sqlite3.connect(rig.base.aos)) as connection:
        connection.executescript("""
            ALTER TABLE schema_migrations ADD COLUMN name TEXT;
            ALTER TABLE schema_migrations ADD COLUMN applied_at TEXT;
            CREATE UNIQUE INDEX fixture_intent_id ON scientist_turn_intents(request_id);
            CREATE UNIQUE INDEX fixture_session_id ON desktop_sessions(session_id);
            CREATE UNIQUE INDEX fixture_record_id ON scientist_admission_history(record_sha256);
            CREATE TABLE scientist_evidence_controls(control_id TEXT,request_id TEXT);
            CREATE TABLE scientist_evidence_responses(control_id TEXT);
            CREATE TABLE scientist_turn_resolutions(request_id TEXT);
            CREATE TRIGGER scientist_one_unresolved_turn BEFORE INSERT ON scientist_turn_intents
            BEGIN SELECT 1; END;
        """)
        connection.executescript(_CLOSURE_SQL)
        rig.config["aos_database"]["schemas"] = {"28": providers.schema_fingerprint(connection)}
    rig.provider = rig.reload()
    context = rig.provider.context()
    recovery_scope = {**rig.old["cleanup_scope"], "retained_recovery": observation.decode(context)}
    raw = rig.raw
    with closing(sqlite3.connect(rig.base.aos)) as connection:
        connection.row_factory = sqlite3.Row
        intent = dict(connection.execute("SELECT * FROM scientist_turn_intents").fetchone())
        if tamper == "context":
            recovery_scope["retained_recovery"]["recovery_request_id"] = "d" * 32
        elif tamper == "scope":
            recovery_scope["lease_id"] = "changed"
        elif tamper == "proof_bytes":
            raw += b"\n"
        elif tamper == "snapshot":
            intent["state"] = "wrong"
        connection.execute(
            "INSERT INTO scientist_no_admission_closures VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                rig.provider.target["request_id"],
                rig.old["cleanup_scope"]["session_id"],
                rig.provider.target["request_sha256"],
                rig.old["original"]["admission_record_sha256"],
                observation.canonical(intent).decode(),
                raw.decode(),
                rig.old["observation_sha256"],
                recovery.OBSERVATION_SCHEMA_SHA256,
                observation.canonical(recovery_scope).decode(),
                "fixture-now",
            ),
        )
        connection.commit()
    before = rig.snapshot()
    if tamper:
        with pytest.raises(readonly.ProviderDenied, match="recovery_closure_conflict"):
            rig.provider.read_closure(context)
    else:
        assert rig.provider.read_closure(context) is True
    assert rig.snapshot() == before


# Exact reviewed AOS migration DDL; execution is limited to the temporary fixture DB.
_CLOSURE_SQL = (
    "PRAGMA foreign_keys = ON;\n"
    "BEGIN IMMEDIATE;\n"
    "CREATE TABLE scientist_no_admission_closures (\n"
    "    request_id TEXT PRIMARY KEY REFERENCES scientist_turn_intents(reques"
    "t_id),\n"
    "    session_id TEXT NOT NULL REFERENCES desktop_sessions(session_id),\n"
    "    request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND req"
    "uest_sha256 NOT GLOB '*[^0-9a-f]*'),\n"
    "    admission_record_sha256 TEXT NOT NULL REFERENCES scientist_admission"
    "_history(record_sha256),\n"
    "    intent_snapshot_json TEXT NOT NULL CHECK(json_valid(intent_snapshot_"
    "json)),\n"
    "    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),\n"
    "    observation_sha256 TEXT NOT NULL UNIQUE CHECK(length(observation_sha"
    "256)=64 AND observation_sha256 NOT GLOB '*[^0-9a-f]*'),\n"
    "    schema_sha256 TEXT NOT NULL CHECK(schema_sha256='92791f45ef6a319a27a"
    "5aade363832b737978080826537b8a1ffa7eef700cd63'),\n"
    "    recovery_scope_json TEXT NOT NULL CHECK(json_valid(recovery_scope_js"
    "on)),\n"
    "    created_at TEXT NOT NULL,\n"
    "    CHECK(json_extract(observation_json,'$.schema')='aos-scientist-no-ad"
    "mission-observation.v1'),\n"
    "    CHECK(json_extract(observation_json,'$.version')=1),\n"
    "    CHECK(json_extract(observation_json,'$.outcome')='never_received'),\n"
    "    CHECK(json_type(observation_json,'$.admission_budget')='null'),\n"
    "    CHECK(json_extract(observation_json,'$.target.request_id')=request_i"
    "d),\n"
    "    CHECK(json_extract(observation_json,'$.target.request_sha256')=reque"
    "st_sha256),\n"
    "    CHECK(json_extract(observation_json,'$.observation_sha256')=observat"
    "ion_sha256),\n"
    "    CHECK(json_extract(observation_json,'$.original.admission_record_sha"
    "256')=admission_record_sha256),\n"
    "    CHECK(json_extract(recovery_scope_json,'$.session_id')=session_id)\n"
    ");\n"
    "CREATE TRIGGER scientist_no_admission_closures_no_update\n"
    "BEFORE UPDATE ON scientist_no_admission_closures\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT,'Scientist no-admission closure is immutable');\n"
    "END;\n"
    "CREATE TRIGGER scientist_no_admission_closures_no_replace\n"
    "BEFORE INSERT ON scientist_no_admission_closures\n"
    "WHEN EXISTS(SELECT 1 FROM scientist_no_admission_closures\n"
    "            WHERE request_id=NEW.request_id OR observation_sha256=NEW.ob"
    "servation_sha256)\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT,'Scientist no-admission closure cannot be replace"
    "d');\n"
    "END;\n"
    "CREATE TRIGGER scientist_no_admission_closures_target\n"
    "BEFORE INSERT ON scientist_no_admission_closures\n"
    "WHEN NOT EXISTS (\n"
    "    SELECT 1 FROM scientist_turn_intents intent\n"
    "    JOIN scientist_admission_history history ON history.request_id=inten"
    "t.request_id\n"
    "    JOIN desktop_sessions session ON session.session_id=intent.session_i"
    "d\n"
    "    WHERE intent.request_id=NEW.request_id AND intent.session_id=NEW.ses"
    "sion_id\n"
    "      AND intent.request_sha256=NEW.request_sha256 AND intent.state='pen"
    "ding' AND intent.receipt_json IS NULL\n"
    "      AND history.record_sha256=NEW.admission_record_sha256\n"
    "      AND json_extract(NEW.intent_snapshot_json,'$.request_json') IS int"
    "ent.request_json\n"
    "      AND json_extract(NEW.intent_snapshot_json,'$.binding_json') IS int"
    "ent.binding_json\n"
    "      AND json_extract(NEW.observation_json,'$.cleanup_scope.session_id'"
    ") IS session.session_id\n"
    "      AND json_extract(NEW.observation_json,'$.cleanup_scope.runtime_id'"
    ") IS session.runtime_id\n"
    "      AND json_extract(NEW.observation_json,'$.cleanup_scope.current_gen"
    "eration') IS session.generation\n"
    "      AND json_extract(NEW.observation_json,'$.cleanup_scope.lease_id') "
    "IS session.lease_id\n"
    "      AND session.owner='PAUSED' AND session.status='stopped'\n"
    ")\n"
    "BEGIN SELECT RAISE(ABORT,'Scientist observation closure target or curren"
    "t recovery scope differs'); END;\n"
    "CREATE TRIGGER scientist_no_admission_closures_pending_control\n"
    "BEFORE INSERT ON scientist_no_admission_closures\n"
    "WHEN EXISTS (\n"
    "    SELECT 1 FROM scientist_evidence_controls control\n"
    "    LEFT JOIN scientist_evidence_responses response ON response.control_"
    "id=control.control_id\n"
    "    WHERE control.request_id=NEW.request_id AND response.control_id IS N"
    "ULL\n"
    ")\n"
    "BEGIN SELECT RAISE(ABORT,'Scientist observation closure has unresolved c"
    "ontrol evidence'); END;\n"
    "CREATE TRIGGER scientist_observed_intent_immutable BEFORE UPDATE ON scie"
    "ntist_turn_intents\n"
    "WHEN EXISTS (SELECT 1 FROM scientist_no_admission_closures WHERE request"
    "_id=OLD.request_id)\n"
    "BEGIN SELECT RAISE(ABORT,'Observed Scientist original intent is immutabl"
    "e'); END;\n"
    "DROP TRIGGER scientist_one_unresolved_turn;\n"
    "CREATE TRIGGER scientist_one_unresolved_turn BEFORE INSERT ON scientist_"
    "turn_intents\n"
    "WHEN EXISTS (\n"
    "    SELECT 1 FROM scientist_turn_intents intent\n"
    "    LEFT JOIN scientist_turn_resolutions resolution ON resolution.reques"
    "t_id=intent.request_id\n"
    "    LEFT JOIN scientist_no_admission_closures closure ON closure.request"
    "_id=intent.request_id\n"
    "    WHERE intent.session_id=NEW.session_id AND resolution.request_id IS "
    "NULL AND closure.request_id IS NULL\n"
    ")\n"
    "BEGIN SELECT RAISE(ABORT,'Scientist session requires trusted resolution "
    "before new work'); END;\n"
    "CREATE TRIGGER scientist_no_admission_closures_no_delete\n"
    "BEFORE DELETE ON scientist_no_admission_closures\n"
    "BEGIN\n"
    "    SELECT RAISE(ABORT,'Scientist no-admission closure is immutable');\n"
    "END;\n"
    "INSERT INTO schema_migrations(version,name,applied_at)\n"
    "VALUES(28,'scientist_no_admission_closures',strftime('%Y-%m-%dT%H:%M:%fZ"
    "','now'));\n"
    "COMMIT;\n"
)
