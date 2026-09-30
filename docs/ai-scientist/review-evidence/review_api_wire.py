"""Actual loopback API and separate AOS client; optional real Director execution.

The review creates runs only through HTTP. SQL is read-only except deletion of
its own terminal/inactive fixtures. It never loads a model or touches live AOS.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[3]
AOS_SOURCE = ROOT / "data/runtime/aos-coexistence/source-current"
AOS_PYTHON = ROOT / "data/runtime/aos-coexistence/.venv/bin/python"


def aos_client(arguments: list[str]) -> None:
    """Execute the actual adapter in its separate Python environment."""
    mode, endpoint, token_file, value = arguments
    sys.path.insert(0, str(AOS_SOURCE / "src"))
    from aos.lab_external import LabApiClient, LabStartRequest

    client = LabApiClient(endpoint, Path(token_file))
    if mode == "start":
        request = LabStartRequest.model_validate_json(Path(value).read_bytes(), strict=True)
        result = client.start(request)
    elif mode == "status":
        result = client.status(UUID(value))
    elif mode == "report":
        result = client.report(UUID(value))
    else:
        raise ValueError("unsupported adapter review command")
    print(result.model_dump_json())


def private_json(path: Path, payload: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))


def source_hashes() -> dict[str, str]:
    paths = [ROOT / name for name in (
        "lab/api/app.py", "lab/api/contracts.py", "lab/api/registry.py",
        "lab/cli.py", "lab/db/schema.py",
    )]
    paths.append(AOS_SOURCE / "src/aos/lab_external.py")
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


class WireReview:
    def __init__(self, config: Path, *, execute: bool):
        self.config = config
        self.execute = execute
        self.review_id = uuid4().hex
        self.runtime = config / self.review_id
        self.runtime.mkdir(mode=0o700)
        self.processes: list[tuple[str, subprocess.Popen, object, object]] = []
        self.run_ids: list[str] = []
        self.record: dict = {
            "schema": "api-wire-review.v1", "checked_at": datetime.now(UTC).isoformat(),
            "scope": (
                "Real Lab HTTP server, actual AOS LabApiClient in separate environment, "
                "and PostgreSQL. Optional one-proposal Director/Docker/Scorer execution. "
                "Source-only fake provider and four synthetic EVT tasks. No AOS typed "
                "tool/policy runtime, model, GPU, public data or coexistence acceptance."
            ),
            "execute_director": execute, "checks": {}, "source_sha256": source_hashes(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"

    def check(self, name: str, condition: bool) -> None:
        self.record["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def http(self, method: str, path: str, payload=None, principal="aos"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if principal is not None:
            token = (self.config / f"{principal}.token").read_text().strip()
            headers["Authorization"] = f"Bearer {token}"
        started = time.monotonic()
        try:
            connection.request(method, path, body=None if payload is None else json.dumps(payload),
                               headers=headers)
            response = connection.getresponse()
            body = response.read(256 * 1024 + 1)
            if len(body) > 256 * 1024:
                raise RuntimeError("review response exceeds the bounded report contract")
            return response.status, json.loads(body), time.monotonic() - started
        finally:
            connection.close()

    def client(self, mode: str, value: str) -> dict:
        result = subprocess.run(
            [str(AOS_PYTHON), str(Path(__file__).resolve()), "aos-client", mode,
             self.endpoint, str(self.config / "aos.token"), value],
            cwd=ROOT, capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            name = f"aos-{mode}-{uuid4().hex}.stderr"
            (self.runtime / name).write_text(result.stderr)
            raise RuntimeError(f"AOS adapter {mode} process failed; private log {name}")
        return json.loads(result.stdout)

    def launch(self, purpose: str, arguments: list[str], *, memory: str, seconds: int):
        unit = f"swapp-review-{purpose}-{self.review_id}.service"
        command = [
            "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
            f"--unit={unit}", "--service-type=exec", f"--working-directory={ROOT}",
            f"--property=MemoryMax={memory}", "--property=MemorySwapMax=0",
            "--property=CPUQuota=100%", "--property=TasksMax=128",
            f"--property=RuntimeMaxSec={seconds}", "--property=TimeoutStopSec=15",
            "--property=KillMode=control-group", "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1", "--setenv=MKL_NUM_THREADS=1",
            f"--setenv=LAB_API_PRINCIPALS_FILE={self.config / 'principals.json'}",
            f"--setenv=LAB_SUITE_REGISTRY_FILE={self.config / 'suites.json'}",
            str(ROOT / ".venv/bin/python"), "-m", "lab.cli", *arguments,
        ]
        stdout = (self.runtime / f"{purpose}.stdout").open("w")
        stderr = (self.runtime / f"{purpose}.stderr").open("w")
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
        self.processes.append((unit, process, stdout, stderr))
        self.record.setdefault("commands", []).append(command)
        return process

    def request(self) -> dict:
        return {
            "idempotency_key": f"wire-review-{uuid4().hex}", "track": "anomaly",
            "suite": "synthetic.api-integration.v1", "program_version": "director.v1",
            "budget": {"experiments": 1, "wall_seconds": 1800, "model_tokens": 25000},
            "external_task_id": f"task-{uuid4().hex}",
            "external_run_id": f"run-{uuid4().hex}",
            "external_action_id": f"action-{uuid4().hex}",
        }

    def controls(self) -> tuple[str, dict]:
        server = self.launch("api", ["api", "serve", "--port", str(self.port)],
                             memory="1G", seconds=1920)
        deadline = time.monotonic() + 20
        while True:
            if server.poll() is not None:
                raise RuntimeError("API process exited before readiness")
            try:
                status, health, _ = self.http("GET", "/health", principal=None)
                if status == 200 and health.get("service") == "lab-api":
                    break
            except (OSError, http.client.HTTPException):
                pass
            if time.monotonic() > deadline:
                raise TimeoutError("API readiness deadline exceeded")
            time.sleep(0.1)
        self.check("actual_loopback_server_ready", True)
        self.capture_units()
        request = self.request()
        path = self.runtime / "request.json"
        private_json(path, request)
        accepted = self.client("start", str(path))
        run_id = accepted["run_id"]
        self.run_ids.append(run_id)
        self.record["run_id"] = run_id
        self.check("separate_aos_client_created_queued_run", accepted["state"] == "queued"
                   and not accepted["reused"])
        reused = self.client("start", str(path))
        self.check("identical_retry_same_run", reused["run_id"] == run_id and reused["reused"])
        status = self.client("status", run_id)
        self.check("authenticated_aos_origin", status["origin"] == "aos")
        code, _, _ = self.http("POST", "/v1/runs", request, principal=None)
        self.check("anonymous_start_denied", code == 401)
        changed = dict(request, budget=dict(request["budget"], experiments=2))
        code, _, _ = self.http("POST", "/v1/runs", changed)
        self.check("same_key_changed_payload_conflicts", code == 409)
        changed = dict(request, idempotency_key=f"wire-review-{uuid4().hex}")
        code, _, _ = self.http("POST", "/v1/runs", changed)
        self.check("same_external_action_new_key_conflicts", code == 409)
        for method, suffix in [("GET", ""), ("POST", "/stop"), ("GET", "/report")]:
            code, _, _ = self.http(method, f"/v1/runs/{run_id}{suffix}",
                                   {} if method == "POST" else None, principal="foreign")
            self.check(f"foreign_owner_denied_{suffix or 'status'}", code == 404)
        for name, bad in [
            ("missing_external_identity", {k: v for k, v in request.items()
                                           if not k.startswith("external_")}),
            ("unregistered_suite", dict(request, suite="unknown.review.v1")),
            ("registry_proposal_ceiling", dict(request, budget=dict(request["budget"], experiments=21))),
        ]:
            code, _, _ = self.http("POST", "/v1/runs", bad)
            self.check(name, code == 422)
        code, _, _ = self.http("POST", "/v1/runs", request, principal="local")
        self.check("local_cannot_submit_aos_mapping", code == 422)
        pending = self.request()
        code, handle, _ = self.http("POST", "/v1/runs", pending)
        self.check("queued_stop_fixture_accepted", code == 202)
        self.run_ids.append(handle["run_id"])
        for _ in range(2):
            code, stopped, _ = self.http("POST", f"/v1/runs/{handle['run_id']}/stop", {})
            self.check("queued_stop_idempotent", code == 200 and stopped["stop_requested"])
        self.record["stop_fixture_run_id"] = handle["run_id"]
        return run_id, request

    def capture_units(self) -> None:
        for unit, process, _, _ in self.processes:
            if process.poll() is not None:
                continue
            result = subprocess.run(
                ["/usr/bin/systemctl", "--user", "show", unit,
                 "--property=InvocationID,MainPID,ControlGroup,ActiveState,MemoryMax,MemorySwapMax,"
                 "CPUQuotaPerSecUSec,TasksMax,MemoryCurrent,MemoryPeak,CPUUsageNSec,RuntimeMaxUSec"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
                self.record.setdefault("resource_samples", []).append({
                    "unit": unit, "checked_at": datetime.now(UTC).isoformat(), **fields,
                })

    def execute_run(self, run_id: str) -> None:
        from harness.fingerprint import compute_harness_hash
        from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE

        self.record["harness_sha256"] = compute_harness_hash(ROOT).sha256
        self.record["image"] = DEFAULT_SANDBOX_IMAGE
        process = self.launch("director", ["director", "dispatch-one", "--run-id", run_id],
                              memory="2G", seconds=1860)
        started = time.monotonic()
        deadline = started + 1890
        samples = []
        while process.poll() is None:
            if time.monotonic() > deadline:
                raise TimeoutError("Director observation deadline exceeded")
            health_code, _, health_s = self.http("GET", "/health", principal=None)
            code, observed, status_s = self.http("GET", f"/v1/runs/{run_id}")
            self.check("api_responsive_during_director", health_code == 200 and code == 200)
            samples.append({"elapsed_s": time.monotonic() - started,
                            "state": observed["state"], "health_s": health_s,
                            "status_s": status_s})
            if len(samples) % 10 == 1:
                self.capture_units()
                print(json.dumps({"stage": "director", **samples[-1]}), flush=True)
            time.sleep(2)
        self.record["process_exit_code"] = process.returncode
        self.record["control_samples"] = samples
        active_units = [sample for sample in self.record.get("resource_samples", [])
                        if sample.get("ActiveState") == "active"]
        for purpose, limit in (("api", str(1024**3)), ("director", str(2 * 1024**3))):
            self.check(f"actual_{purpose}_resource_caps", any(
                f"swapp-review-{purpose}-" in item["unit"] and item.get("MemoryMax") == limit
                and item.get("MemorySwapMax") == "0" and item.get("TasksMax") == "128"
                and item.get("CPUQuotaPerSecUSec") == "1s" for item in active_units
            ))
        self.check("director_process_exit_zero", process.returncode == 0)
        status = self.client("status", run_id)
        self.check("terminal_state_completed", status["state"] == "completed")
        report = self.client("report", run_id)
        self.record["report"] = report
        self.check("aos_client_verified_report", report["run_id"] == run_id
                   and report["report_sha256"] == status["report_sha256"])
        self.check("harness_unchanged", compute_harness_hash(ROOT).sha256
                   == self.record["harness_sha256"])

    def inspect_ledger(self, run_id: str, request: dict) -> None:
        from review_scorer_queue import engine
        from sqlalchemy import text

        director = engine("director")
        try:
            with director.connect() as connection:
                row = dict(connection.execute(text("SELECT * FROM lab.runs WHERE run_id=:run"),
                                               {"run": UUID(run_id)}).mappings().one())
                self.check("durable_principal_and_mapping", row["origin"] == "aos"
                           and row["owner_id"] == "aos:lab-service"
                           and all(row[key] == request[key] for key in
                                   ("external_task_id", "external_run_id", "external_action_id")))
                self.check("immutable_requested_budget", row["request_json"]["budget"]
                           == request["budget"] and row["request_json"]["proposal_limit"] == 1)
                self.record["ledger_request"] = row["request_json"]
                counts = dict(connection.execute(text(
                    "SELECT (SELECT count(*) FROM lab.experiments WHERE run_id=:run) AS experiments, "
                    "(SELECT count(*) FROM lab.dev_task_results WHERE run_id=:run) AS scores"
                ), {"run": UUID(run_id)}).mappings().one())
                self.record["ledger_counts"] = counts
                if self.execute:
                    self.check("three_baselines_one_proposal_48_scores", counts == {
                        "experiments": 4, "scores": 48})
                    self.inspect_artifacts(connection, run_id, row)
                else:
                    self.check("post_only_enqueues", counts == {"experiments": 0, "scores": 0})
        finally:
            director.dispose()

    def inspect_artifacts(self, connection, run_id: str, row: dict) -> None:
        from review_director20 import verify_scored_decisions
        from sqlalchemy import text

        from lab.director.artifacts import read_director_artifact, read_registered_calibration
        from lab.director.contracts import ExperimentDocument, TrajectoryDocument
        from lab.director.ledger import canonical_json_bytes

        blob_root = ROOT / "data/runtime/director-artifacts" / run_id
        documents = []
        experiments = connection.execute(text(
            "SELECT * FROM lab.experiments WHERE run_id=:run ORDER BY sequence"
        ), {"run": UUID(run_id)}).mappings().all()
        for experiment in experiments:
            receipt = connection.execute(text("SELECT lab.experiment_record_receipt(:id)"),
                                         {"id": experiment["experiment_id"]}).scalar_one()
            pair = {}
            for name, model in (("experiment", ExperimentDocument), ("trajectory", TrajectoryDocument)):
                payload = read_director_artifact(receipt[f"{name}_blob_sha256"], artifact_root=blob_root)
                assert hashlib.sha256(payload).hexdigest() == receipt[f"{name}_sha256"]
                document = model.model_validate_json(payload, strict=True)
                assert document.run_id == UUID(run_id)
                assert document.experiment_id == experiment["experiment_id"]
                pair[name] = document.model_dump(mode="json", by_alias=True)
            proposal = pair["experiment"]
            candidate = read_director_artifact(proposal["candidate_blob_sha256"], artifact_root=blob_root)
            assert hashlib.sha256(candidate).hexdigest() == proposal["candidate_sha256"]
            for key in ("messages_blob_sha256", "inputs_sha256"):
                digest = pair["trajectory"][key]
                assert hashlib.sha256(read_director_artifact(digest, artifact_root=blob_root)).hexdigest() == digest
            assert pair["trajectory"]["outcome"] == proposal["decision"]
            documents.append(pair)
        scores = connection.execute(text(
            "SELECT * FROM lab.dev_task_results WHERE run_id=:run "
            "ORDER BY experiment_id,evaluation_kind,seed,task_id"
        ), {"run": UUID(run_id)}).mappings().all()
        # Reuse the actual Director receipt reader; its connection is independent.
        calibration = read_registered_calibration(connection.engine, run_id=UUID(run_id), artifact_root=blob_root)
        trace = verify_scored_decisions(documents, scores, calibration, blob_root)
        self.check("physical_terminal_documents_and_decision_parity", len(documents) == 4 and len(trace) == 1)
        self.record["documents"] = documents
        self.record["decision_trace"] = trace
        self.record["calibration"] = calibration.model_dump(mode="json")
        report = self.record["report"]
        self.check("sealed_plan_verified_report_48_scores", row["task_plan_count"] == 48
                   and row["task_plan_sha256"] is not None
                   and len(report["report"]["task_scores"]) == 48
                   and hashlib.sha256(canonical_json_bytes(report["report"])).hexdigest()
                   == row["report_sha256"] == report["report_sha256"])

    def cleanup(self) -> None:
        # Stop only uniquely named review units we created; do not kill by name pattern.
        for unit, process, stdout, stderr in reversed(self.processes):
            if process.poll() is None:
                subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit],
                               capture_output=True, timeout=30, check=False)
                process.wait(timeout=20)
            stdout.close()
            stderr.close()
        from review_scorer_queue import engine
        from sqlalchemy import text

        migrator = engine("migrator")
        try:
            with migrator.begin() as connection:
                for run_id in self.run_ids:
                    sandbox = ROOT / "data/runtime/director-artifacts/sandbox" / run_id
                    if sandbox.exists() and any(sandbox.iterdir()):
                        raise RuntimeError("owned sandbox files remain; preserve run for recovery")
                    live = connection.execute(text(
                        "SELECT count(*) FROM scorer.score_jobs WHERE run_id=:run AND state='running'"
                    ), {"run": UUID(run_id)}).scalar_one()
                    if live:
                        raise RuntimeError("owned run retains active Scorer; cleanup deferred")
                    connection.execute(text("DELETE FROM lab.runs WHERE run_id=:run"),
                                       {"run": UUID(run_id)})
            self.record["owned_run_rows_removed"] = True
        finally:
            migrator.dispose()

    def run(self) -> None:
        self.check("host_disk_reserve", shutil.disk_usage(ROOT).free >= 20 * 1024**3)
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        self.record["host_available_memory_bytes"] = int(memory["MemAvailable"].split()[0]) * 1024
        self.check("host_memory_reserve", self.record["host_available_memory_bytes"] >= 12 * 1024**3)
        try:
            run_id, request = self.controls()
            if self.execute:
                self.execute_run(run_id)
            self.inspect_ledger(run_id, request)
        except Exception as error:
            self.record["error_type"] = type(error).__name__
            # Assertion labels are review constants, never credential-bearing errors.
            if isinstance(error, AssertionError):
                self.record["failed_check"] = str(error)
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.record["cleanup_error_type"] = type(error).__name__
            self.record["source_unchanged"] = source_hashes() == self.record["source_sha256"]
            self.record["all_passed"] = (all(self.record["checks"].values())
                                         and self.record["source_unchanged"]
                                         and "error_type" not in self.record
                                         and "cleanup_error_type" not in self.record)
            self.record["runtime_logs"] = str(self.runtime.relative_to(ROOT))
            name = "api-director-wire-review.json" if self.execute else "api-control-wire-review.json"
            Path(__file__).with_name(name).write_text(json.dumps(self.record, indent=2) + "\n")
            print(json.dumps({key: self.record.get(key) for key in
                              ("all_passed", "checks", "error_type", "failed_check", "runtime_logs")}))
        if not self.record["all_passed"]:
            raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    WireReview(args.config_dir.resolve(strict=True), execute=args.execute).run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "aos-client":
        aos_client(sys.argv[2:])
    else:
        main()
