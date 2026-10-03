"""CPU fixtures for factual worker inventory; no real model, systemd or GPU."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_aos_original_physical_snapshot as aos_source

from lab.llm import native_runtime
from lab.llm import worker_inventory as inventory
from lab.llm.gpu_scheduler import PrincipalReceipt, _current_process_identity

publication = aos_source.publication
aos_released = aos_source.released


class _Principal:
    def __init__(self) -> None:
        self.receipt = PrincipalReceipt(
            "lab", _current_process_identity(), "swapp-lab-gpu-fixture.service", "e" * 32
        )

    def resolve(self, owner):
        assert owner == "lab"
        return self.receipt

    def verify(self, receipt):
        return receipt == self.receipt


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    absent = native_runtime.UnitSnapshot("not-found", "inactive", "", "", 0, "", "", 0, 0, 0, 0, 0)
    processes = {}
    state = {"unit": absent, "gpu": native_runtime.GpuSnapshot({}, 0, 16384), "inspected": []}

    def inspect(name):
        state["inspected"].append(name)
        return state["unit"]

    units = SimpleNamespace(
        inspect=inspect,
        cgroup_pids=lambda _group: frozenset(processes),
        cgroup_empty=lambda _group: not processes,
    )
    gpu = SimpleNamespace(snapshot=lambda: state["gpu"])
    instance = native_runtime.OwnedVllmRuntime(
        tmp_path / "arbiter.sqlite3",
        principal_resolver=_Principal(),
        unit_manager=units,
        pin=native_runtime.ModelPin(tmp_path, native_runtime.MODEL_REVISION, "c" * 64, ()),
        gpu_observer=gpu,
    )
    instance.scheduler.submit("lab", "fixture-turn", b"inert fixture")
    lease = instance.scheduler.try_acquire("lab", "fixture-turn")
    assert lease is not None
    row = instance._prepare_unit(lease)
    group = "/user.slice/swapp-gpu.slice/" + row["unit"]
    with instance._connect() as db:
        db.execute(
            "UPDATE gpu_runtime_bindings SET launch_state='created',invocation_id=?,main_pid=?,"
            "main_start_ticks=?,boot_id=?,control_group=?",
            ("a" * 32, 7001, 1234, inventory._boot_id(), group),
        )
    instance.scheduler.release(lease)
    state["inspected"].clear()
    return SimpleNamespace(
        database=instance._database,
        instance=instance,
        lease=lease,
        row=row,
        group=group,
        state=state,
        units=units,
        gpu=gpu,
        processes=processes,
    )


def observe(case, **options):
    arguments = dict(
        units=case.units,
        gpu=case.gpu,
        enumerate_units=lambda: (),
        enumerate_processes=lambda: tuple(case.processes),
        process_reader=lambda pid: case.processes.get(pid),
        principal_reader=lambda _receipt: True,
    )
    arguments.update(options)
    return inventory.observe_worker_inventory(case.database, **arguments)


def make_current(case):
    with case.instance._connect() as db:
        db.execute("UPDATE gpu_turn_requests SET state='active'")
        db.execute(
            "UPDATE gpu_turn_state SET active_owner='lab',active_request_id=?,phase='activating',"
            "owner_pid=?,owner_start_ticks=?,owner_boot_id=?,owner_unit=?,owner_invocation_id=?,"
            "activation_deadline=?,inference_deadline=?,heartbeat_deadline=?,total_deadline=?",
            (
                case.lease.request_id,
                case.lease.owner_identity.pid,
                case.lease.owner_identity.start_ticks,
                case.lease.owner_identity.boot_id,
                case.lease.owner_unit,
                case.lease.owner_invocation_id,
                case.lease.activation_deadline,
                case.lease.inference_deadline,
                case.lease.heartbeat_deadline,
                case.lease.total_deadline,
            ),
        )
    case.processes[7001] = inventory.ProcessObservation(
        inventory.ProcessIdentity(7001, 1234, inventory._boot_id()),
        os.getuid(),
        case.group,
        (("pid", 11), ("net", 12), ("user", 13), ("mnt", 14)),
    )
    case.state["unit"] = native_runtime.UnitSnapshot(
        "loaded",
        "active",
        "a" * 32,
        case.group,
        7001,
        case.row["expected_description"],
        "SWAPP_GPU_TURN_NONCE=" + case.row["nonce"],
        native_runtime.MODEL_UNIT_MEMORY_BYTES,
        0,
        native_runtime.MODEL_UNIT_CPU_PERCENT * 10_000,
        native_runtime.MODEL_UNIT_TASKS,
        native_runtime.MODEL_RUNTIME_MAX_SECONDS * 1_000_000,
    )
    case.state["gpu"] = native_runtime.GpuSnapshot({7001: 1024}, 1024, 16384)


def test_historical_created_rows_are_physically_inspected_without_any_database_write(
    case, monkeypatch
):
    before = case.database.read_bytes()
    mode = case.database.stat().st_mode
    files = sorted(path.name for path in case.database.parent.iterdir())
    monkeypatch.setattr(
        native_runtime.OwnedVllmRuntime,
        "__init__",
        lambda *_args, **_kwargs: pytest.fail("constructor"),
    )
    result = observe(case)

    assert result.authority is False and result.source_changed is False
    assert result.before_source_sha256 == result.after_source_sha256
    assert result.blockers == ()
    (worker,) = result.workers
    assert worker.status == "historical_not_observed" and worker.physical_presence == "not_observed"
    assert not worker.allocation_active and case.state["inspected"] == [case.row["unit"]]
    assert worker.profile_id is None and worker.broker_generation_sha256 is None
    assert (
        worker.namespace_provenance is None
        and "original_namespace_provenance_unknown" in worker.blockers
    )
    assert case.database.read_bytes() == before and case.database.stat().st_mode == mode
    assert sorted(path.name for path in case.database.parent.iterdir()) == files


def test_actual_current_lab_identity_does_not_fabricate_broker_or_profile_provenance(case):
    make_current(case)
    result = observe(case)
    (worker,) = result.workers
    assert worker.status == "observed" and worker.physical_presence == "observed"
    assert worker.allocation_active and worker.principal.identity == case.lease.owner_identity
    assert worker.gpu_pids == (7001,) and worker.processes[0].namespaces[1] == ("net", 12)
    assert worker.broker_generation_sha256 is None and worker.provider_config_sha256 is None
    assert "lab_profile_provider_deployment_provenance_unknown" in result.blockers
    assert result.authority is False


@pytest.mark.parametrize("launch_state", ["prepared", "uncertain"])
def test_missing_pending_worker_is_not_historical_drain_evidence(case, launch_state):
    with case.instance._connect() as db:
        db.execute("UPDATE gpu_runtime_bindings SET launch_state=?", (launch_state,))
    result = observe(case)
    assert result.workers[0].status == "pending"
    assert "worker_launch_pending_or_uncertain" in result.blockers


def test_quarantined_worker_is_reported_even_with_matching_physical_identity(case):
    make_current(case)
    with case.instance._connect() as db:
        db.execute("UPDATE gpu_turn_state SET phase='quarantined'")
    result = observe(case)
    assert result.workers[0].status == "quarantined"
    assert "allocation_quarantined" in result.blockers


@pytest.mark.parametrize("fault", ["start_ticks", "invocation", "namespace_change", "principal"])
def test_current_physical_or_principal_mismatch_remains_a_blocker(case, fault):
    make_current(case)
    options = {}
    if fault == "start_ticks":
        process = case.processes[7001]
        case.processes[7001] = replace(
            process, identity=replace(process.identity, start_ticks=4321)
        )
    elif fault == "invocation":
        case.state["unit"] = replace(case.state["unit"], invocation_id="b" * 32)
    elif fault == "namespace_change":
        original = case.processes[7001]
        calls = iter((original, replace(original, namespaces=(("net", 99),))))
        options["process_reader"] = lambda _pid: next(calls)
    else:
        options["principal_reader"] = lambda _receipt: False
    result = observe(case, **options)
    expected = (
        "allocation_principal_not_observed"
        if fault == "principal"
        else (
            "worker_changed_during_observation"
            if fault == "namespace_change"
            else "worker_physical_generation_mismatch"
        )
    )
    assert expected in result.blockers and result.authority is False


def test_orphan_units_live_historical_workers_and_uncovered_gpu_pids_are_reported(case):
    make_current(case)
    with case.instance._connect() as db:
        db.execute("UPDATE gpu_turn_requests SET state='done'")
        db.execute("UPDATE gpu_turn_state SET active_owner=NULL,active_request_id=NULL,phase=NULL")
    orphan = "swapp-aos-gpu-turn-" + "b" * 32 + ".service"
    case.state["gpu"] = native_runtime.GpuSnapshot({7001: 1024, 8001: 256}, 1280, 16384)
    result = observe(case, enumerate_units=lambda: (orphan,))
    assert all(worker.status == "orphan" for worker in result.workers)
    assert "live_worker_without_matching_allocation" in result.blockers
    assert "unit_without_durable_worker_binding" in result.blockers
    assert "gpu_process_without_observed_worker_binding" in result.blockers


def test_os_reader_runs_outside_transaction_and_source_change_is_detected(case):
    def mutate_source():
        # A separate writer succeeds only if the observer closed its SQL snapshot.
        with sqlite3.connect(case.database, timeout=0) as db:
            db.execute("UPDATE gpu_turn_state SET active_token=active_token+1")
        return ()

    result = observe(case, enumerate_units=mutate_source)
    assert result.source_changed is True
    assert result.before_source_sha256 != result.after_source_sha256
    assert "source_changed_during_observation" in result.blockers


def test_missing_database_is_not_created_and_is_not_an_empty_observation(case):
    missing = case.database.parent / "missing.sqlite3"
    result = inventory.observe_worker_inventory(missing, units=case.units, gpu=case.gpu)
    assert not missing.exists() and result.blockers == ("inventory_observation_unavailable",)
    assert result.before_source_sha256 is None and result.after_source_sha256 is None


def test_truncation_and_unreadable_physical_state_are_explicit(case):
    result = observe(case, enumerate_processes=lambda: tuple(range(8193)))
    assert result.blockers == ("physical_enumeration_limit_exceeded",)
    result = observe(
        case,
        enumerate_processes=lambda: (7001,),
        process_reader=lambda _pid: (_ for _ in ()).throw(PermissionError()),
    )
    assert "process_observation_unavailable" in result.blockers


def test_active_ticket_with_no_binding_or_allocation_is_not_dropped(case):
    with case.instance._connect() as db:
        db.execute("DELETE FROM gpu_runtime_bindings")
        db.execute("UPDATE gpu_turn_requests SET state='active'")
    result = observe(case)
    assert result.workers == ()
    assert "active_ticket_without_matching_worker_allocation" in result.blockers


def test_symlink_parent_is_declined_without_following_or_writing_source(case):
    alias = case.database.parent / "alias"
    alias.symlink_to(case.database.parent, target_is_directory=True)
    before = case.database.read_bytes()
    result = inventory.observe_worker_inventory(
        alias / case.database.name, units=case.units, gpu=case.gpu
    )
    assert result.blockers == ("database_path_not_canonical",)
    assert case.database.read_bytes() == before


@pytest.mark.parametrize("corrupt_child_hash", [False, True])
def test_aos_original_frame_and_child_payload_have_distinct_exact_hash_bindings(
    aos_released, corrupt_child_hash
):
    p = aos_released
    absent = native_runtime.UnitSnapshot("not-found", "inactive", "", "", 0, "", "", 0, 0, 0, 0, 0)
    units = SimpleNamespace(inspect=lambda _name: absent, cgroup_pids=lambda _group: frozenset())
    gpu = SimpleNamespace(snapshot=lambda: native_runtime.GpuSnapshot({}, 0, 16384))
    native_runtime.OwnedVllmRuntime(
        p.store.database,
        principal_resolver=_Principal(),
        unit_manager=units,
        gpu_observer=gpu,
        pin=native_runtime.ModelPin(
            p.store.database.parent, native_runtime.MODEL_REVISION, "c" * 64, ()
        ),
    )
    with p.store._connect() as db:
        child = db.execute("SELECT * FROM aos_gpu_child_bindings").fetchone()
        control = db.execute("SELECT * FROM aos_control_requests").fetchone()
        assert child["request_sha256"] != control["request_sha256"]
        server = json.loads(control["admission_binding_json"])["server_generation"]
        if corrupt_child_hash:
            db.execute("UPDATE aos_gpu_child_bindings SET request_sha256=?", ("f" * 64,))
    before = p.store.database.read_bytes()
    result = inventory.observe_worker_inventory(
        p.store.database,
        units=units,
        gpu=gpu,
        enumerate_units=lambda: (),
        enumerate_processes=lambda: (),
        process_reader=lambda _pid: None,
        principal_reader=lambda _receipt: pytest.fail(
            "historical source does not require a live broker"
        ),
    )
    assert result.authority is False and p.store.database.read_bytes() == before
    (worker,) = result.workers
    if corrupt_child_hash:
        assert worker.broker_generation_sha256 is None
        assert "aos_original_control_unavailable_or_mismatched" in result.blockers
    else:
        assert worker.status == "historical_not_observed" and result.blockers == ()
        assert worker.broker_generation_sha256 == inventory.digest(server)
        assert worker.provider_config_sha256 is not None
