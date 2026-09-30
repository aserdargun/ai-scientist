"""Run six real local-Qwen proposals through the production queued Director.

The suite is explicitly synthetic. Model providers, experiments, guards, scoring
and the report must come from the production implementation. This driver records
failures and keeps the actual run ledger for later replay inspection.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from uuid import UUID, uuid4

from review_gpu_host import GIB, ROOT, snapshot
from review_local_qwen_observer import QwenReviewObserver


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes() -> dict[str, str]:
    files = [path for name in ("lab", "harness", "vendor") for path in (ROOT / name).rglob("*.py")]
    files.extend(ROOT / name for name in (
        "harness/VERSION", "ops/sandbox-image.lock", "pyproject.toml", "uv.lock",
        "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock",
        "docs/ai-scientist/review-evidence/qwen-model-source.json",
        "docs/ai-scientist/review-evidence/review_gpu_host.py",
        "docs/ai-scientist/review-evidence/review_local_qwen_observer.py",
        "docs/ai-scientist/review-evidence/review_local_qwen_ledger.py",
        "docs/ai-scientist/review-evidence/review_director20.py",
        "docs/ai-scientist/review-evidence/review_native_doctor.py",
        "docs/ai-scientist/review-evidence/review_local_qwen_wire.py",
    ))
    return {str(path.relative_to(ROOT)): sha(path) for path in sorted(files)}


class LocalQwenWireReview:
    def __init__(self, config: Path, output: Path, wall_seconds: int):
        from lab.api.registry import load_principals, load_suite_registry
        from lab.director.local_llm import provider_config_sha256

        if output.exists():
            raise ValueError("refusing to overwrite evidence")
        if not 1800 <= wall_seconds <= 12_000:
            raise ValueError("review wall limit must be between 1800 and 12000 seconds")
        if config.is_symlink() or config.stat().st_mode & 0o077:
            raise ValueError("review config directory must be private")
        registry = load_suite_registry(config / "suites.json", ROOT / "data/runtime")
        if set(registry.entries) != {"synthetic.local-qwen-smoke.v1"}:
            raise ValueError("review registry must contain only its prepared synthetic suite")
        self.entry = registry.get("synthetic.local-qwen-smoke.v1")
        if self.entry.provider != "local-qwen" or self.entry.proposal_limit < 6:
            raise ValueError("review requires a real local provider and at least six proposals")
        if self.entry.provider_config_sha256 != provider_config_sha256():
            raise ValueError("review registry provider configuration differs from the current worker")
        principals = load_principals(config / "principals.json")
        if len(principals) != 1 or principals[0].origin != "local":
            raise ValueError("this review requires one explicit local API principal")
        self.principal = principals[0]
        self.config, self.output, self.wall_seconds = config, output, wall_seconds
        self.review_id = uuid4().hex
        self.parent = f"swapp-lab-gpu-review-{self.review_id}.service"
        self.parent_description = f"SWAPP local Qwen research review {self.review_id}"
        self.api_unit = f"swapp-review-qwen-api-{self.review_id}.service"
        self.api_description = f"SWAPP local Qwen review API {self.review_id}"
        self.runtime = config / self.review_id
        self.runtime.mkdir(mode=0o700)
        gpu_root = ROOT / "data/runtime/gpu"
        if gpu_root.is_symlink() or not gpu_root.is_dir():
            raise ValueError("review requires the fixed private GPU runtime directory")
        gpu_stat = gpu_root.stat()
        if gpu_stat.st_uid != os.getuid() or gpu_stat.st_mode & 0o077:
            raise ValueError("GPU runtime directory must be private to the service owner")
        self.database = gpu_root / f"local-qwen-review-{self.review_id}.sqlite3"
        if self.database.exists() or self.database.is_symlink():
            raise ValueError("review GPU database must be new")
        self.observer = QwenReviewObserver(self.database, self.parent, self.parent_description)
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs = []
        self.run_id: str | None = None
        self.api_invocation: str | None = None
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.record = {
            "schema": "local-qwen-wire-review.v1", "checked_at": datetime.now(UTC).isoformat(),
            "scope": "Real local Qwen proposals through Lab HTTP/queued Director/Docker/Scorer; "
                     "four synthetic EVT tasks. No AOS model/coexistence, public-data suite or training acceptance.",
            "review_id": self.review_id, "runtime_directory": str(self.runtime.relative_to(ROOT)),
            "runtime_database": str(self.database.relative_to(ROOT)),
            "checks": {}, "samples": [], "controls": [], "source_sha256": source_hashes(),
            "config_sha256": {name: sha(config / name) for name in ("suites.json", "principals.json")},
            "minimum_required_proposals": 6, "minimum_required_S1": 2, "minimum_required_S2": 2,
        }

    def check(self, name: str, condition: bool) -> None:
        self.record["checks"][name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    def http(self, method: str, path: str, payload=None, *, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.principal.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        started = time.monotonic()
        try:
            connection.request(method, path, body=None if payload is None else json.dumps(payload), headers=headers)
            response = connection.getresponse()
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise RuntimeError("HTTP result exceeds the bounded report size")
            return response.status, json.loads(raw), time.monotonic() - started
        finally:
            connection.close()

    def launch(self, purpose: str):
        director = purpose == "director"
        unit = self.parent if director else self.api_unit
        description = self.parent_description if director else self.api_description
        arguments = (["director", "dispatch-one", "--run-id", self.run_id] if director
                     else ["api", "serve", "--port", str(self.port)])
        command = [
            "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
            f"--unit={unit}", f"--description={description}", "--service-type=exec",
            f"--working-directory={ROOT}", f"--property=MemoryMax={'2G' if director else '1G'}",
            "--property=MemorySwapMax=0", "--property=CPUQuota=100%", "--property=TasksMax=128",
            f"--property=RuntimeMaxSec={self.wall_seconds + 120}", "--property=TimeoutStopSec=15",
            "--property=KillMode=control-group", "--property=UMask=0077", "--property=LimitFSIZE=33554432",
            "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1", "--setenv=MKL_NUM_THREADS=1",
            f"--setenv=LAB_API_PRINCIPALS_FILE={self.config / 'principals.json'}",
            f"--setenv=LAB_SUITE_REGISTRY_FILE={self.config / 'suites.json'}",
        ]
        if director:
            command.extend([
                "--slice=swapp-gpu.slice", f"--setenv=SWAPP_LAB_GPU_UNIT={self.parent}",
                f"--setenv=SWAPP_AOS_GPU_UNIT=swapp-aos-gpu-review-{self.review_id}.service",
                f"--setenv=SWAPP_GPU_RUNTIME_DB={self.database}",
            ])
        command.extend([str(ROOT / ".venv/bin/python"), "-m", "lab.cli", *arguments])
        stdout = (self.runtime / f"{purpose}.stdout").open("xb")
        stderr = (self.runtime / f"{purpose}.stderr").open("xb")
        self.logs.extend((stdout, stderr))
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
        self.processes[purpose] = process
        self.record.setdefault("commands", []).append(command)
        return process

    def api_state(self) -> dict[str, str]:
        result = subprocess.run([
            "/usr/bin/systemctl", "--user", "show", self.api_unit, "--no-pager",
            "--property=LoadState,ActiveState,MainPID,InvocationID,Description,ControlGroup,MemoryPeak",
        ], capture_output=True, text=True, timeout=5, check=False)
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if int(state.get("MainPID", "0")):
            if state.get("Description") != self.api_description:
                raise RuntimeError("API review identity mismatch")
            if self.api_invocation is not None and state.get("InvocationID") != self.api_invocation:
                raise RuntimeError("API review generation changed")
            self.api_invocation = state["InvocationID"]
        return state

    def execute(self) -> None:
        from harness.fingerprint import compute_harness_hash
        from review_native_doctor import runtime_packages
        from lab.llm.native_runtime import SystemdUnitManager
        from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE

        preflight = snapshot()
        self.record["preflight"] = preflight
        self.check("fresh_host_capacity", preflight["memory_available_bytes"] >= 18 * GIB
                   and preflight["disk_available_bytes"] >= 20 * GIB
                   and preflight["gpu"]["temperature_c"] < 83)
        self.check("no_foreign_gpu_at_start", all(p["existing_display_exemption"]
                                                  for p in preflight["gpu_consumers"]))
        self.record["harness_sha256"] = compute_harness_hash(ROOT).sha256
        self.record["image"] = DEFAULT_SANDBOX_IMAGE
        self.record["runtime_packages_before"] = runtime_packages()
        lock = ROOT / "docs/ai-scientist/review-evidence/vllm-cuda132-resolved.lock"
        locked = {name.lower().replace("_", "-"): version for name, version in
                  re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", lock.read_text(), re.M)}
        self.check("pinned_runtime_package_versions", self.record["runtime_packages_before"] == locked)
        SystemdUnitManager().ensure_slice()
        api = self.launch("api")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if api.poll() is not None:
                raise RuntimeError("API exited before readiness")
            self.api_state()
            try:
                code, body, _ = self.http("GET", "/health", authenticated=False)
                if code == 200 and body.get("service") == "lab-api":
                    break
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        else:
            raise TimeoutError("API readiness deadline exceeded")
        request = {
            "idempotency_key": "local-qwen-review-" + self.review_id,
            "track": self.entry.track, "suite": self.entry.suite_id,
            "program_version": self.entry.program_version,
            "budget": {"experiments": 6, "wall_seconds": self.wall_seconds, "model_tokens": 350_000},
        }
        code, accepted, _ = self.http("POST", "/v1/runs", request)
        self.check("api_accepted_queued_run", code == 202 and accepted.get("state") == "queued")
        self.run_id = str(UUID(accepted["run_id"]))
        # Recovery ownership accepts only the fixed drain or this exact run-bound
        # service. Bind the model principal and observer before launching it.
        self.parent = f"swapp-ai-scientist-director-dispatch-{UUID(self.run_id).hex}.service"
        self.observer = QwenReviewObserver(self.database, self.parent, self.parent_description)
        self.record.update({"run_id": self.run_id, "request": request, "accepted": accepted})
        process = self.launch("director")
        started = time.monotonic()
        while process.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed > self.wall_seconds + 135:
                raise TimeoutError("bounded Director observation deadline exceeded")
            observation = self.observer.observe()
            observation["elapsed_seconds"] = elapsed
            self.record["samples"].append(observation)
            self.check("host_reserves_maintained", observation["reserves_maintained"])
            self.check("no_foreign_gpu_during_run", not observation["foreign_gpu_consumers"])
            code, status, latency = self.http("GET", f"/v1/runs/{self.run_id}")
            health, _, health_latency = self.http("GET", "/health", authenticated=False)
            self.check("api_responsive_during_models", code == 200 and health == 200)
            sample = {"elapsed_seconds": elapsed, "state": status["state"],
                      "status_seconds": latency, "health_seconds": health_latency}
            self.record["controls"].append(sample)
            if len(self.record["controls"]) % 30 == 1:
                print(json.dumps({"stage": "actual_director", **sample}), flush=True)
            time.sleep(1)
        self.record["director_exit_code"] = process.wait(timeout=2)
        self.record["samples"].append(self.observer.observe())
        self.check("director_exit_zero", self.record["director_exit_code"] == 0)
        code, terminal, _ = self.http("GET", f"/v1/runs/{self.run_id}")
        self.record["terminal"] = terminal
        self.check("run_completed", code == 200 and terminal["state"] == "completed")
        code, report, _ = self.http("GET", f"/v1/runs/{self.run_id}/report")
        self.record["report"] = report
        self.check("report_available", code == 200 and report.get("report_sha256") == terminal["report_sha256"])
        from review_local_qwen_ledger import verify_run

        self.record["ledger_verification"] = verify_run(
            self.run_id, observed_models=self.observer.models, report=report,
            owner_id=self.principal.owner_id, request=request,
        )
        self.check("independent_ledger_verification_passed",
                   all(self.record["ledger_verification"]["checks"].values()))

    def cleanup_execution_children(self) -> None:
        """Use existing fenced recovery only for this run's execution children."""
        from sqlalchemy import text
        from review_scorer_queue import engine
        from lab.scorer.supervisor import stop_owned_scorer_unit
        from lab.sandbox.docker_runner import DEFAULT_ADMISSION_LOCK, DEFAULT_SANDBOX_IMAGE, LocalDockerRunner

        if self.run_id is None:
            return
        scorer = engine("scorer")
        try:
            with scorer.connect() as connection:
                jobs = connection.execute(text(
                    "SELECT job_id,claim_invocation_id FROM scorer.score_jobs "
                    "WHERE run_id=:run AND state='running'"
                ), {"run": UUID(self.run_id)}).mappings().all()
            self.record["scorer_cleanup"] = []
            for job in jobs:
                if job["claim_invocation_id"] is None:
                    raise RuntimeError("active Scorer has no durable generation; preserve for recovery")
                drained = stop_owned_scorer_unit(job["job_id"], invocation_id=job["claim_invocation_id"])
                self.record["scorer_cleanup"].append({"job_id": str(job["job_id"]), "drained": drained})
                if not drained:
                    raise RuntimeError("owned Scorer generation did not drain")
        finally:
            scorer.dispose()
        marker = DEFAULT_ADMISSION_LOCK.with_suffix(".intent")
        if not marker.exists():
            self.record["sandbox_cleanup"] = "no_inflight_intent"
            return
        descriptor = os.open(DEFAULT_ADMISSION_LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not marker.exists():
                self.record["sandbox_cleanup"] = "no_inflight_intent"
                return
            intent = json.loads(marker.read_bytes())
            work_root = ROOT / "data/runtime/director-artifacts/sandbox" / self.run_id
            parent = self.observer.parent_identity
            if (parent is None or intent.get("owner_pid") != parent[1]
                    or intent.get("owner_start") != str(parent[3]) or intent.get("boot_id") != parent[2]
                    or intent.get("work_root") != str(work_root)):
                self.record["sandbox_cleanup"] = "different_owner_intent_untouched"
                return
            runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=work_root)
            runner._reconcile_owned_containers()
            self.record["sandbox_cleanup"] = "owned_orphan_reconciled"
        finally:
            os.close(descriptor)

    def cleanup(self) -> None:
        if self.run_id and self.processes.get("director") and self.processes["director"].poll() is None:
            try:
                code, _, _ = self.http("POST", f"/v1/runs/{self.run_id}/stop", {})
                self.record["cleanup_stop_http_status"] = code
            except (OSError, http.client.HTTPException):
                self.record["cleanup_stop_http_unavailable"] = True
        try:
            if "director" in self.processes:
                self.observer.stop_owned_parent()
                self.processes["director"].wait(timeout=20)
                try:
                    self.observer.stop_owned_models()
                finally:
                    self.cleanup_execution_children()
                self.record["model_cleanup"] = self.observer.cleanup
                final = self.observer.observe()
                self.record["postflight"] = final
                self.record["owned_model_generations"] = list(self.observer.models.values())
                self.check("owned_model_cgroups_drained", all(
                    not entry["state"].get("cgroup") or entry["state"]["cgroup"].get("pids.current") == "0"
                    for entry in final["models"]
                ))
                groups = {entry["identity"][4] for entry in self.observer.models.values() if entry["identity"]}
                self.check("owned_gpu_contexts_drained", all(
                    item.get("cgroup") not in groups for item in final["gpu_consumers"]
                ))
        finally:
            try:
                if "api" in self.processes:
                    current = self.api_state()
                    if int(current.get("MainPID", "0")):
                        result = subprocess.run(["/usr/bin/systemctl", "--user", "stop", self.api_unit],
                                                capture_output=True, timeout=20, check=False)
                        self.record["api_stop_exit_code"] = result.returncode
                    self.record["api_exit_code"] = self.processes["api"].wait(timeout=20)
            finally:
                for log in self.logs:
                    log.close()
                self.record["ledger_retained_for_replay"] = self.run_id is not None

    def run(self) -> int:
        try:
            self.execute()
        except Exception as error:
            self.record["error_type"] = type(error).__name__
            if isinstance(error, AssertionError):
                self.record["failed_check"] = str(error)
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.record["cleanup_error_type"] = type(error).__name__
            self.record["sources_unchanged"] = source_hashes() == self.record["source_sha256"]
            self.record["config_unchanged"] = all(sha(self.config / name) == value
                for name, value in self.record["config_sha256"].items())
            if "runtime_packages_before" in self.record:
                from review_native_doctor import runtime_packages
                self.record["runtime_packages_after"] = runtime_packages()
                self.record["checks"]["runtime_package_versions_unchanged"] = (
                    self.record["runtime_packages_before"] == self.record["runtime_packages_after"]
                )
            self.record["execution_observed"] = self.run_id is not None
            self.record["all_passed"] = (
                self.record["checks"].get("independent_ledger_verification_passed") is True
                and all(self.record["checks"].values())
                and self.record["sources_unchanged"] and self.record["config_unchanged"]
                and "error_type" not in self.record and "cleanup_error_type" not in self.record
            )
            with self.output.open("x") as stream:
                json.dump(self.record, stream, indent=2, allow_nan=False)
                stream.write("\n")
        print(json.dumps({key: self.record.get(key) for key in
              ("run_id", "all_passed", "execution_observed", "error_type", "failed_check", "cleanup_error_type")}))
        return 0 if self.record["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--wall-seconds", type=int, default=6000)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    raise SystemExit(LocalQwenWireReview(args.config_dir.resolve(strict=True), args.output, args.wall_seconds).run())
