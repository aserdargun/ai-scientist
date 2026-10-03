#!/usr/bin/env python3
"""Opt-in CPU proof of a retained-provider socket inherited as service stdin.

Default invocation only validates source pins and prints the proposed experiment.
Execution creates two bounded transient user services and private fixture databases.
It grants no production control admission and exercises no GPU or model.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.check_aos_resolution_composition import (  # noqa: E402
    canonical,
    digest,
    file_hash,
    make_fixture,
    preflight,
    require,
)

BROKER_UNIT = "swapp-lab-gpu-broker.service"
UNIT_PATTERN = re.compile(r"swapp-aos-provider-bootstrap-[0-9a-f]{32}\.service\Z")
LOG_LIMIT = 1024 * 1024
FILE_LIMIT = 64 * 1024 * 1024
SOURCE_FILES = (
    "scripts/check_aos_provider_service_bootstrap.py",
    "scripts/check_aos_resolution_composition.py",
    "scripts/aos_resolution_composition_fixture.py",
    "scripts/aos_retained_provider_adapter.py",
    "lab/llm/aos_retained_provider.py",
    "lab/llm/aos_gpu_service.py",
    "lab/llm/aos_gpu_executor.py",
    "lab/llm/aos_gpu_control.py",
    "lab/llm/aos_gpu_control_store.py",
    "lab/llm/aos_physical_readback.py",
    "lab/llm/native_runtime.py",
    "lab/llm/gpu_scheduler.py",
)
PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "MainPID",
    "InvocationID",
    "ControlGroup",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "Transient",
    "Restart",
    "RuntimeMaxUSec",
    "CPUQuotaPerSecUSec",
    "MemoryMax",
    "MemorySwapMax",
    "TasksMax",
    "LimitFSIZE",
    "ReadOnlyPaths",
    "TimeoutStartUSec",
    "TimeoutStopUSec",
    "KillMode",
    "SendSIGKILL",
    "UnsetEnvironment",
)
INJECTION_VARIABLES = (
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_PROFILE",
    "PYTHONHOME",
    "PYTHONPATH",
)


def user_environment():
    runtime = Path("/run/user") / str(os.getuid())
    require(
        runtime.is_dir() and not runtime.is_symlink() and runtime.stat().st_uid == os.getuid(),
        "owned user runtime directory required",
    )
    require((runtime / "bus").is_socket(), "fixed user bus socket required")
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(runtime / "bus"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "CUDA_VISIBLE_DEVICES": "",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }


def resource_preflight(workdir):
    available = None
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
            break
    parent = workdir
    while not parent.exists():
        parent = parent.parent
    free = shutil.disk_usage(parent).free
    require(
        available is not None and available >= 6 * 1024**3,
        "at least 6 GiB available host RAM required for the two CPU services",
    )
    require(free >= 20 * 1024**3, "at least 20 GiB host disk reserve required")
    return dict(
        mem_available_bytes=available,
        disk_free_bytes=free,
        aggregate_memory_limit_bytes=2 * 1024**3,
        aggregate_cpu_quota_percent=200,
    )


def unit_snapshot(unit, environment):
    require(unit == BROKER_UNIT or UNIT_PATTERN.fullmatch(unit), "unowned service name")
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            "--no-pager",
            *["--property=" + name for name in PROPERTIES],
            unit,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    require(len(result.stdout) <= 64 * 1024, "oversized unit snapshot")
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    # An unknown unit is reported with exit code 1 on some systemd versions.
    require(
        result.returncode == 0 or values.get("LoadState") == "not-found",
        "systemd unit lookup failed",
    )
    require(values.get("LoadState") in {"loaded", "not-found"}, "ambiguous unit identity")
    return values


def require_absent(snapshot):
    require(
        snapshot.get("LoadState") == "not-found"
        and snapshot.get("ActiveState") == "inactive"
        and snapshot.get("MainPID", "0") == "0",
        "existing service must not be replaced, started, or stopped",
    )


def require_service_limits(snapshot, aos_root):
    """Verify actual manager properties before trusting an owned active service."""
    expected = {
        "Transient": {"yes"},
        "Restart": {"no"},
        "RuntimeMaxUSec": {"3min", "180s"},
        "CPUQuotaPerSecUSec": {"1s"},
        "MemoryMax": {str(1024**3)},
        "MemorySwapMax": {"0"},
        "TasksMax": {"128"},
        "LimitFSIZE": {str(FILE_LIMIT)},
        "TimeoutStartUSec": {"10s"},
        "TimeoutStopUSec": {"5s"},
        "KillMode": {"control-group"},
        "SendSIGKILL": {"yes"},
    }
    for name, allowed in expected.items():
        require(snapshot.get(name) in allowed, "actual transient service bound differs: " + name)
    require(
        str(aos_root) in snapshot.get("ReadOnlyPaths", "").split(),
        "actual AOS source read-only mount missing",
    )
    require(
        set(INJECTION_VARIABLES).issubset(snapshot.get("UnsetEnvironment", "").split()),
        "loader and Python injection variables were not cleared",
    )


def ownership_matches(unit, snapshot, generation, identity, boot_id):
    """Fail closed before stopping a unique service; process birth is mandatory."""
    return bool(
        UNIT_PATTERN.fullmatch(unit)
        and generation.get("unit") == unit
        and generation.get("uid") == os.getuid()
        and snapshot.get("Id") == unit
        and snapshot.get("LoadState") == "loaded"
        and snapshot.get("ActiveState") == "active"
        and snapshot.get("MainPID") == str(generation.get("pid"))
        and snapshot.get("InvocationID") == generation.get("invocation_id")
        and snapshot.get("ControlGroup") == generation.get("control_group")
        and generation.get("boot_id") == boot_id
        and identity is not None
        and identity.pid == generation.get("pid")
        and identity.start_ticks == generation.get("start_ticks")
        and identity.boot_id == generation.get("boot_id")
    )


def stop_owned_aos(unit, generation, environment):
    from lab.llm.gpu_scheduler import _boot_id, _process_cgroup, _read_process_identity

    observed = unit_snapshot(unit, environment)
    identity = _read_process_identity(generation.get("pid", 0))
    if not ownership_matches(unit, observed, generation, identity, _boot_id()):
        return False
    # Recheck the invocation and PID immediately before the exact-unit operation.
    if observed != unit_snapshot(unit, environment):
        return False
    if (
        _read_process_identity(generation["pid"]) != identity
        or _process_cgroup(generation["pid"]) != generation["control_group"]
    ):
        return False
    result = subprocess.run(
        ["/usr/bin/systemctl", "--user", "stop", unit],
        env=environment,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def service_command(unit, python, program, arguments, args, environment):
    require(unit == BROKER_UNIT or UNIT_PATTERN.fullmatch(unit), "invalid transient unit")
    properties = [
        "Restart=no",
        "RuntimeMaxSec=180",
        "CPUQuota=100%",
        "MemoryMax=1G",
        "MemorySwapMax=0",
        "TasksMax=128",
        "LimitFSIZE=64M",
        "UMask=0077",
        "TimeoutStartSec=10s",
        "TimeoutStopSec=5s",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "UnsetEnvironment=" + " ".join(INJECTION_VARIABLES),
        "WorkingDirectory=" + str(args.workdir),
        "ReadOnlyPaths=" + str(args.aos_root),
        "NoNewPrivileges=yes",
    ]
    child_environment = dict(
        environment, PYTHONPATH=str(args.aos_root / "src" if unit != BROKER_UNIT else ROOT)
    )
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--pipe",
        "--quiet",
        "--no-ask-password",
        "--service-type=exec",
        "--collect",
        "--unit=" + unit,
        *["--property=" + value for value in properties],
        "/usr/bin/env",
        "-i",
        *[key + "=" + value for key, value in child_environment.items()],
        str(python),
        "-B",
        str(program),
        *arguments,
    ]


def launch(command, stdin, workdir, environment, prefix, errors):
    """Drain launcher pipes while retaining at most one MiB per private log."""
    child = subprocess.Popen(
        command,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=workdir,
        env=environment,
        close_fds=True,
    )
    threads = []

    def retain(pipe, path):
        size = 0
        overflow = False
        try:
            with pipe, path.open("xb") as output:
                while chunk := pipe.read(8192):
                    output.write(chunk[: max(0, LOG_LIMIT - size)])
                    size += len(chunk)
                    if size > LOG_LIMIT and not overflow:
                        errors.append("bounded launcher log exceeded: " + path.name)
                        overflow = True
        except Exception as error:
            errors.append("log capture: " + repr(error))

    for pipe, suffix in ((child.stdout, "stdout"), (child.stderr, "stderr")):
        thread = threading.Thread(
            target=retain, args=(pipe, workdir / (prefix + "." + suffix)), daemon=True
        )
        thread.start()
        threads.append(thread)
    return child, threads


def source_arguments(args):
    result = [
        "--aos-root",
        str(args.aos_root),
        "--workdir",
        str(args.workdir),
        "--preflight-report",
        str(args.preflight_report),
    ]
    for name in (
        "preflight_sha256",
        "head",
        "selected_source_sha256",
        "tracked_diff_sha256",
        "selected_untracked_sha256",
        "retained_host_sha256",
    ):
        result += ["--expected-" + name.replace("_", "-"), getattr(args, "expected_" + name)]
    if getattr(args, "native_bootstrap", False):
        result.append("--native-bootstrap")
    return result


def verify_bootstrap_manifest(args):
    path = args.workdir / "bootstrap-source.json"
    require(
        not path.is_symlink() and path.stat().st_size < 64 * 1024,
        "bounded bootstrap source manifest required",
    )
    manifest = json.loads(path.read_bytes())
    require(
        manifest.get("aos_unit") == args.aos_unit and UNIT_PATTERN.fullmatch(args.aos_unit),
        "bootstrap unit ownership differs",
    )
    require(
        manifest.get("source_files") == {name: file_hash(ROOT / name) for name in SOURCE_FILES},
        "Scientist bootstrap source changed",
    )
    return manifest


def unchanged_store(control):
    """Snapshot every private Store row after the two quota-consuming controls."""
    with closing(control.store._connect()) as connection:
        result = {}
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            name = row[0]
            if name.startswith("sqlite_"):
                continue
            require(re.fullmatch(r"[a-zA-Z0-9_]+", name), "unexpected private SQL table")
            result[name] = [
                list(item)
                for item in connection.execute('SELECT * FROM "' + name + '" ORDER BY rowid')
            ]
        return canonical(result)


def verify_bootstrap_audit(workdir, request, broker, original_request):
    """Independently read durable AOS audit before replying to bootstrap."""
    database = workdir / "aos.sqlite3"
    info = database.lstat()
    require(
        stat.S_ISREG(info.st_mode)
        and not database.is_symlink()
        and info.st_uid == os.getuid()
        and info.st_mode & 0o077 == 0,
        "bootstrap audit database must be private and owned",
    )
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as reader:
        reader.row_factory = sqlite3.Row
        row = reader.execute(
            "SELECT * FROM desktop_events WHERE event_id=?", (request["control_id"],)
        ).fetchone()
        require(
            row is not None and row["kind"] == "scientist_bootstrap_intent",
            "missing durable bootstrap audit",
        )
        payload = json.loads(row["payload_json"])
        # EvidenceClient persists codec bytes; the newline is added only on send.
        frame = canonical(request)
        binding = payload["intent_binding"]
        session = reader.execute(
            "SELECT * FROM desktop_sessions WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        require(
            payload["authority"] == "audit-only"
            and payload["control_frame"].encode() == frame
            and payload["control_sha256"] == digest_bytes(frame)
            and payload["broker_peer"] == {k: v for k, v in broker.items() if k != "unit"}
            and payload["request_id"] == original_request["request_id"]
            and payload["request_sha256"] == digest(original_request)
            and session is not None
            and session["status"] == "running"
            and binding["owner"] == "AGENT"
            and all(
                session[key] == binding[key]
                for key in ("session_id", "runtime_id", "owner", "lease_id", "generation")
            )
            and reader.execute("SELECT count(*) FROM scientist_turn_intents").fetchone()[0] == 0,
            "bootstrap audit differs or original intent already began",
        )
        return dict(
            control_id=request["control_id"],
            audit_payload_sha256=digest(payload),
            original_intent_absent=True,
        )


def digest_bytes(value):
    import hashlib

    return hashlib.sha256(value).hexdigest()


def broker_stage(args):
    from lab.llm.aos_gpu_executor import SystemdSocketPeerAuthenticator
    from lab.llm.aos_gpu_service import _broker_generation, _broker_still_current
    from lab.llm.aos_retained_provider import RetainedProviderServer, prepare_channel

    environment = user_environment()
    verify_bootstrap_manifest(args)
    report = preflight(args)
    resources = resource_preflight(args.workdir)
    server = _broker_generation()
    require(
        server["unit"] == BROKER_UNIT and server["pid"] == os.getpid(),
        "CPU broker must be canonical service MainPID",
    )
    broker_active = unit_snapshot(BROKER_UNIT, environment)
    require_service_limits(broker_active, args.aos_root)
    require_absent(unit_snapshot(args.aos_unit, environment))
    errors = []
    stopping = threading.Event()
    threads = []
    log_threads = []
    peer = None
    child = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(args.workdir / "control.sock"))
        listener.listen(2)
        listener.settimeout(0.2)
        broker_channel, aos_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        prepare_channel(broker_channel)
        prepare_channel(aos_channel)
        try:
            command = service_command(
                args.aos_unit,
                args.aos_root / ".venv/bin/python",
                ROOT / "scripts/aos_resolution_composition_fixture.py",
                [str(args.workdir), str(args.aos_root), "0"],
                args,
                environment,
            )
            child, log_threads = launch(
                command, aos_channel, args.workdir, environment, "consumer", errors
            )
            aos_channel.close()
            deadline = time.monotonic() + 10
            authenticator = SystemdSocketPeerAuthenticator(args.aos_unit)
            while peer is None:
                snapshot = unit_snapshot(args.aos_unit, environment)
                pid = int(snapshot.get("MainPID", "0"))
                if snapshot.get("ActiveState") == "active" and pid > 1:
                    peer = authenticator.authenticate(pid, os.getuid())
                else:
                    require(
                        child.poll() is None and time.monotonic() < deadline,
                        "AOS service did not reach a bounded active generation",
                    )
                    time.sleep(0.05)
            require(peer.pid != child.pid, "launcher PID must differ from AOS service MainPID")
            require_service_limits(snapshot, args.aos_root)
            ownership = dict(
                peer_generation=asdict(peer),
                server_generation=server,
                aos_active=snapshot,
                broker_active=broker_active,
                launcher_pid=child.pid,
            )
            (args.workdir / "owned-generation.json").write_bytes(canonical(ownership))
            lifecycle = SimpleNamespace(pid=peer.pid, poll=child.poll)
            control, fixture = make_fixture(
                args.workdir,
                lifecycle,
                report,
                retained_provider=True,
                peer_generation=peer,
                server_generation=server,
                authenticator=authenticator,
                server_current=lambda: _broker_still_current(server),
            )
            native_bootstrap = getattr(args, "native_bootstrap", False)
            fixture["native_bootstrap"] = native_bootstrap
            fixture["aos_source_sha256"] = report["source_sha256"]
            expected_controls = 3 if native_bootstrap else 2
            bootstrap_audits = []
            if native_bootstrap:
                original_handle = control.handle

                def audited_handle(observed_peer, request):
                    if request["op"] == "capability" and request["target"] is None:
                        require(
                            observed_peer == peer and not bootstrap_audits,
                            "unexpected bootstrap replay",
                        )
                        bootstrap_audits.append(
                            verify_bootstrap_audit(
                                args.workdir, request, server, fixture["request"]
                            )
                        )
                    return original_handle(observed_peer, request)

                control.handle = audited_handle
            provider_baseline = []
            control_complete = threading.Event()

            def forbidden(*_args):
                raise RuntimeError("CPU bootstrap forbids GPU or physical observation")

            provider = RetainedProviderServer(
                control,
                units=SimpleNamespace(inspect=forbidden, cgroup_empty=forbidden),
                gpu=SimpleNamespace(snapshot=forbidden),
            )
            provider.verify_configuration()
            exchanges = [0]
            control_calls = [0]

            def serve_provider():
                try:
                    require(
                        control_complete.wait(timeout=15),
                        "control responses must finish before provider readback",
                    )
                    provider_baseline.append(unchanged_store(control))
                    while not stopping.is_set():
                        ready, _, _ = select.select([broker_channel], [], [], 0.2)
                        if ready:
                            if not broker_channel.recv(1, socket.MSG_PEEK):
                                break
                            provider.serve_once(broker_channel, exchanges[0] + 1)
                            exchanges[0] += 1
                except Exception as error:
                    errors.append("provider: " + repr(error))

            def serve_control():
                try:
                    while not stopping.is_set() and control_calls[0] < expected_controls:
                        try:
                            connection, _address = listener.accept()
                        except TimeoutError:
                            continue
                        with connection:
                            control.serve_connection(connection)
                        control_calls[0] += 1
                    if control_calls[0] == expected_controls:
                        control_complete.set()
                except Exception as error:
                    errors.append("control: " + repr(error))

            for function in (serve_provider, serve_control):
                thread = threading.Thread(target=function, daemon=True)
                thread.start()
                threads.append(thread)
            temporary = args.workdir / "fixture.tmp"
            temporary.write_bytes(canonical(fixture))
            temporary.replace(args.workdir / "fixture.json")
            child.wait(timeout=100)
            require(child.returncode == 0, "AOS systemd-run exit code was not zero")
            stopping.set()
            for thread in threads:
                thread.join(timeout=3)
                require(not thread.is_alive(), "owned channel handler did not finish")
            for thread in log_threads:
                thread.join(timeout=3)
            require(not errors, "CPU service channel failed: " + repr(errors))
            require(
                control_calls[0] == expected_controls,
                "all expected control socket responses required",
            )
            require(
                exchanges[0] >= 2 and exchanges[0] == provider._next_sequence - 1,
                "authenticated provider exchanges did not finish consistently",
            )
            require(
                len(provider_baseline) == 1 and unchanged_store(control) == provider_baseline[0],
                "retained provider changed private Store rows",
            )
            require(_broker_still_current(server), "broker service generation changed")
            preflight(args)
            verify_bootstrap_manifest(args)
            result = json.loads((args.workdir / "consumer-result.json").read_bytes())
            require(result.get("cpu_composition_passed") is True, "AOS resolution flow failed")
            if native_bootstrap:
                require(
                    len(bootstrap_audits) == 1
                    and result.get("native_bootstrap_factory_used") is True
                    and result.get("native_aos_provider_used") is True
                    and result.get("native_bootstrap_audit_committed") is True,
                    "native bootstrap factory/provider proof incomplete",
                )
            metadata = result.get("provider_socket_metadata", {})
            require(
                result.get("real_service_identity") is True
                and metadata.get("fd0") is True
                and metadata.get("family") == socket.AF_UNIX
                and metadata.get("socket_type") == socket.SOCK_SEQPACKET
                and metadata.get("passcred") == 1
                and metadata.get("exchange_count") == exchanges[0]
                and result.get("provider_checks_in_resolution_transaction", 0) > 0,
                "AOS FD0 full duplex provider identity proof is incomplete",
            )
            result.update(
                schema="aos_provider_service_bootstrap.v1",
                **ownership,
                provider_exchanges=exchanges[0],
                control_responses=control_calls[0],
                bootstrap_audits_before_response=bootstrap_audits,
                consumer_launcher_exit_code=child.returncode,
                aos_exit_snapshot=unit_snapshot(args.aos_unit, environment),
                original_private_store_unchanged=True,
                socket_bootstrap="systemd-run --user --pipe stdin FD0 SOCK_SEQPACKET",
                selected_bootstrap_source_manifest_is_not_runtime_attestation=True,
                native_acceptance=False,
                physical_gpu_proof=False,
                model_execution=False,
                production_control_admission=False,
                deployment_acceptance=False,
                broker_resource_preflight=resources,
                preflight_sha256=args.expected_preflight_sha256,
                retained_host_additional_source_sha256=args.expected_retained_host_sha256,
            )
            (args.workdir / "broker-result.json").write_bytes(canonical(result))
        finally:
            stopping.set()
            if child is not None and child.poll() is None and peer is not None:
                stop_owned_aos(args.aos_unit, asdict(peer), environment)
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for thread in threads + log_threads:
                thread.join(timeout=4)
            broker_channel.close()
            aos_channel.close()


def outer_stage(args):
    report = preflight(args)
    planned = dict(
        schema="aos_provider_service_bootstrap.plan.v1",
        execute=args.execute,
        aos_root=str(args.aos_root),
        workdir=str(args.workdir),
        broker_unit=BROKER_UNIT,
        source_ready=report["source_ready"],
        admission_allowed=False,
        physical_gpu_proof=False,
        model_execution=False,
    )
    if not args.execute:
        print(json.dumps(planned, sort_keys=True))
        return 0
    environment = user_environment()
    resources = resource_preflight(args.workdir)
    require_absent(unit_snapshot(BROKER_UNIT, environment))
    args.aos_unit = "swapp-aos-provider-bootstrap-" + uuid.uuid4().hex + ".service"
    require_absent(unit_snapshot(args.aos_unit, environment))
    args.workdir.mkdir(mode=0o700)
    os.umask(0o077)
    manifest = dict(
        aos_unit=args.aos_unit, source_files={name: file_hash(ROOT / name) for name in SOURCE_FILES}
    )
    (args.workdir / "bootstrap-source.json").write_bytes(canonical(manifest))
    command = service_command(
        BROKER_UNIT,
        Path(sys.executable),
        Path(__file__).resolve(),
        [*source_arguments(args), "--execute", "--broker-stage", "--aos-unit", args.aos_unit],
        args,
        environment,
    )
    # Check again immediately before transient creation. systemd-run itself refuses
    # an existing unit, so a concurrent creator cannot cause replacement.
    require_absent(unit_snapshot(BROKER_UNIT, environment))
    errors = []
    child, threads = launch(
        command, subprocess.DEVNULL, args.workdir, environment, "broker", errors
    )
    try:
        child.wait(timeout=120)
        for thread in threads:
            thread.join(timeout=3)
        require(
            child.returncode == 0 and not errors, "broker CPU service failed; inspect private logs"
        )
        result = json.loads((args.workdir / "broker-result.json").read_bytes())
        preflight(args)
        verify_bootstrap_manifest(args)
        deadline = time.monotonic() + 10
        while True:
            snapshots = {
                unit: unit_snapshot(unit, environment) for unit in (BROKER_UNIT, args.aos_unit)
            }
            if all(item.get("LoadState") == "not-found" for item in snapshots.values()):
                break
            require(time.monotonic() < deadline, "owned transient service did not disappear")
            time.sleep(0.1)
        from lab.llm.gpu_scheduler import ProcessIdentity, _process_identity_alive

        for name in ("server_generation", "peer_generation"):
            generation = result[name]
            identity = ProcessIdentity(
                generation["pid"], generation["start_ticks"], generation["boot_id"]
            )
            require(
                not _process_identity_alive(identity),
                "original owned service process is still alive or unreadable: " + name,
            )
        result.update(
            broker_launcher_exit_code=child.returncode,
            collected_unit_snapshots=snapshots,
            owned_services_disappeared=True,
            broker_original_process_absent=True,
            aos_original_process_absent=True,
            outer_resource_preflight=resources,
        )
        (args.workdir / "report.json").write_bytes(canonical(result))
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if child.poll() is None:
            ownership_path = args.workdir / "owned-generation.json"
            if ownership_path.is_file() and not ownership_path.is_symlink():
                require(ownership_path.stat().st_size < 64 * 1024, "oversized ownership receipt")
                generation = json.loads(ownership_path.read_bytes())["peer_generation"]
                stop_owned_aos(args.aos_unit, generation, environment)
            # The canonical broker is never stopped by this driver. Its exact
            # transient invocation has RuntimeMaxSec=180 even if this wait fails.
        for thread in threads:
            thread.join(timeout=1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--native-bootstrap", action="store_true")
    parser.add_argument("--broker-stage", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--aos-unit", help=argparse.SUPPRESS)
    for name in ("aos-root", "workdir", "preflight-report"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in (
        "preflight-sha256",
        "head",
        "selected-source-sha256",
        "tracked-diff-sha256",
        "selected-untracked-sha256",
        "retained-host-sha256",
    ):
        parser.add_argument("--expected-" + name, required=True)
    args = parser.parse_args(argv)
    for name in (
        "preflight_sha256",
        "selected_source_sha256",
        "tracked_diff_sha256",
        "selected_untracked_sha256",
        "retained_host_sha256",
    ):
        require(
            re.fullmatch(r"[0-9a-f]{64}", getattr(args, "expected_" + name)),
            "explicit lowercase SHA256 source pins required",
        )
    require(re.fullmatch(r"[0-9a-f]{40}", args.expected_head), "exact AOS commit required")
    require(not args.aos_root.is_symlink(), "AOS source root symlink forbidden")
    args.aos_root = args.aos_root.resolve(strict=True)
    args.workdir = args.workdir.absolute()
    args.preflight_report = args.preflight_report.resolve(strict=True)
    args.retained_provider = True
    require(not args.workdir.resolve().is_relative_to(args.aos_root), "no AOS writes allowed")
    require(len(os.fsencode(args.workdir / "control.sock")) < 104, "Unix socket path too long")
    if args.broker_stage:
        require(
            args.execute and args.aos_unit and UNIT_PATTERN.fullmatch(args.aos_unit),
            "broker stage requires explicit execution and unique owned unit",
        )
        require(
            args.workdir.is_dir()
            and not args.workdir.is_symlink()
            and args.workdir.stat().st_uid == os.getuid()
            and args.workdir.stat().st_mode & 0o077 == 0,
            "broker stage requires private owned workdir",
        )
    else:
        require(args.aos_unit is None, "AOS unit name is generated by the driver")
        require(not args.workdir.exists() and not args.workdir.is_symlink(), "workdir must be new")
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.broker_stage:
        os.umask(0o077)
        broker_stage(args)
        return 0
    return outer_stage(args)


if __name__ == "__main__":
    raise SystemExit(main())
