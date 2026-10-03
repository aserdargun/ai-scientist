"""CPU provider fixtures. No real observer authority, AOS writes or GPU work."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from test_aos_no_admission_store import ObservationRig

from lab.llm import aos_no_admission_providers as providers
from lab.llm.aos_no_admission_observation import canonical, digest, schema_hash


def pin(path, value):
    path.write_bytes(canonical(value))
    path.chmod(0o600)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class ProviderRig:
    def __init__(self, tmp_path, monkeypatch):
        self.observation = ObservationRig(tmp_path, monkeypatch)
        old = self.observation
        self.aos = tmp_path / "aos.sqlite"
        with closing(sqlite3.connect(self.aos)) as connection:
            connection.executescript("""
                CREATE TABLE schema_migrations(version INTEGER);
                INSERT INTO schema_migrations VALUES(27);
                CREATE TABLE desktop_sessions(session_id TEXT,runtime_id TEXT,lease_id TEXT,
                    generation INTEGER,status TEXT,owner TEXT);
                CREATE TABLE scientist_turn_intents(request_id TEXT,session_id TEXT,
                    binding_json TEXT,request_json TEXT,request_sha256 TEXT,broker_peer_json TEXT,
                    state TEXT,receipt_json TEXT);
                CREATE TABLE scientist_admission_history(request_id TEXT,session_id TEXT,
                    request_sha256 TEXT,intent_binding_sha256 TEXT,admission_binding_sha256 TEXT,
                    record_sha256 TEXT,record_json TEXT);
            """)
            scope, original = old.cleanup, old.original
            connection.execute(
                "INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?)",
                (
                    scope["session_id"],
                    scope["runtime_id"],
                    scope["lease_id"],
                    scope["current_generation"],
                    "stopped",
                    "PAUSED",
                ),
            )
            connection.execute(
                "INSERT INTO scientist_turn_intents VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    old.target["request_id"],
                    scope["session_id"],
                    canonical(original["intent_binding"]).decode(),
                    original["request_canonical"],
                    old.target["request_sha256"],
                    canonical(
                        {
                            key: value
                            for key, value in original["broker_generation"].items()
                            if key != "unit"
                        }
                    ).decode(),
                    "pending",
                ),
            )
            connection.execute(
                "INSERT INTO scientist_admission_history VALUES(?,?,?,?,?,?,?)",
                (
                    old.target["request_id"],
                    scope["session_id"],
                    old.target["request_sha256"],
                    original["intent_binding_sha256"],
                    original["admission_binding_sha256"],
                    original["admission_record_sha256"],
                    canonical(original["admission_record"]).decode(),
                ),
            )
            connection.commit()
        self.aos.chmod(0o600)
        scope["store"] = providers.store_identity(self.aos)
        original["store"] = scope["store"]
        context = {
            "schema": "aos-scientist-no-admission-cleanup-authority.v1",
            "version": 1,
            "target": old.target,
            "cleanup_scope_without_context": {
                key: value for key, value in scope.items() if key != "authorization_context_sha256"
            },
            "observer_unit": providers.UNIT,
            "purpose": providers.PURPOSE,
            "inference_allowed": False,
        }
        scope["authorization_context_sha256"] = digest(context)
        source = Path(providers.__file__).resolve()
        sources = [{"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
        self.historical = {
            name: ("historical " + name).encode() for name in providers._HISTORICAL_REQUIRED
        }
        historical_files = [
            {"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
            for name, raw in sorted(self.historical.items())
        ]
        binding = original["admission_record"]["admission_binding"]
        receipt = {
            "reviewed_bindings": {binding["profile_id"]: binding},
            "snapshot": {
                "source_inputs": {
                    str(tmp_path / item["path"]): {"sha256": item["sha256"]}
                    for item in historical_files
                }
            },
        }
        receipt_pin = pin(tmp_path / "receipt.json", receipt)
        canonical_store = providers.store_identity(old.control.database)
        review = {
            "schema": "aos-scientist-no-admission-dispatch-review.v1",
            "version": 1,
            "target": old.target,
            "historical_source_fingerprints": binding["source_fingerprints"],
            "historical_receipt_sha256": receipt_pin["sha256"],
            "canonical_store": canonical_store,
            "ordering": "canonical_intent_before_queue_before_child",
            "deferred_storage": "canonical_only",
            "quarantine_storage": "canonical_only",
            "reviewed_source_manifest_sha256": digest(sources),
            "historical_scientist_commit": "a" * 40,
            "historical_source_files": historical_files,
        }
        cleanup = {"container_id": "b" * 64}
        for key, generation in (
            ("original_native", binding["caller_generation"]),
            ("original_broker", binding["server_generation"]),
        ):
            cleanup[key] = {
                "MainPID": str(generation["pid"]),
                "InvocationID": generation["invocation_id"],
                "ControlGroup": generation["control_group"],
            }
        self.config = {
            "schema": providers.CONFIG_SCHEMA,
            "version": 1,
            "target": old.target,
            "aos_database": {
                "path": str(self.aos),
                "identity": scope["store"],
                "schema_version": 27,
            },
            "canonical_database": {"path": str(old.control.database), "identity": canonical_store},
            "original": {
                "admission_record_sha256": original["admission_record_sha256"],
                "intent_binding_sha256": original["intent_binding_sha256"],
                "source_fingerprints": binding["source_fingerprints"],
            },
            "cleanup_scope": scope,
            "sources": sources,
            "cleanup_evidence": pin(tmp_path / "cleanup.json", cleanup),
            "dispatch_review": pin(tmp_path / "review.json", review),
            "observer": {"unit": providers.UNIT, "source_sha256": digest(sources)},
            "observation_schema_sha256": schema_hash(),
            "historical_receipt": receipt_pin,
            "historical_repository": str(tmp_path),
            "cleanup_authority": pin(tmp_path / "authority.json", context),
        }
        self.path = tmp_path / "provider.json"
        self.provider = self.reload()
        self.commands = []
        self.docker_ok = True

        def command(arguments, **_kwargs):
            self.commands.append(arguments)
            if arguments[0] == "/usr/bin/git":
                raw = self.historical[arguments[-1].split(":", 1)[1]]
                return subprocess.CompletedProcess(arguments, 0, raw, b"")
            if arguments[0] == "/usr/bin/docker":
                return subprocess.CompletedProcess(
                    arguments,
                    1,
                    b"[]\n",
                    (
                        "Error: No such container: " + "b" * 64
                        if self.docker_ok
                        else "Cannot connect to daemon"
                    ).encode(),
                )
            raise AssertionError("unexpected fixture command")

        monkeypatch.setattr(providers, "_command", command)
        monkeypatch.setattr(
            providers, "_observer_generation", lambda: dict(old.observer["generation"])
        )
        monkeypatch.setattr(providers.gpu_scheduler, "_boot_id", lambda: old.base.boot)
        monkeypatch.setattr(providers.gpu_scheduler, "_read_process_identity", lambda _pid: None)
        monkeypatch.setattr(
            providers.gpu_scheduler,
            "_systemctl_show",
            lambda *_args, **_kwargs: {
                "MainPID": "0",
                "ActiveState": "inactive",
                "ControlGroup": "",
            },
        )

    def reload(self):
        item = pin(self.path, self.config)
        return providers.ReadOnlyObservationProviders(self.path, item["sha256"])

    def update(self, sql, parameters=()):
        with closing(sqlite3.connect(self.aos)) as connection:
            connection.execute(sql, parameters)
            connection.commit()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return ProviderRig(tmp_path, monkeypatch)


def test_real_readonly_readers_reconstruct_original_and_scope(rig):
    before = rig.aos.read_bytes()
    summary = rig.provider.inspect()
    assert summary["runtime_authorized"] is False and "original" not in summary
    assert rig.provider.original_reader(rig.provider.target) == canonical(rig.observation.original)
    authority = rig.provider.observer_authority(rig.provider.target)
    assert json.loads(authority.cleanup_scope) == rig.observation.cleanup
    assert rig.provider.aos_database == rig.aos
    assert rig.provider.canonical_database == rig.observation.control.database
    assert rig.provider.read_closure("f" * 64) is False
    assert rig.aos.read_bytes() == before


def test_physical_absence_depends_on_sources_sql_processes_and_exact_container(rig):
    before = rig.aos.read_bytes()
    result = rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))
    assert json.loads(result.dispatch_provenance)["deferred_execution"] == []
    assert json.loads(result.physical)["provenance_complete"] is True
    assert rig.aos.read_bytes() == before
    docker = next(call for call in rig.commands if call[0] == "/usr/bin/docker")
    assert docker == [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "container",
        "inspect",
        "b" * 64,
    ]
    assert all(
        "--no-ext-diff" in call and "--no-textconv" in call
        for call in rig.commands
        if call[0] == "/usr/bin/git"
    )


@pytest.mark.parametrize(
    "column,value",
    [("owner", "AGENT"), ("status", "running"), ("lease_id", "wrong"), ("generation", 2)],
)
def test_current_scope_changes_revoke_authority(rig, column, value):
    rig.update(f"UPDATE desktop_sessions SET {column}=?", (value,))
    with pytest.raises(providers.ProviderDenied, match="cleanup_scope_changed"):
        rig.provider.observer_authority(rig.provider.target)


@pytest.mark.parametrize(
    "table,column",
    [
        ("scientist_turn_intents", "request_sha256"),
        ("scientist_admission_history", "record_sha256"),
    ],
)
def test_retained_row_tamper_denies(rig, table, column):
    rig.update(f"UPDATE {table} SET {column}=?", ("0" * 64,))
    with pytest.raises(providers.ProviderDenied, match="original_mismatch"):
        rig.provider.original_reader(rig.provider.target)


@pytest.mark.parametrize("change", ["missing", "extra_unit", "wrong_uid", "duplicate"])
def test_actual_six_field_broker_peer_cannot_be_reinterpreted(rig, change):
    peer = {
        key: value
        for key, value in rig.observation.original["broker_generation"].items()
        if key != "unit"
    }
    if change == "missing":
        del peer["start_ticks"]
    elif change == "extra_unit":
        peer["unit"] = rig.observation.original["broker_generation"]["unit"]
    elif change == "wrong_uid":
        peer["uid"] += 1
    raw = canonical(peer).decode()
    if change == "duplicate":
        raw = raw[:-1] + ',"uid":' + str(peer["uid"]) + "}"
    rig.update("UPDATE scientist_turn_intents SET broker_peer_json=?", (raw,))
    with pytest.raises((providers.ProviderDenied, ValueError)):
        rig.provider.original_reader(rig.provider.target)


def test_missing_or_changed_review_cannot_be_empty_provenance(rig):
    Path(rig.config["dispatch_review"]["path"]).unlink()
    with pytest.raises(providers.ProviderDenied, match="file_unavailable"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))


def test_historical_source_bytes_must_match_retained_receipt(rig):
    rig.historical[next(iter(rig.historical))] = b"changed source"
    with pytest.raises(providers.ProviderDenied, match="historical_source_unavailable"):
        rig.provider.inspect()


def test_runtime_dispatch_source_must_be_in_retained_historical_inventory(rig):
    review_path = Path(rig.config["dispatch_review"]["path"])
    review = json.loads(review_path.read_bytes())
    review["historical_source_files"] = [
        item
        for item in review["historical_source_files"]
        if item["path"] != "lab/llm/native_runtime.py"
    ]
    rig.config["dispatch_review"] = pin(review_path, review)
    rig.provider = rig.reload()
    with pytest.raises(providers.ProviderDenied, match="historical_source_inventory"):
        rig.provider.inspect()


def test_daemon_failure_is_not_container_absence(rig):
    rig.docker_ok = False
    with pytest.raises(providers.ProviderDenied, match="original_container_not_proven_absent"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))


def test_takeover_during_physical_read_revokes_returned_proof(rig, monkeypatch):
    original = providers._command

    def command(arguments, **kwargs):
        result = original(arguments, **kwargs)
        if arguments[0] == "/usr/bin/docker":
            rig.update("UPDATE desktop_sessions SET lease_id='taken-over'")
        return result

    monkeypatch.setattr(providers, "_command", command)
    with pytest.raises(providers.ProviderDenied, match="cleanup_scope_changed"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))


def test_live_or_reused_original_pid_denies(rig, monkeypatch):
    monkeypatch.setattr(providers.gpu_scheduler, "_read_process_identity", lambda pid: object())
    with pytest.raises(providers.ProviderDenied, match="old_pid_present_or_reused"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))


def test_present_canonical_dispatch_provenance_denies(rig):
    with closing(sqlite3.connect(rig.provider.canonical_database)) as connection:
        connection.execute(
            "INSERT INTO aos_gpu_child_bindings(request_id) VALUES(?)",
            (rig.provider.target["request_id"],),
        )
        connection.commit()
    with pytest.raises(providers.ProviderDenied, match="canonical_provenance_present"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))


@pytest.mark.parametrize("owner", ["aos", "lab"])
@pytest.mark.parametrize("state", ["prepared", "created", "uncertain"])
@pytest.mark.parametrize("token", [1, 97])
def test_runtime_binding_denies_before_physical_proof_without_mutation(rig, owner, state, token):
    rig.observation.runtime_binding(owner=owner, state=state, token=token)
    before = rig.provider.canonical_database.read_bytes()
    with pytest.raises(providers.ProviderDenied, match="canonical_provenance_present"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))
    assert rig.provider.canonical_database.read_bytes() == before
    assert not rig.observation.rows()
    assert not any(call[0] == "/usr/bin/docker" for call in rig.commands)


@pytest.mark.parametrize("change", ["missing_runtime", "unknown_deferred"])
def test_runtime_inventory_must_be_complete_and_closed(rig, change):
    with closing(sqlite3.connect(rig.provider.canonical_database)) as connection:
        if change == "missing_runtime":
            connection.execute("DROP TABLE gpu_runtime_bindings")
        else:
            connection.execute("CREATE TABLE deferred_runtime(request_id TEXT)")
        connection.commit()
    with pytest.raises(providers.ProviderDenied, match="canonical_inventory_unknown"):
        rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))
    assert not rig.observation.rows()


def test_unrelated_runtime_binding_does_not_claim_target_dispatch(rig):
    rig.observation.runtime_binding(owner="lab", state="uncertain", request_id="b" * 32)
    before = rig.provider.canonical_database.read_bytes()
    proof = rig.provider.physical_reader(rig.provider.target, canonical(rig.observation.original))
    assert json.loads(proof.dispatch_provenance)["quarantined_allocations"] == []
    assert rig.provider.canonical_database.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "wrong_hash"])
def test_config_requires_private_exact_unlinked_file(rig, tmp_path, kind):
    original = rig.path
    checksum = hashlib.sha256(original.read_bytes()).hexdigest()
    if kind == "symlink":
        rig.path = tmp_path / "symlink.json"
        rig.path.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, tmp_path / "hardlink.json")
    elif kind == "public":
        original.chmod(0o644)
    else:
        checksum = "0" * 64
    with pytest.raises(providers.ProviderDenied):
        providers.ReadOnlyObservationProviders(rig.path, checksum)


def test_changed_config_revokes_already_constructed_provider(rig):
    rig.config["version"] = 2
    pin(rig.path, rig.config)
    with pytest.raises(providers.ProviderDenied, match="config_changed"):
        rig.provider.inspect()


def test_normal_shell_cannot_authenticate_as_observer(monkeypatch):
    monkeypatch.setattr(
        providers,
        "_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            0,
            b"LoadState=not-found\nActiveState=inactive\nMainPID=0\nInvocationID=\nControlGroup=\n",
            b"",
        ),
    )
    with pytest.raises(providers.ProviderDenied, match="observer_not_current_process"):
        providers._observer_generation()


def test_db_connection_cannot_write(rig):
    with providers._database(rig.config["aos_database"]) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM scientist_turn_intents")


def test_unknown_closure_schema_denies(rig):
    rig.update("UPDATE schema_migrations SET version=29")
    with pytest.raises(providers.ProviderDenied, match="aos_schema_changed"):
        rig.provider.read_closure("f" * 64)


@pytest.mark.parametrize("tamper", [None, "snapshot", "observation_hash", "schema"])
def test_closure_requires_exact_reviewed_schema_original_and_canonical_tombstone(
    rig, monkeypatch, tmp_path, tamper
):
    from lab.llm.aos_no_admission_store import NoAdmissionObservationStore

    ddl = (
        "CREATE TABLE scientist_no_admission_closures (request_id TEXT,session_id TEXT,"
        "request_sha256 TEXT,admission_record_sha256 TEXT,intent_snapshot_json TEXT,"
        "observation_json TEXT,observation_sha256 TEXT,schema_sha256 TEXT,"
        "recovery_scope_json TEXT,created_at TEXT);\n"
    )
    # Fixture schema is independently pinned and compared byte-for-byte; real
    # composition pins the complete reviewed AOS migration instead.
    for name in ("one", "two", "three", "four", "five", "six"):
        ddl += (
            f"CREATE TRIGGER scientist_{name} BEFORE UPDATE ON scientist_no_admission_closures "
            "BEGIN SELECT RAISE(ABORT,'immutable'); END;\n"
        )
    migration = tmp_path / "database/migrations/0028_scientist_no_admission_closures.sql"
    migration.parent.mkdir(parents=True)
    migration.write_text(ddl)
    migration.chmod(0o600)
    rig.config["sources"].append(
        {"path": str(migration), "sha256": hashlib.sha256(migration.read_bytes()).hexdigest()}
    )
    rig.config["observer"]["source_sha256"] = digest(rig.config["sources"])
    review_path = Path(rig.config["dispatch_review"]["path"])
    review = json.loads(review_path.read_bytes())
    review["reviewed_source_manifest_sha256"] = digest(rig.config["sources"])
    rig.config["dispatch_review"] = pin(review_path, review)
    rig.provider = rig.reload()
    store = NoAdmissionObservationStore(
        rig.observation.control,
        observer_authority=rig.provider.observer_authority,
        original_reader=rig.provider.original_reader,
        physical_reader=rig.provider.physical_reader,
    )
    import time

    observation = store.observe(rig.provider.target, deadline_monotonic=time.monotonic() + 4)
    monkeypatch.setattr(providers.gpu_scheduler, "boottime", lambda: rig.observation.base.now)
    with closing(sqlite3.connect(rig.aos)) as connection:
        connection.row_factory = sqlite3.Row
        intent = dict(connection.execute("SELECT * FROM scientist_turn_intents").fetchone())
        connection.executescript(ddl)
        connection.execute("UPDATE schema_migrations SET version=28")
        if tamper == "snapshot":
            intent["request_json"] = "{}"
        connection.execute(
            "INSERT INTO scientist_no_admission_closures VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                rig.provider.target["request_id"],
                rig.observation.cleanup["session_id"],
                rig.provider.target["request_sha256"],
                rig.observation.original["admission_record_sha256"],
                canonical(intent).decode(),
                canonical(observation).decode(),
                "f" * 64 if tamper == "observation_hash" else observation["observation_sha256"],
                schema_hash(),
                canonical(rig.observation.cleanup).decode(),
                "fixture-now",
            ),
        )
        if tamper == "schema":
            connection.execute("DROP TRIGGER scientist_one")
        connection.commit()
    if tamper:
        with pytest.raises(providers.ProviderDenied):
            rig.provider.read_closure(observation["observation_sha256"])
    else:
        assert rig.provider.read_closure(observation["observation_sha256"]) is True
