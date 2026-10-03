"""AOS-interpreter half of the explicitly synthetic CPU composition runner.

No Lab imports. Original source/physical callbacks compare the parent producer's
separately retained fixture snapshots; these callbacks are NOT live providers.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import sqlite3
import sys
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def imports_manifest(aos_root):
    """Observed import closure; explicitly not deployment/runtime attestation."""
    result = {}
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        if not path.is_file():
            continue
        if name == "aos" or name.startswith("aos."):
            require(path.is_relative_to(aos_root / "src"), "foreign AOS import: " + name)
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            require(path.is_relative_to(aos_root / ".venv"), "foreign dependency import: " + name)
        result[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return result


def main():
    workdir, aos_root = map(Path, sys.argv[1:3])
    provider_fd = None if len(sys.argv) == 3 else int(sys.argv[3])
    require(sys.prefix == str(aos_root / ".venv"), "AOS interpreter isolation required")
    require(
        Path.cwd() == workdir and workdir.stat().st_mode & 0o077 == 0,
        "private owned working directory required",
    )
    deadline = time.monotonic() + 20
    while not (workdir / "fixture.json").exists():
        require(time.monotonic() < deadline, "Scientist fixture handshake timed out")
        time.sleep(0.025)
    fixture = json.loads((workdir / "fixture.json").read_bytes())
    native_bootstrap = fixture.get("native_bootstrap") is True

    from aos.scientist_admission_history import ScientistAdmissionHistory
    from aos.scientist_budget_witness import (
        ScientistBudgetWitnessVerifier,
        ScientistRetainedTerminalVerifier,
        budget_witness_schema,
    )
    from aos.scientist_evidence_client import ScientistEvidenceClient
    from aos.scientist_evidence_journal import ScientistEvidenceJournal
    from aos.scientist_intents import ScientistIntentBinding, ScientistIntentJournal
    from aos.scientist_profile_output import admission_profile_validator
    from aos.scientist_protocol import ScientistTurnRequest, scientist_request_frame
    from aos.scientist_resolution import ScientistResolutionJournal
    from aos.scientist_retained_evidence_transport import ScientistRetainedEvidenceCodec
    from aos.scientist_terminal import TERMINAL_DESCRIPTOR_SHA256, terminal_schema_sha256
    from aos.scientist_transport import (
        BrokerPeer,
        ScientistAdmissionError,
        SystemdBrokerAuthenticator,
        _process_identity,
    )
    from aos.storage import TrajectoryStore

    before_imports = imports_manifest(aos_root)
    server = fixture["server"]
    peer = BrokerPeer(**{key: value for key, value in server.items() if key != "unit"})
    request = ScientistTurnRequest.model_validate(fixture["request"], strict=True)
    binding = ScientistIntentBinding(
        session_id="cpu-resolution-session",
        runtime_id="cpu-runtime",
        owner="AGENT",
        lease_id="cpu-lease",
        generation=0,
        authorization_context_sha256="a" * 64,
    )
    caller = fixture["capture"]["admission_binding"]["caller_generation"]
    real_service_identity = fixture.get("real_service_identity") is True
    require(
        not native_bootstrap or real_service_identity and provider_fd is not None,
        "native bootstrap CPU proof requires actual service identity and inherited provider",
    )
    native_authenticator = SystemdBrokerAuthenticator() if real_service_identity else None
    require(
        caller["pid"] == os.getpid()
        and (real_service_identity or peer.pid == os.getppid())
        and caller["uid"] == os.getuid() == peer.uid,
        "owned fixture process identity changed",
    )
    require(
        digest(request.model_dump(mode="json")) == fixture["target"]["request_sha256"],
        "original request hash mismatch",
    )

    def current_peer(observed):
        if real_service_identity:
            return (
                observed == peer
                and _process_identity(os.getpid())
                == (caller["start_ticks"], caller["boot_id"], caller["control_group"])
                and native_authenticator.still_current(peer)
            )
        return observed == peer and os.getppid() == peer.pid and os.getuid() == peer.uid

    if real_service_identity:
        require(
            native_authenticator.authenticate(peer.pid, peer.uid) == peer and current_peer(peer),
            "actual systemd service identity differs",
        )

    class SyntheticAuthenticator:
        def authenticate(self, pid, uid):
            if (pid, uid) != (peer.pid, peer.uid) or not current_peer(peer):
                raise ScientistAdmissionError("synthetic owned server changed")
            return peer

        still_current = staticmethod(current_peer)

    broker_authenticator = (
        native_authenticator if real_service_identity else SyntheticAuthenticator()
    )

    def check(condition, message):
        if not condition:
            raise ScientistAdmissionError(message)

    def exact_original(original):
        check(
            original.request_id == request.request_id
            and original.request_sha256 == fixture["target"]["request_sha256"]
            and original.session_id == binding.session_id
            and original.intent_binding_sha256 == digest(binding.model_dump(mode="json"))
            and original.admission_binding.model_dump(mode="json")
            == fixture["capture"]["admission_binding"],
            "synthetic original identity differs",
        )

    def capture(observed_request, observed_binding, observed_peer):
        check(
            observed_request == request
            and observed_binding == binding
            and current_peer(observed_peer),
            "fixture capture cannot admit a different turn",
        )
        return deepcopy(fixture["capture"])

    def verify_history(original, observed_request, observed_binding, observed_peer):
        exact_original(original)
        check(
            observed_request == request
            and observed_binding == binding
            and current_peer(observed_peer),
            "original admission current authority changed",
        )

    store = TrajectoryStore(workdir / "aos.sqlite3")
    try:
        native_bootstrap_audit = None
        factory = None
        if native_bootstrap:
            from aos.desktop_control import DesktopController
            from aos.scientist_bootstrap import CONTROL_DESCRIPTOR_SHA256
            from aos.scientist_bootstrap_factory import ScientistBootstrapAdmissionFactory

            # Only the runtime is synthetic. Session/owner/lease/generation and
            # the Store are created by the actual native desktop controller.
            runtime = SimpleNamespace(
                runtime_id="cpu-native-bootstrap-runtime", pins={"image_id": "synthetic-image"}
            )
            controller = DesktopController(store, runtime)
            desktop = controller.state()
            binding = ScientistIntentBinding(
                **{
                    key: desktop[key]
                    for key in ("session_id", "runtime_id", "owner", "lease_id", "generation")
                },
                authorization_context_sha256="a" * 64,
            )
            reviewed = {request.profile_id: deepcopy(fixture["capture"]["admission_binding"])}
            source_pins = fixture.get("aos_source_sha256")
            require(
                type(source_pins) is dict and source_pins,
                "native bootstrap requires explicit independent AOS source pins",
            )

            def verify_source(selected):
                check(
                    {profile: value.model_dump(mode="json") for profile, value in selected.items()}
                    == reviewed
                    and current_peer(peer),
                    "independent native CPU source or full reviewed binding changed",
                )
                for relative, checksum in source_pins.items():
                    source = aos_root / relative
                    check(
                        not Path(relative).is_absolute()
                        and ".." not in Path(relative).parts
                        and source.resolve().is_relative_to(aos_root)
                        and hashlib.sha256(source.read_bytes()).hexdigest() == checksum,
                        "independently pinned AOS source changed: " + relative,
                    )

            factory = ScientistBootstrapAdmissionFactory(
                reviewed,
                Path(fixture["socket_path"]),
                control_descriptor_sha256=CONTROL_DESCRIPTOR_SHA256,
                verify_source=verify_source,
                authenticator=native_authenticator,
                clock=lambda: fixture["synthetic_clock"],
            )
            history = factory(controller)
            require(
                history.store is store
                and history.capture.store is store
                and history.record_version == "2.0",
                "native bootstrap must preserve the exact original Store and history2.0",
            )
            factory.confirm_runtime({request.profile_id: request.deployment_digest})
            history.capture.prepare(request, binding, factory.expected_peer(request, binding))
            require(
                not store.connection.in_transaction,
                "native bootstrap preparation entered original intent transaction",
            )
            with sqlite3.connect(f"file:{workdir / 'aos.sqlite3'}?mode=ro", uri=True) as reader:
                reader.row_factory = sqlite3.Row
                events = reader.execute(
                    "SELECT * FROM desktop_events WHERE kind='scientist_bootstrap_intent'"
                ).fetchall()
                require(len(events) == 1, "native bootstrap audit was not independently committed")
                event = dict(events[0])
                payload = json.loads(event["payload_json"])
                bootstrap_frame = payload["control_frame"].encode("utf-8")
                bootstrap_control = json.loads(bootstrap_frame)
                require(
                    event["session_id"] == binding.session_id
                    and event["event_id"] == bootstrap_control["control_id"]
                    and payload["authority"] == "audit-only"
                    and canonical(payload).decode() == event["payload_json"]
                    and canonical(bootstrap_control) == bootstrap_frame
                    and payload["control_sha256"] == hashlib.sha256(bootstrap_frame).hexdigest()
                    and payload["broker_peer"] == asdict(peer)
                    and payload["intent_binding"] == binding.model_dump(mode="json")
                    and payload["request_id"] == request.request_id
                    and payload["request_sha256"] == fixture["target"]["request_sha256"]
                    and bootstrap_control["op"] == "capability"
                    and bootstrap_control["target"] is None
                    and bootstrap_control["expected_capability_sha256"] is None
                    and bootstrap_control["profile_id"] == request.profile_id
                    and bootstrap_control["deployment_digest"] == request.deployment_digest,
                    "native committed bootstrap audit differs from exact original context",
                )
                native_bootstrap_audit = dict(
                    event_sha256=digest(event),
                    control_sha256=payload["control_sha256"],
                    event_id=event["event_id"],
                    original_request_sha256=payload["request_sha256"],
                )
        else:
            with store.connection:
                store.connection.execute(
                    "INSERT INTO desktop_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        binding.session_id,
                        binding.runtime_id,
                        "synthetic-image",
                        binding.owner,
                        binding.lease_id,
                        binding.generation,
                        "running",
                        "synthetic",
                        "synthetic",
                    ),
                )
            history = ScientistAdmissionHistory(
                store,
                capture=capture,
                verify_current=verify_history,
                clock=lambda: fixture["synthetic_clock"],
                record_version="2.0",
            )
        intents = ScientistIntentJournal(store, binding, admission_history=history)
        frame = scientist_request_frame(request)[:-1]
        intents.persist_intent(
            frame, hashlib.sha256(frame).hexdigest(), time.monotonic() + 60, peer
        )
        original, original_sha = history.read(request.request_id)
        original_intent = tuple(
            store.connection.execute(
                "SELECT * FROM scientist_turn_intents WHERE request_id=?", (request.request_id,)
            ).fetchone()
        )
        original_history = tuple(
            store.connection.execute(
                "SELECT * FROM scientist_admission_history WHERE request_id=?",
                (request.request_id,),
            ).fetchone()
        )
        fresh = request.model_copy(update={"request_id": "f" * 32}, deep=True)

        def must_deny(operation):
            try:
                operation()
            except ScientistAdmissionError:
                return
            raise ValueError("mandatory denial did not occur")

        must_deny(lambda: intents.verify_admission(fresh))

        def control_authority(control, observed_original, observed_binding, observed_peer):
            exact_original(observed_original)
            check(
                control["target"] == fixture["target"]
                and control["op"] in {"capability", "reconcile"}
                and control["profile_id"] == request.profile_id
                and control["deployment_digest"] == request.deployment_digest
                and control["evidence_schema_sha256"] == fixture["evidence_sha256"]
                and control["transport_schema_sha256"] == fixture["transport_sha256"]
                and observed_binding == binding
                and current_peer(observed_peer),
                "synthetic target cleanup authority changed",
            )

        schema_bytes = Path(fixture["schema_path"]).read_bytes()

        def journal(capability=None):
            codec = ScientistRetainedEvidenceCodec(
                schema_bytes,
                transport_schema_sha256=fixture["transport_sha256"],
                evidence_schema_sha256=fixture["evidence_sha256"],
                expected_capability=capability,
            )
            return ScientistEvidenceJournal(
                store,
                binding,
                codec=codec,
                admission_history=history,
                verify_control=control_authority,
            )

        def exchange(evidence, control):
            return ScientistEvidenceClient(
                Path(fixture["socket_path"]),
                codec=evidence.codec,
                timeout_seconds=5,
                authenticator=broker_authenticator,
                authorize=evidence.authorize,
                persist_intent=evidence.persist_intent,
                record_response=evidence.record_response,
            ).exchange(control)

        control = dict(
            schema="aos-scientist-control-evidence.v3",
            version=3,
            op="capability",
            control_id="3" * 32,
            profile_id=request.profile_id,
            deployment_digest=request.deployment_digest,
            target=fixture["target"],
            expected_capability_sha256=None,
            evidence_schema_sha256=fixture["evidence_sha256"],
            transport_schema_sha256=fixture["transport_sha256"],
        )
        discovery = journal()
        discovered = exchange(discovery, control)
        require(discovered["ok"] is True, "capability discovery denied")
        capability = discovered["data"]["capability"]
        evidence = journal(capability)
        control.update(
            op="reconcile", control_id="4" * 32, expected_capability_sha256=digest(capability)
        )
        response = exchange(evidence, control)
        require(response["ok"] is True, "retained evidence response denied")
        ack = evidence.inspect(control["control_id"])
        require(ack["response_sha256"] == digest(response), "durable response digest changed")
        must_deny(lambda: intents.verify_admission(fresh))
        counts = {"budget": 0, "resolver": 0, "physical_fixture": 0, "resolution": 0, "result": 0}
        provider_adapter = None
        provider_channel = None
        provider_reads_in_resolution = [0]
        if provider_fd is not None:
            from aos.scientist_retained_host import ScientistRetainedHost

            if native_bootstrap:
                from aos.scientist_retained_provider import (
                    ScientistRetainedProviderAdapter,
                    ScientistRetainedProviderClient,
                )

                client_type, adapter_type = (
                    ScientistRetainedProviderClient,
                    ScientistRetainedProviderAdapter,
                )
            else:
                module = runpy.run_path(
                    str(Path(__file__).with_name("aos_retained_provider_adapter.py"))
                )
                client_type, adapter_type = (
                    module["RetainedProviderClient"],
                    module["RetainedProviderAdapter"],
                )
            provider_channel = socket.socket(fileno=provider_fd)
            provider_client = client_type(
                provider_channel,
                authenticator=broker_authenticator,
                expected_peer=peer,
            )
            host = ScientistRetainedHost(
                store,
                history,
                binding,
                socket_path=Path(fixture["socket_path"]),
                reviewed_schema_bytes=schema_bytes,
                transport_schema_sha256=fixture["transport_sha256"],
                evidence_schema_sha256=fixture["evidence_sha256"],
                authenticator=broker_authenticator,
                verify_control=control_authority,
            )

            def provider_current(_operation, observed_original, observed_binding):
                exact_original(observed_original)
                check(
                    observed_binding == binding and current_peer(peer),
                    "synthetic provider authority changed",
                )
                if store.connection.in_transaction:
                    provider_reads_in_resolution[0] += 1

            provider_adapter = adapter_type(
                host,
                provider_client,
                capability_control_id="3" * 32,
                capability_response_sha256=discovery.inspect("3" * 32)["response_sha256"],
                reconcile_control_id=control["control_id"],
                reconcile_response_sha256=ack["response_sha256"],
                verify_current=provider_current,
            )

        def budget_source(observed_request, observed_original, witness):
            exact_original(observed_original)
            check(
                observed_request == request
                and witness.model_dump(mode="json", by_alias=True) == fixture["witness"]
                and current_peer(peer),
                "independent fixture budget source changed",
            )
            counts["budget"] += 1

        def resolver(observed_original, terminal):
            exact_original(observed_original)
            check(
                terminal.model_dump(mode="json", by_alias=True) == fixture["terminal"]
                and current_peer(peer),
                "synthetic current resolver revoked",
            )
            counts["resolver"] += 1

        def physical(observed_original, terminal, allocation, drain, no_admission):
            resolver(observed_original, terminal)
            check(
                allocation is None and drain is None and no_admission == fixture["no_admission"],
                "synthetic original nonallocation proof changed",
            )
            counts["physical_fixture"] += 1

        output_validator = admission_profile_validator(
            history, fixture["output_pin"], context_tokens=1536
        )

        def result_validator(observed_request, receipt):
            counts["result"] += 1
            return output_validator(observed_request, receipt)

        budget = ScientistBudgetWitnessVerifier(
            history,
            schema_sha256=digest(budget_witness_schema()),
            verify_source=budget_source
            if provider_adapter is None
            else provider_adapter.verify_source,
            read_source=None if provider_adapter is None else provider_adapter.read_source,
        )
        verifier = ScientistRetainedTerminalVerifier(
            budget,
            evidence_schema_bytes=Path(fixture["evidence_schema_path"]).read_bytes(),
            evidence_schema_sha256=fixture["evidence_sha256"],
            descriptor_sha256=TERMINAL_DESCRIPTOR_SHA256,
            terminal_schema_sha256=terminal_schema_sha256(),
            validate_result=result_validator,
            verify_resolver=resolver
            if provider_adapter is None
            else provider_adapter.verify_resolver,
            verify_physical=physical
            if provider_adapter is None
            else provider_adapter.verify_physical,
        )
        default_denied = ScientistResolutionJournal(evidence, verifier=verifier)
        must_deny(
            lambda: default_denied.resolve(
                control["control_id"], response_sha256=ack["response_sha256"]
            )
        )
        require(
            store.connection.execute("SELECT count(*) FROM scientist_turn_resolutions").fetchone()[
                0
            ]
            == 0,
            "default-deny resolution wrote a row",
        )

        def resolution_authority(observed_original, observed_binding, terminal, observed_peer):
            resolver(observed_original, terminal)
            check(
                observed_binding == binding and current_peer(observed_peer),
                "synthetic original resolution authority changed",
            )
            counts["resolution"] += 1

        resolution = ScientistResolutionJournal(
            evidence, verifier=verifier, verify_resolution=resolution_authority
        )
        resolved = resolution.resolve(control["control_id"], response_sha256=ack["response_sha256"])
        require(
            resolved["admission_record_sha256"] == original_sha,
            "resolution replaced original admission",
        )
        require(
            resolution.resolve(control["control_id"], response_sha256=ack["response_sha256"])
            == resolved,
            "exact resolution retry changed durable receipt",
        )
        require(
            tuple(
                store.connection.execute(
                    "SELECT * FROM scientist_turn_intents WHERE request_id=?", (request.request_id,)
                ).fetchone()
            )
            == original_intent,
            "original intent was rewritten",
        )
        require(
            tuple(
                store.connection.execute(
                    "SELECT * FROM scientist_admission_history WHERE request_id=?",
                    (request.request_id,),
                ).fetchone()
            )
            == original_history,
            "original admission was rewritten",
        )
        with sqlite3.connect(f"file:{workdir / 'aos.sqlite3'}?mode=ro", uri=True) as reader:
            require(
                reader.execute("SELECT count(*) FROM scientist_turn_resolutions").fetchone()[0]
                == 1,
                "resolution is not independently durable",
            )
        must_deny(lambda: intents.verify_admission(request))
        intents.verify_admission(fresh)
        if native_bootstrap:
            with store.connection:
                store.connection.execute(
                    "UPDATE desktop_sessions SET status='paused',generation=generation+1 "
                    "WHERE session_id=?",
                    (binding.session_id,),
                )
            must_deny(lambda: factory.expected_peer(fresh, binding))
        if provider_adapter is not None:
            require(
                provider_reads_in_resolution[0] > 0,
                "provider did not run while original resolution transaction was open",
            )
            provider_socket_metadata = dict(
                fd0=provider_fd == 0,
                family=provider_channel.family,
                socket_type=provider_channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE),
                passcred=provider_channel.getsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED),
                exchange_count=provider_client._sequence,
            )
            provider_channel.close()
        else:
            provider_socket_metadata = None
        after_imports = imports_manifest(aos_root)
        require(
            all(after_imports.get(name) == value for name, value in before_imports.items()),
            "imported dependency bytes changed during composition",
        )
        (workdir / "imports.json").write_bytes(canonical(after_imports))
        report = dict(
            cpu_composition_passed=True,
            scope=(
                "real_service_identity_synthetic_admission_canceled_before_allocation"
                if real_service_identity
                else "synthetic_auth_admitted_canceled_before_allocation"
            ),
            next_admission_gate_passed=True,
            original_infer_replay_denied=True,
            default_resolution_authority_denied=True,
            original_intent_and_history_unchanged=True,
            durable_resolution_sha256=digest(resolved),
            response_sha256=ack["response_sha256"],
            callback_counts=counts,
            retained_provider_used=provider_adapter is not None,
            real_service_identity=real_service_identity,
            native_bootstrap_factory_used=native_bootstrap,
            native_aos_provider_used=native_bootstrap and provider_adapter is not None,
            native_bootstrap_audit_committed=native_bootstrap_audit is not None,
            native_bootstrap_audit=native_bootstrap_audit,
            native_bootstrap_confirm_runtime_checked=native_bootstrap,
            native_bootstrap_revoked_desktop_denied=native_bootstrap,
            provider_socket_metadata=provider_socket_metadata,
            provider_checks_in_resolution_transaction=provider_reads_in_resolution[0],
            imported_closure_sha256=digest(after_imports),
            selected_attestation_is_not_dependency_attestation=True,
            remaining=[
                "live authorization and independent physical providers",
                "completed typed model result",
                "real shared GPU fairness",
                "next inference execution",
                "deployment dependency attestation",
            ],
        )
        (workdir / "consumer-result.json").write_bytes(canonical(report))
    finally:
        store.close()


if __name__ == "__main__":
    main()
