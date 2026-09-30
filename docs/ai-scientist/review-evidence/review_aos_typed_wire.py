"""Run the isolated typed AOS bridge against the real Lab API and Director.

The AOS store, desktop controller, policy, tool registry and Lab adapter are real.
The desktop runtime and DecisionEngine are explicit fixtures; no real desktop,
model or GPU result is claimed. SQL only inspects the run created by the bridge.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import http.client
import json
import selectors
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "data/runtime/aos-coexistence/source-typed"
PYTHON = ROOT / "data/runtime/aos-coexistence/.venv/bin/python"


class FixtureDesktop:
    """Only the desktop boundary is simulated; ownership lives in real SQLite."""

    pins = {"image_id": "explicit-review-fixture-no-container"}

    def __init__(self):
        self.runtime_id = "review-desktop-" + uuid4().hex
        self.note = ""

    def perform(self, tool, arguments):
        assert arguments == {}
        if tool == "probe":
            return {"display": True, "xfce": True, "note": True, "vnc": "RFB fixture"}
        if tool == "type_note":
            self.note = "AOS desktop input"
            return {}
        if tool == "read_note":
            return {"text": self.note}
        raise AssertionError("unregistered fixture operation")


def child(endpoint: str, token: str, database: str) -> None:
    sys.path.insert(0, str(SOURCE / "src"))
    from aos.contracts import Action, Prediction, Option, digest
    from aos.decision import FixtureDecisionEngine
    from aos.desktop_control import DesktopController
    from aos.lab_external import AOSLabStartRequest, LabApiClient, LabExternalJobCoordinator
    from aos.storage import TrajectoryStore

    store = TrajectoryStore(Path(database))
    controller = DesktopController(store, FixtureDesktop())
    admission = controller.control("take-control")
    # The bridge's fixture engine must remain explicit in its persisted calls.
    coordinator = LabExternalJobCoordinator(
        store.connection, controller, LabApiClient(endpoint, Path(token)),
        allowed_suites=frozenset({"synthetic.api-integration.v1"}),
        decision_engine=FixtureDecisionEngine(),
    )
    request = AOSLabStartRequest(
        lease_id=admission["lease_id"], generation=admission["generation"],
        idempotency_key="typed-review-" + uuid4().hex, suite="synthetic.api-integration.v1",
        experiments=1, wall_seconds=1800, model_tokens=25000,
    )
    job = None
    try:
        for line in sys.stdin:
            message = json.loads(line)
            command = message["command"]
            try:
                if command == "quit":
                    print(json.dumps({"ok": True, "result": "closed"}), flush=True)
                    break
                if command == "start":
                    job = asyncio.run(coordinator.start(controller.session_id, request))
                    row = store.connection.execute(
                        "SELECT request_json FROM aos_external_jobs WHERE job_id=?", (job["job_id"],)
                    ).fetchone()
                    result = {"job": job, "request": json.loads(row["request_json"])}
                elif command == "status":
                    job = asyncio.run(coordinator.status(controller.session_id, job["job_id"]))
                    result = {"run_id": job["lab_run_id"], "state": job["state"],
                              "report_sha256": job["report_sha256"]}
                elif command == "report":
                    result = asyncio.run(coordinator.report(controller.session_id, job["job_id"]))["report"]
                elif command == "foreground":
                    state = controller.control("return-control")
                    input_id = controller.enqueue(state["lease_id"], state["generation"])
                    observed = controller.execute(input_id)
                    result = {"owner": controller.state()["owner"], "input_result": observed,
                              "foreground_tasks": store.connection.execute(
                                  "SELECT count(*) FROM desktop_tasks").fetchone()[0]}
                elif command == "inspect":
                    state = store.state(job["aos_run_id"])
                    records = store.connection.execute(
                        "SELECT a.tool,a.status,a.arguments_json,d.options_json,d.probabilities_json,"
                        "d.selected_option,d.policy_result,m.deployment_id,m.request_json,m.response_json,"
                        "e.envelope_json,e.payload_sha256 FROM actions a "
                        "JOIN decisions d USING(decision_id) JOIN model_calls m USING(call_id) "
                        "JOIN action_envelopes e USING(action_id) WHERE a.run_id=?",
                        (state.run_id,),
                    ).fetchall()
                    for item in records:
                        action = Action.model_validate_json(item["envelope_json"], strict=True)
                        options = [Option.model_validate(value) for value in json.loads(item["options_json"])]
                        prediction = Prediction(selected_option=item["selected_option"],
                                                probabilities=json.loads(item["probabilities_json"]))
                        prediction.validate_options(options)
                        assert len(options) >= 2 and prediction.selected_option == action.selected_option
                        assert item["payload_sha256"] == digest(action.model_dump(mode="json"))
                        assert action.task_id == state.task_id and action.run_id == state.run_id
                        assert item["policy_result"] == "allow" and item["status"] == "ok"
                        identity = json.loads(item["response_json"])["engine"]
                        assert identity["real_model"] is False
                        expected = ("fixture-decision-v1", "deterministic_fixture") if action.tool == "lab.start" else (
                            "lab-control-policy-v1", "host_policy")
                        assert item["deployment_id"] == identity["deployment_id"] == expected[0]
                        assert identity["kind"] == expected[1]
                    row = store.connection.execute(
                        "SELECT status,outcome,training_eligible FROM runs WHERE run_id=?", (state.run_id,)
                    ).fetchone()
                    verifications = [dict(item) for item in store.connection.execute(
                        "SELECT result,expected_json,actual_json FROM verifications WHERE run_id=?",
                        (state.run_id,),
                    )]
                    result = {"state": state.model_dump(mode="json"), "run": dict(row),
                              "action_count": len(records), "tools": sorted({row["tool"] for row in records}),
                              "verifications": verifications, "foreground_owner": controller.state()["owner"]}
                else:
                    raise ValueError("unknown review command")
                print(json.dumps({"ok": True, "result": result}), flush=True)
            except Exception as error:
                # Do not serialize client exceptions that may contain request credentials.
                print(json.dumps({"ok": False, "error_type": type(error).__name__,
                                  "command": command}), flush=True)
    finally:
        store.close()


def hashes() -> dict[str, str]:
    from review_api_wire import source_hashes
    values = source_hashes()
    names = ["src/aos/lab_external.py", "src/aos/contracts.py", "src/aos/computer.py",
             "src/aos/decision.py", "src/aos/storage.py", "src/aos/desktop_control.py"]
    paths = [SOURCE / name for name in names]
    paths.extend(sorted((SOURCE / "database/migrations").glob("*.sql")))
    paths.append(Path(__file__).with_name("review_api_wire.py"))
    values.update({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in paths})
    return values


def parent(config: Path, execute: bool) -> None:
    from review_api_wire import WireReview

    class TypedReview(WireReview):
        def __init__(self):
            super().__init__(config, execute=execute)
            self.bridge = None
            self.job = None
            self.record.update({
                "schema": "aos-typed-wire-review.v1", "source_sha256": hashes(),
                "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "scope": "Actual isolated AOS TrajectoryStore/DesktopController/typed coordinator/policy/"
                         "registry and real HTTP Lab API, PostgreSQL, optional Director/Docker/Scorer. "
                         "Explicit FixtureDecisionEngine and fixture desktop boundary; four synthetic EVT "
                         "tasks and source-only fake Lab provider. No live AOS, real desktop, local model, "
                         "GPU, public data or full coexistence acceptance.",
            })

        def bridge_call(self, command: str) -> dict:
            if self.bridge.poll() is not None:
                raise RuntimeError("isolated AOS bridge exited")
            self.bridge.stdin.write(json.dumps({"command": command}) + "\n")
            self.bridge.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(self.bridge.stdout, selectors.EVENT_READ)
                if not selector.select(15):
                    raise TimeoutError("bounded AOS review command timed out")
            line = self.bridge.stdout.readline(512 * 1024)
            if not line.endswith("\n"):
                raise RuntimeError("AOS review result framing failed")
            result = json.loads(line)
            if not result["ok"]:
                self.record["aos_error"] = result
                raise RuntimeError("typed AOS command failed")
            return result["result"]

        def client(self, mode: str, value: str) -> dict:
            assert self.job and value == self.job["lab_run_id"]
            return self.bridge_call(mode)

        def controls(self):
            server = self.launch("api", ["api", "serve", "--port", str(self.port)],
                                 memory="1G", seconds=1920)
            deadline = time.monotonic() + 20
            while True:
                if server.poll() is not None:
                    raise RuntimeError("Lab API exited before readiness")
                try:
                    code, body, _ = self.http("GET", "/health", principal=None)
                    if code == 200 and body.get("service") == "lab-api":
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError("Lab API readiness deadline exceeded")
                time.sleep(0.1)
            unit = f"swapp-review-aos-typed-{self.review_id}.service"
            command = ["/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
                       f"--unit={unit}", "--service-type=exec", f"--working-directory={ROOT}",
                       "--property=MemoryMax=512M", "--property=MemorySwapMax=0",
                       "--property=CPUQuota=100%", "--property=TasksMax=32",
                       "--property=RuntimeMaxSec=1900", "--property=TimeoutStopSec=10",
                       "--property=KillMode=control-group", "--setenv=OPENBLAS_NUM_THREADS=1",
                       str(PYTHON), str(Path(__file__).resolve()), "child", self.endpoint,
                       str(config / "aos.token"), str(self.runtime / "aos.sqlite")]
            stderr = (self.runtime / "aos.stderr").open("w")
            self.bridge = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=stderr, text=True, bufsize=1)
            self.processes.append((unit, self.bridge, self.bridge.stdout, stderr))
            self.record.setdefault("commands", []).append(command)
            started = self.bridge_call("start")
            self.job = started["job"]
            run_id = self.job["lab_run_id"]
            self.run_ids.append(run_id)
            self.record.update({"run_id": run_id, "aos_job": self.job})
            self.check("typed_aos_start_created_queued_run", self.job["state"] == "queued")
            repeated = self.bridge_call("start")
            self.check("typed_retry_reuses_run_and_action", repeated == started)
            foreground = self.bridge_call("foreground")
            self.record["fixture_foreground"] = foreground
            self.check("desktop_controller_can_return_to_agent", foreground["owner"] == "AGENT"
                       and foreground["input_result"] == {"text": "AOS desktop input"}
                       and foreground["foreground_tasks"] == 0)
            observed = self.bridge_call("status")
            self.check("detached_status_works_during_agent_ownership", observed["run_id"] == run_id
                       and observed["state"] == "queued")
            self.capture_units()
            return run_id, started["request"]

        def capture_units(self):
            super().capture_units()
            if self.bridge is not None and self.job is not None:
                self.record.setdefault("typed_status_samples", []).append({
                    "checked_at": datetime.now(UTC).isoformat(), **self.bridge_call("status")})

        def cleanup(self):
            if self.bridge is not None and self.bridge.poll() is None:
                try:
                    self.bridge_call("quit")
                    self.bridge.stdin.close()
                    self.bridge.wait(timeout=15)
                    self.record["aos_process_exit_code"] = self.bridge.returncode
                except (OSError, RuntimeError, TimeoutError, subprocess.TimeoutExpired):
                    self.record["aos_graceful_shutdown_failed"] = True
            # A start response may be lost before controls() learns the Lab UUID.
            # Recover only mappings from this review's fresh private AOS database.
            # Wait for the owned bridge to exit before reading its durable state.
            if self.bridge is not None and self.bridge.poll() is None:
                unit = next(item[0] for item in self.processes if item[1] is self.bridge)
                subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit],
                               capture_output=True, timeout=30, check=False)
                self.bridge.wait(timeout=15)
            database = self.runtime / "aos.sqlite"
            if database.exists():
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='aos_external_jobs'").fetchone()
                    mappings = connection.execute("SELECT run_id,action_id FROM aos_external_jobs").fetchall() if exists else []
                if mappings:
                    from review_scorer_queue import engine
                    from sqlalchemy import text

                    reader = engine("director")
                    try:
                        with reader.connect() as connection:
                            for aos_run, aos_action in mappings:
                                ids = connection.execute(text(
                                    "SELECT run_id FROM lab.runs WHERE origin='aos' AND owner_id='aos:lab-service' "
                                    "AND external_run_id=:run AND external_action_id=:action"
                                ), {"run": aos_run, "action": aos_action}).scalars().all()
                                for run_id in ids:
                                    if str(run_id) not in self.run_ids:
                                        self.run_ids.append(str(run_id))
                    finally:
                        reader.dispose()
            super().cleanup()

        def run(self):
            try:
                self.check("host_disk_reserve", shutil.disk_usage(ROOT).free >= 20 * 1024**3)
                mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
                self.check("host_memory_reserve", int(mem["MemAvailable"].split()[0]) * 1024 >= 12 * 1024**3)
                run_id, request = self.controls()
                if execute:
                    self.execute_run(run_id)
                self.inspect_ledger(run_id, request)
                audit = self.bridge_call("inspect")
                self.record["aos_typed_audit"] = audit
                self.check("actual_ai_scientist_state_and_finite_decisions", audit["state"]["task_kind"]
                           == "ai_scientist" and audit["action_count"] >= 2
                           and {"lab.start", "lab.status"}.issubset(audit["tools"]))
                if execute:
                    self.check("aos_completed_only_with_matching_verification", audit["run"]["status"] == "succeeded"
                               and audit["state"]["phase"] == "SUCCEEDED" and bool(audit["verifications"])
                               and all(item["result"] == "passed" and item["expected_json"] == item["actual_json"]
                                       for item in audit["verifications"]) and "lab.report" in audit["tools"])
                self.check("fixture_not_training_eligible", audit["run"]["training_eligible"] == 0)
                active = [item for item in self.record["resource_samples"] if item.get("ActiveState") == "active"]
                self.check("aos_actual_resource_caps", any("swapp-review-aos-typed-" in item["unit"]
                           and item["MemoryMax"] == str(512 * 1024**2) and item["MemorySwapMax"] == "0"
                           and item["CPUQuotaPerSecUSec"] == "1s" and item["TasksMax"] == "32" for item in active))
            except Exception as error:
                self.record["error_type"] = type(error).__name__
                if isinstance(error, AssertionError):
                    self.record["failed_check"] = str(error)
            finally:
                try:
                    self.cleanup()
                except Exception as error:
                    self.record["cleanup_error_type"] = type(error).__name__
                self.record["source_unchanged"] = hashes() == self.record["source_sha256"]
                self.record["all_passed"] = (all(self.record["checks"].values()) and self.record["source_unchanged"]
                                             and not any(key in self.record for key in
                                                         ("error_type", "cleanup_error_type", "aos_graceful_shutdown_failed")))
                self.record["runtime_logs"] = str(self.runtime.relative_to(ROOT))
                name = "aos-typed-director-review.json" if execute else "aos-typed-control-review.json"
                if not self.record["all_passed"]:
                    name = name.replace("-review.json", "-before.json")
                Path(__file__).with_name(name).write_text(json.dumps(self.record, indent=2) + "\n")
                print(json.dumps({key: self.record.get(key) for key in
                                  ("all_passed", "checks", "error_type", "failed_check", "aos_error", "runtime_logs")}))
            if not self.record["all_passed"]:
                raise SystemExit(1)

    TypedReview().run()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        child(*sys.argv[2:])
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--config-dir", type=Path, required=True)
        parser.add_argument("--execute", action="store_true")
        arguments = parser.parse_args()
        parent(arguments.config_dir.resolve(strict=True), arguments.execute)
