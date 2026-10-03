#!/usr/bin/env python3
"""Bounded CPU composition, with synthetic authority; never native acceptance.

Run with Scientist's Python. A separate AOS Python consumes the real v3 socket.
Only a new private workdir is written. No model, scheduler acquisition, systemd,
or canonical runtime database is used. The scheduler constructor initializes
the private SQL schema only; its resolver and drain callbacks always raise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import socket
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def preflight(args):
    """Recheck the root-reviewed receipt and its exact selected snapshot."""
    native_bootstrap = getattr(args, "native_bootstrap", False)
    expected_profile = (
        "bootstrap_factory_candidate_v1" if native_bootstrap else "resolution_candidate_v3"
    )
    require(
        file_hash(args.preflight_report) == args.expected_preflight_sha256,
        "preflight receipt hash mismatch",
    )
    receipt = json.loads(args.preflight_report.read_bytes())
    report = receipt.get("report", receipt)
    require(
        report.get("schema") == "aos_lab_source_preflight.v1"
        and report.get("source_ready") is True
        and report.get("source_profile") == expected_profile
        and report.get("snapshot_kind") == "reviewed_snapshot"
        and Path(report["source_root"]).resolve() == args.aos_root,
        "root-reviewed selected AOS preflight required",
    )
    require(
        report.get("admission_allowed") is False,
        "source preflight must not grant runtime admission",
    )
    sys.path.insert(0, str(ROOT))
    from scripts.check_aos_lab_compatibility import inspect_source

    current = inspect_source(
        args.aos_root,
        args.expected_head,
        snapshot_kind="reviewed_snapshot",
        source_profile=expected_profile,
        expected_selected_source_sha256=args.expected_selected_source_sha256,
        expected_tracked_diff_sha256=args.expected_tracked_diff_sha256,
        expected_selected_untracked_sha256=args.expected_selected_untracked_sha256,
    )
    require(
        current.get("source_ready") is True and current.get("admission_allowed") is False,
        "guarded preflight rejected current AOS snapshot: " + repr(current.get("issues")),
    )
    for key in (
        "expected_head",
        "selected_source_sha256",
        "tracked_diff_sha256",
        "selected_untracked_sha256",
        "source_sha256",
    ):
        require(current[key] == report[key], "preflight receipt changed: " + key)
    require(
        len(current["source_sha256"]) == (59 if native_bootstrap else 54),
        "exact selected AOS source set required",
    )
    if args.retained_provider:
        require(
            args.expected_retained_host_sha256 is not None
            and file_hash(args.aos_root / "src/aos/scientist_retained_host.py")
            == args.expected_retained_host_sha256,
            "additional retained-host source pin differs",
        )
    return report


def make_fixture(
    workdir,
    child,
    source_report,
    *,
    retained_provider=False,
    peer_generation=None,
    server_generation=None,
    authenticator=None,
    server_current=None,
):
    # These imports occur only in the Scientist interpreter.
    sys.path.insert(0, str(ROOT))
    from dataclasses import asdict

    from lab.llm import aos_evidence_transport as v2
    from lab.llm import aos_retained_evidence_transport as v3
    from lab.llm.aos_gpu_broker import PeerGeneration
    from lab.llm.aos_gpu_control import ControlError, ControlPolicy, LabAOSControl
    from lab.llm.aos_gpu_control_store import ControlAuthority, ControlStore
    from lab.llm.aos_gpu_executor import SystemdAOSProfileRuntime, TurnBudgets
    from lab.llm.gpu_scheduler import SharedGpuScheduler

    boot = "11111111-1111-1111-1111-111111111111"
    server = dict(
        uid=os.getuid(),
        pid=os.getpid(),
        start_ticks=90,
        boot_id=boot,
        unit="swapp-lab-gpu-broker.service",
        invocation_id="f" * 32,
        control_group="/synthetic/lab",
    )
    peer = PeerGeneration(
        os.getuid(),
        child.pid,
        100,
        boot,
        "swapp-aos-gpu-fixture.service",
        "e" * 32,
        "/synthetic/aos",
        os.getpid(),
        90,
    )
    real_service_identity = peer_generation is not None
    require(
        real_service_identity == (server_generation is not None)
        and real_service_identity == (authenticator is not None)
        and real_service_identity == (server_current is not None),
        "real-service identity overrides must be supplied together",
    )
    if real_service_identity:
        peer, server = peer_generation, dict(server_generation)
        boot = server["boot_id"]
        require(
            peer.boot_id == boot and peer.pid == child.pid and server_current(),
            "real-service bootstrap identity differs",
        )
    bundle = json.loads((ROOT / "lab/llm/contracts/profile_output_v2/bundle.json").read_bytes())
    output_pin = dict(
        name="aos-scientist-profile-output.v2", version=2, bundle_sha256=digest(bundle)
    )
    profile = SimpleNamespace(
        profile_id="aos.decider.turn.v1",
        deployment_digest="b" * 64,
        manifest_sha256="c" * 64,
        config_sha256="d" * 64,
        response_schema_sha256="a" * 64,
        output_contract=output_pin,
        budgets=TurnBudgets(10, 5, 15, 45),
        max_output_tokens=16,
        context_tokens=1536,
    )
    policy_data = dict(
        enabled=True,
        history_reconcile=False,
        evidence_schema_sha256=v2.EVIDENCE_SCHEMA_SHA256,
        evidence_transport_schema_sha256=v2.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        retained_evidence_transport_schema_sha256=v3.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        source_files={
            "aos": source_report["source_sha256"],
            "scientist": {str(Path(__file__).name): file_hash(Path(__file__))},
        },
    )
    policy_path = workdir / "synthetic-policy.json"
    if retained_provider:
        policy_data["source_files"]["scientist"] = {
            name: file_hash(ROOT / name)
            for name in (
                "lab/llm/aos_retained_provider.py",
                "lab/llm/aos_physical_readback.py",
                "scripts/aos_retained_provider_adapter.py",
            )
        }
    policy_path.write_bytes(canonical(policy_data))

    class SyntheticAuth:
        def still_current(self, observed):
            return observed == peer and child.poll() is None

        def authenticate(self, pid, uid):
            if (pid, uid) != (peer.pid, peer.uid) or not self.still_current(peer):
                raise ControlError("unauthorized")
            return peer

    class SyntheticPolicy:
        config = policy_data
        sha256 = file_hash(policy_path)
        _pin = staticmethod(ControlPolicy._pin)

        def verify_policy_hash(self):
            if file_hash(policy_path) != self.sha256:
                raise ControlError("capability_mismatch")

        def bound_profile(self, observed, profile_id, deployment):
            self.verify_policy_hash()
            if observed != peer or (profile_id, deployment) != (
                profile.profile_id,
                profile.deployment_digest,
            ):
                raise ControlError("unauthorized")
            return profile

        def verify(self):
            self.verify_policy_hash()
            if retained_provider:
                for name, expected in policy_data["source_files"]["scientist"].items():
                    require(file_hash(ROOT / name) == expected, "provider source changed")

    def forbidden(*_arguments):
        raise RuntimeError("CPU composition must never resolve/acquire/drain a GPU lease")

    store = ControlStore(
        workdir / "scientist.sqlite3",
        clock=lambda: 100.0,
        boot_id=lambda: boot,
        peer_verifier=lambda observed: (
            observed == asdict(peer)
            and child.poll() is None
            and (authenticator is None or authenticator.still_current(peer))
        ),
    )
    SharedGpuScheduler(
        store.database,
        principal_resolver=SimpleNamespace(resolve=forbidden, verify=forbidden),
        drain_verifier=forbidden,
        control_store=store,
    )
    if retained_provider:
        SystemdAOSProfileRuntime(
            store.database,
            units=SimpleNamespace(inspect=forbidden, cgroup_empty=forbidden),
            gpu=SimpleNamespace(snapshot=forbidden),
            work_root=workdir / "unused-workers",
            control_store=store,
        )
    control = LabAOSControl(
        policy=SyntheticPolicy(),
        authenticator=SyntheticAuth() if authenticator is None else authenticator,
        store=store,
        server_generation=server,
        clock=lambda: 100.0,
        server_current=server_current,
    )
    minted = control._mint(peer, profile)["capability"]
    admission = control.admit_infer(peer, profile.profile_id, profile.deployment_digest)
    request = dict(
        version=1,
        op="infer",
        request_id="1" * 32,
        profile_id=profile.profile_id,
        deployment_digest=profile.deployment_digest,
        payload={"fixture": "CPU canceled"},
    )
    target = dict(
        request_id=request["request_id"],
        request_sha256=digest(request),
        original_peer_generation_sha256=digest(asdict(peer)),
    )
    budget = {**asdict(profile.budgets), "max_output_tokens": 16, "context_tokens": 1536}
    store.register_intent(
        asdict(peer),
        request["request_id"],
        digest(request),
        profile.profile_id,
        profile.deployment_digest,
        profile.config_sha256,
        profile.response_schema_sha256,
        160.0,
        budget,
        admission=admission,
    )
    canceled = control.handle(
        peer,
        dict(
            schema="aos-scientist-control.v1",
            version=1,
            op="cancel",
            control_id="2" * 32,
            expected_capability_sha256=digest(minted),
            target=target,
            profile_id=profile.profile_id,
            deployment_digest=profile.deployment_digest,
        ),
    )
    terminal = canceled["data"]["terminal_receipt"]
    authority = ControlAuthority(admission.binding_json, None, admission.verify_current)
    witness = store.read_original_budget(
        asdict(peer), target, profile.profile_id, profile.deployment_digest, authority=authority
    )
    # Retain original SQL evidence independently of the later socket response.
    with closing(store._connect()) as connection:
        row = connection.execute(
            "SELECT * FROM aos_control_requests WHERE request_id=?", (request["request_id"],)
        ).fetchone()
        no_admission = json.loads(row["no_admission_json"])
        require(
            connection.execute("SELECT count(*) FROM gpu_turn_requests").fetchone()[0] == 0,
            "unexpected scheduler submission",
        )
    capture = dict(
        admission_binding=json.loads(admission.binding_json),
        capability_sha256=digest(minted),
        capability_freshness={
            key: minted[key] for key in ("boot_id", "issued_boottime", "expires_boottime")
        },
    )
    return control, dict(
        request=request,
        target=target,
        server=server,
        capture=capture,
        witness=witness,
        terminal=terminal,
        no_admission=no_admission,
        output_pin=output_pin,
        transport_sha256=v3.EVIDENCE_TRANSPORT_SCHEMA_HASH,
        evidence_sha256=v3.EVIDENCE_SCHEMA_SHA256,
        schema_path=str(
            ROOT / "docs/ai-scientist/contracts/retained-evidence-transport-v3.schema.json"
        ),
        evidence_schema_path=str(
            ROOT / "lab/llm/contracts/control_v2/terminal-evidence.schema.json"
        ),
        synthetic_clock=100.0,
        socket_path=str(workdir / "control.sock"),
        real_service_identity=real_service_identity,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aos-root", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--preflight-report", type=Path, required=True)
    parser.add_argument("--retained-provider", action="store_true")
    parser.add_argument("--expected-retained-host-sha256")
    for name in (
        "preflight-sha256",
        "head",
        "selected-source-sha256",
        "tracked-diff-sha256",
        "selected-untracked-sha256",
    ):
        parser.add_argument("--expected-" + name, required=True)
    args = parser.parse_args()
    args.aos_root = args.aos_root.resolve()
    args.workdir = args.workdir.absolute()
    require(not args.workdir.exists() and not args.workdir.is_symlink(), "workdir must be new")
    require(not args.workdir.resolve().is_relative_to(args.aos_root), "no AOS writes allowed")
    require(len(os.fsencode(args.workdir / "control.sock")) < 104, "Unix socket path too long")
    report = preflight(args)
    args.workdir.mkdir(mode=0o700)
    os.umask(0o077)
    errors = []
    stopping = threading.Event()
    provider_pair = None
    if args.retained_provider:
        from lab.llm.aos_retained_provider import RetainedProviderServer, prepare_channel

        provider_pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        for channel in provider_pair:
            prepare_channel(channel)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(args.workdir / "control.sock"))
        listener.listen(2)
        listener.settimeout(0.2)
        environment = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1",
            PYTHONPATH=str(args.aos_root / "src"),
        )
        environment.pop("PYTHONHOME", None)
        with (
            (args.workdir / "consumer.stdout").open("wb") as stdout,
            (args.workdir / "consumer.stderr").open("wb") as stderr,
        ):
            child = subprocess.Popen(
                [
                    str(args.aos_root / ".venv/bin/python"),
                    "-B",
                    str(ROOT / "scripts/aos_resolution_composition_fixture.py"),
                    str(args.workdir),
                    str(args.aos_root),
                    *([] if provider_pair is None else [str(provider_pair[1].fileno())]),
                ],
                cwd=args.workdir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=() if provider_pair is None else (provider_pair[1].fileno(),),
            )
            thread = None
            provider_thread = None
            try:
                control, fixture = make_fixture(
                    args.workdir, child, report, retained_provider=args.retained_provider
                )
                if provider_pair is not None:
                    provider_pair[1].close()

                    def no_observation(*_args):
                        raise RuntimeError("no-admission provider must not observe GPU/systemd")

                    provider = RetainedProviderServer(
                        control,
                        units=SimpleNamespace(inspect=no_observation, cgroup_empty=no_observation),
                        gpu=SimpleNamespace(snapshot=no_observation),
                    )
                    provider.verify_configuration()

                    def serve_provider():
                        sequence = 1
                        try:
                            while not stopping.is_set():
                                ready, _, _ = select.select([provider_pair[0]], [], [], 0.2)
                                if not ready:
                                    continue
                                if not provider_pair[0].recv(1, socket.MSG_PEEK):
                                    break
                                provider.serve_once(provider_pair[0], sequence)
                                sequence += 1
                        except Exception as error:
                            errors.append("provider: " + repr(error))

                    provider_thread = threading.Thread(target=serve_provider, daemon=True)
                    provider_thread.start()

                def serve():
                    served = 0
                    try:
                        while not stopping.is_set() and served < 2:
                            try:
                                connection, _address = listener.accept()
                            except TimeoutError:
                                continue
                            with connection:
                                control.serve_connection(connection)
                            served += 1
                    except Exception as error:
                        errors.append(repr(error))

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                temporary = args.workdir / "fixture.tmp"
                temporary.write_bytes(canonical(fixture))
                temporary.replace(args.workdir / "fixture.json")
                child.wait(timeout=35)
                require(child.returncode == 0, "AOS consumer failed; inspect consumer.stderr")
                require(not errors, "Scientist socket failed: " + repr(errors))
                preflight(args)
                result = json.loads((args.workdir / "consumer-result.json").read_bytes())
                require(
                    result.get("cpu_composition_passed") is True,
                    "consumer did not prove composition",
                )
                result.update(
                    preflight_sha256=args.expected_preflight_sha256,
                    selected_source_sha256=args.expected_selected_source_sha256,
                    aos_head=args.expected_head,
                    native_acceptance=False,
                    physical_gpu_proof=False,
                    model_execution=False,
                    next_infer_dispatched=False,
                    runtime_attestation=False,
                    retained_provider_channel=args.retained_provider,
                    retained_host_additional_source_sha256=args.expected_retained_host_sha256,
                )
                (args.workdir / "report.json").write_bytes(canonical(result))
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            finally:
                stopping.set()
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=3)
                if thread is not None:
                    thread.join(timeout=12)
                    require(not thread.is_alive(), "owned control handler did not stop")
                if provider_thread is not None:
                    provider_thread.join(timeout=4)
                    require(not provider_thread.is_alive(), "owned provider handler did not stop")
                if provider_pair is not None:
                    for channel in provider_pair:
                        channel.close()


if __name__ == "__main__":
    main()
