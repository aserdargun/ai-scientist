#!/usr/bin/env python3
"""Drive an isolated real AOS + Lab coexistence acceptance run.

Default mode is preflight only. `--execute` is an explicit root-operated gate;
it starts only the named isolated AOS/Lab run-bound units supplied here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIVE_AOS_ROOT = Path("/home/cachyos/aos").resolve()
MAX_RUNTIME_SECONDS = 14_400
DIRECTOR_OVERHEAD_SECONDS = 600
UNIT_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "Result",
    "MainPID",
    "InvocationID",
    "ControlGroup",
    "MemoryCurrent",
    "MemoryMax",
    "CPUUsageNSec",
    "CPUQuotaPerSecUSec",
    "TasksCurrent",
    "TasksMax",
)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _private_file(path: Path, name: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{name} must be an existing non-symlink file")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f"{name} must be private to the current owner")
    return path.resolve(strict=True)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _show(unit: str) -> dict[str, str]:
    if re.fullmatch(r"[a-zA-Z0-9_.@:-]{1,255}", unit) is None:
        raise ValueError("invalid systemd unit identity")
    completed = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            unit,
            *[f"--property={name}" for name in UNIT_PROPERTIES],
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0:
        return {"LoadState": "not-found"}
    result = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    return {key: result.get(key, "") for key in UNIT_PROPERTIES}


def _host_sample() -> dict[str, object]:
    memory: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        key, _, value = line.partition(":")
        fields = value.strip().split()
        if fields and fields[0].isdigit():
            memory[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    gpu: list[dict[str, int]] = []
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        result = subprocess.run(
            [
                nvidia,
                "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                try:
                    used, total, utilization = (int(value.strip()) for value in line.split(","))
                except (ValueError, TypeError):
                    continue
                gpu.append(
                    {
                        "memory_used_mib": used,
                        "memory_total_mib": total,
                        "utilization_percent": utilization,
                    }
                )
    return {
        "sampled_at": time.time(),
        "mem_available_bytes": memory.get("MemAvailable"),
        "mem_total_bytes": memory.get("MemTotal"),
        "gpu": gpu,
    }


def _gpu_ledger(path: Path, since: float) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        return {"available": False, "reason": "runtime_db_not_created"}
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            db.row_factory = sqlite3.Row
            names = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "gpu_turn_requests" not in names or "gpu_turn_state" not in names:
                return {"available": False, "reason": "expected_broker_ledger_absent"}
            rows = db.execute(
                "SELECT owner,sequence,state,submitted_at,owner_unit FROM gpu_turn_requests "
                "WHERE submitted_at>=? ORDER BY sequence",
                (since,),
            ).fetchall()
            state = db.execute(
                "SELECT active_owner,phase,next_owner FROM gpu_turn_state WHERE singleton=1"
            ).fetchone()
            return {
                "available": True,
                "turns": [
                    {
                        "owner": row["owner"],
                        "sequence": row["sequence"],
                        "state": row["state"],
                        "submitted_at": row["submitted_at"],
                        "unit": row["owner_unit"],
                    }
                    for row in rows
                ],
                "active_owner": state["active_owner"] if state else None,
                "phase": state["phase"] if state else None,
                "next_owner": state["next_owner"] if state else None,
            }
    except sqlite3.Error as exc:
        return {"available": False, "reason": type(exc).__name__}


def _suite_metadata(args: argparse.Namespace) -> dict[str, object]:
    code = r"""import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from lab.api.registry import load_suite_registry
registry=load_suite_registry(Path(sys.argv[2]),Path(sys.argv[3]))
entry=registry.get(sys.argv[4]); manifest,_=registry.verify_entry(entry)
print(json.dumps({"suite_id":entry.suite_id,"provider":entry.provider,"track":entry.track,
"program_version":entry.program_version,"proposal_limit":entry.proposal_limit,
"suite_manifest_sha256":entry.suite_manifest_sha256,
"registry_entry_sha256":registry.entry_sha256(entry),
"manifest_path":str(manifest)}))"""
    env = {**os.environ, "PYTHONPATH": str(args.lab_root)}
    result = subprocess.run(
        [
            str(args.lab_python),
            "-c",
            code,
            str(args.lab_root),
            str(args.suite_registry),
            str(args.suite_runtime_root),
            args.suite,
        ],
        cwd=args.lab_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("trusted Lab suite registry verification failed")
    try:
        metadata = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValueError("trusted Lab suite registry returned no metadata") from exc
    if metadata.get("provider") != "local-qwen":
        raise ValueError("acceptance requires a registered local-qwen suite")
    if metadata.get("track") != "anomaly" or metadata.get("program_version") != "director.v1":
        raise ValueError("registered suite contract differs from the AOS Lab adapter")
    if args.experiments > metadata["proposal_limit"]:
        raise ValueError("requested experiment budget exceeds trusted suite registration")
    return metadata


class Console:
    def __init__(self, origin: str, token: str, timeout: float = 30.0) -> None:
        self.origin = origin.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self._request("POST", "/api/login", {"token": token}, authenticated=False)

    def _request(
        self, method: str, path: str, value: object | None = None, *, authenticated: bool = True
    ) -> tuple[dict[str, Any], float]:
        body = None if value is None else json.dumps(value, separators=(",", ":")).encode()
        headers = {"Origin": self.origin, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + path, data=body, headers=headers, method=method)
        started = time.monotonic()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
                if not isinstance(result, dict):
                    raise ValueError("AOS API returned a non-object response")
                return result, round((time.monotonic() - started) * 1000, 3)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            status = getattr(exc, "code", None)
            raise RuntimeError(
                f"AOS API {method} {path} failed ({status or type(exc).__name__})"
            ) from None

    def get(self, path: str) -> tuple[dict[str, Any], float]:
        return self._request("GET", path)

    def post(self, path: str, value: object) -> tuple[dict[str, Any], float]:
        return self._request("POST", path, value)


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _fresh_console_token(source: Path, before: set[Path], deadline: float) -> tuple[Path, str]:
    runs = source / "runs"
    while time.monotonic() < deadline:
        candidates = {path for path in runs.glob("desktop-console-*.token") if path not in before}
        if len(candidates) == 1:
            token_path = candidates.pop()
            _private_file(token_path, "AOS console token")
            return token_path, token_path.read_text(encoding="utf-8").strip()
        if len(candidates) > 1:
            raise RuntimeError("ambiguous fresh AOS console token files")
        time.sleep(0.2)
    raise TimeoutError("isolated AOS console did not create its private API token")


class CoexistenceRun:
    def __init__(self, args: argparse.Namespace, output: Path, suite: dict[str, object]) -> None:
        self.args, self.output, self.suite = args, output, suite
        self.run_key = uuid4().hex
        self.port = _unused_port()
        self.unit = args.aos_unit
        self.base = output / self.run_key
        self.base.mkdir(mode=0o700, parents=True)
        self.workspace = self.base / "desktop-workspace"
        self.workspace.mkdir(mode=0o700)
        self.database = self.base / "desktop-console.sqlite"
        self.token_before = set((args.aos_source / "runs").glob("desktop-console-*.token"))
        self.console: Console | None = None
        self.token_path: Path | None = None
        self.dispatch_unit: str | None = None
        self.dispatch_wrapper: subprocess.Popen | None = None
        self.samples: list[dict[str, object]] = []
        self.api_ms: dict[str, list[float]] = {}
        self.phase_receipts: list[dict[str, object]] = []
        self.outcome = "running"
        self.failure_type: str | None = None

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _api_get(self, key: str, path: str) -> dict[str, Any]:
        assert self.console is not None
        value, latency = self.console.get(path)
        self.api_ms.setdefault(key, []).append(latency)
        return value

    def _api_post(self, key: str, path: str, value: object) -> dict[str, Any]:
        assert self.console is not None
        result, latency = self.console.post(path, value)
        self.api_ms.setdefault(key, []).append(latency)
        return result

    def _launch_aos(self) -> None:
        existing = _show(self.unit)
        if existing.get("ActiveState") == "active":
            raise RuntimeError("configured AOS owner unit is already active")
        run_command = [
            "/usr/bin/systemd-run",
            "--user",
            "--collect",
            "--quiet",
            f"--unit={self.unit}",
            "--slice=swapp-gpu.slice",
            "-p",
            "MemoryMax=2G",
            "-p",
            "MemorySwapMax=0",
            "-p",
            "CPUQuota=100%",
            "-p",
            "TasksMax=128",
            "-p",
            f"RuntimeMaxSec={MAX_RUNTIME_SECONDS}s",
            f"--working-directory={self.args.aos_source}",
            f"--setenv=PYTHONPATH={self.args.aos_source / 'src'}",
            f"--setenv=SWAPP_AOS_GPU_UNIT={self.unit}",
            "--setenv=OPENBLAS_NUM_THREADS=1",
            "--setenv=OMP_NUM_THREADS=1",
            "--setenv=MKL_NUM_THREADS=1",
            str(self.args.aos_python),
            str(self.args.aos_source / "scripts/serve_desktop.py"),
            "--port",
            str(self.port),
            "--workspace",
            str(self.workspace),
            "--database",
            str(self.database),
            "--trajectory-database",
            str(self.database),
            "--desktop-manifest",
            str(self.args.desktop_manifest),
            "--engine",
            "decider",
            "--reuse-decider",
            "--shared-gpu-turns",
            "--decider-manifest",
            str(self.args.decider_manifest),
            "--model-python",
            str(self.args.model_python),
            "--lab-external-api-url",
            self.args.lab_api_url,
            "--lab-external-token-file",
            str(self.args.lab_token_file),
            "--lab-external-suite",
            self.args.suite,
        ]
        result = subprocess.run(
            run_command, capture_output=True, text=True, timeout=15, check=False
        )
        if result.returncode != 0:
            raise RuntimeError("isolated AOS systemd unit could not be launched")
        token_path, token = _fresh_console_token(
            self.args.aos_source, self.token_before, time.monotonic() + self.args.startup_seconds
        )
        self.token_path = token_path
        self.console = Console(self.origin, token)
        deadline = time.monotonic() + self.args.startup_seconds
        while time.monotonic() < deadline:
            state = self._api_get("state", "/api/state")
            if state.get("runtime", {}).get("status") == "running":
                self.phase_receipts.append(
                    {
                        "phase": "aos_bootstrap",
                        "result": "running",
                        "session_id": state["control"]["session_id"],
                        "runtime_id": state["control"]["runtime_id"],
                    }
                )
                return
            time.sleep(0.25)
        raise TimeoutError("isolated desktop runtime did not become ready")

    def _stop_aos(self) -> None:
        if self.console is not None:
            try:
                self._api_post("control_stop", "/api/control", {"command": "stop"})
            except RuntimeError:
                pass
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "stop", self.unit],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and _show(self.unit).get("ActiveState") == "active":
            time.sleep(0.2)
        self.console = None
        self.token_path = None

    def _take_human(self) -> dict[str, Any]:
        return self._api_post("control_take_human", "/api/control", {"command": "take-control"})

    def _start_lab(self) -> dict[str, Any]:
        state = self._api_get("state_for_lab_start", "/api/state")["control"]
        if state.get("owner") != "HUMAN":
            state = self._take_human()
        request = {
            "lease_id": state["lease_id"],
            "generation": state["generation"],
            "idempotency_key": "aos-coexistence-" + uuid4().hex,
            "suite": self.args.suite,
            "experiments": self.args.experiments,
            "wall_seconds": self.args.wall_seconds,
            "model_tokens": self.args.model_tokens,
        }
        started = self._api_post("lab_start", "/api/lab/start", request)
        if not started.get("lab_run_id") or not started.get("aos_action_id"):
            raise RuntimeError("AOS typed Lab start returned no durable run/action identity")
        return started

    def _launch_dispatch(self, run_id: str) -> str:
        normalized = run_id.replace("-", "")
        if re.fullmatch(r"[0-9a-f]{32}", normalized) is None:
            raise ValueError("AOS returned a malformed Lab run UUID")
        unit = f"swapp-ai-scientist-director-dispatch-{normalized}.service"
        self.dispatch_unit = unit
        environment = {
            "LAB_DIRECTOR_DSN_FILE": str(self.args.director_dsn_file),
            "LAB_PLANNER_DSN_FILE": str(self.args.planner_dsn_file),
            "LAB_SUITE_REGISTRY_FILE": str(self.args.suite_registry),
            "SWAPP_AOS_GPU_UNIT": self.args.aos_unit,
            "SWAPP_LAB_GPU_UNIT": unit,
            "SWAPP_GPU_RUNTIME_DB": str(self.args.gpu_runtime_db),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        command = [
            "/usr/bin/systemd-run",
            "--user",
            "--collect",
            "--wait",
            "--pipe",
            "--slice=swapp-gpu.slice",
            f"--unit={unit}",
            "-p",
            "MemoryMax=2G",
            "-p",
            "MemorySwapMax=0",
            "-p",
            "CPUQuota=100%",
            "-p",
            "TasksMax=128",
            "-p",
            f"RuntimeMaxSec={self.args.wall_seconds + DIRECTOR_OVERHEAD_SECONDS}s",
            f"--working-directory={self.args.lab_root}",
        ]
        command.extend(f"--setenv={name}={value}" for name, value in environment.items())
        command.extend(
            [
                str(self.args.lab_python),
                "-m",
                "lab.cli",
                "director",
                "dispatch-one",
                "--run-id",
                run_id,
            ]
        )
        log_path = self.base / f"{normalized}-director-dispatch.log"
        log = log_path.open("xb")
        os.chmod(log_path, 0o600)
        self.dispatch_wrapper = subprocess.Popen(
            command, cwd=self.args.lab_root, stdout=log, stderr=subprocess.STDOUT, close_fds=True
        )
        log.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            current = _show(unit)
            if current.get("ActiveState") == "active":
                self.phase_receipts.append(
                    {
                        "phase": "lab_dispatch",
                        "unit": unit,
                        "invocation_id": current.get("InvocationID"),
                        "main_pid": current.get("MainPID"),
                    }
                )
                return unit
            if self.dispatch_wrapper.poll() is not None:
                raise RuntimeError("run-bound Lab dispatcher exited before becoming active")
            time.sleep(0.1)
        raise TimeoutError("run-bound Lab dispatcher did not become active")

    def _poll_status(self, job_id: str) -> dict[str, Any]:
        return self._api_get("lab_status", f"/api/lab/jobs/{job_id}")

    def _poll_foreground(self, foreground_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        tasks = self._api_get("foreground_status", "/api/tasks")
        jobs = tasks.get("jobs", [])
        match = next((job for job in jobs if job.get("job_id") == foreground_id), None)
        if match is not None:
            return match, tasks
        return None, tasks

    def _sample_once(self, foreground_id: str, lab_job_id: str, since: float) -> tuple[bool, bool]:
        state = self._poll_status(lab_job_id)
        foreground, tasks = self._poll_foreground(foreground_id)
        unit_samples = {self.unit: _show(self.unit)}
        if self.dispatch_unit:
            unit_samples[self.dispatch_unit] = _show(self.dispatch_unit)
        sample = {
            "monotonic": time.monotonic(),
            "host": _host_sample(),
            "units": unit_samples,
            "lab_state": state.get("lab_state"),
            "lab_job_state": state.get("state"),
            "foreground": None
            if foreground is None
            else {
                "status": foreground.get("status"),
                "real_model": foreground.get("real_model"),
                "runtime_id": foreground.get("runtime_id"),
            },
            "foreground_slot_busy": tasks.get("busy"),
            "lab_job_in_foreground_scheduler": any(
                job.get("job_id") == lab_job_id
                for job in tasks.get("jobs", [])
                if isinstance(job, dict)
            ),
            "gpu_turn_ledger": _gpu_ledger(self.args.gpu_runtime_db, since),
        }
        self.samples.append(sample)
        both_working = bool(
            foreground
            and foreground.get("status") == "running"
            and state.get("lab_state") == "running"
        )
        both_active = bool(
            unit_samples.get(self.unit, {}).get("ActiveState") == "active"
            and self.dispatch_unit
            and unit_samples.get(self.dispatch_unit, {}).get("ActiveState") == "active"
        )
        return both_working, both_active

    def _wait_coexistence(self, started: dict[str, Any], since: float) -> dict[str, Any]:
        assert self.console is not None
        agent = self._api_post(
            "control_return_agent", "/api/control", {"command": "return-control"}
        )
        session = self._api_get("state", "/api/state")["control"]
        task = self._api_post(
            "foreground_start",
            "/api/tasks",
            {"kind": "hello", "lease_id": agent["lease_id"], "generation": agent["generation"]},
        )
        foreground_id = task.get("job_id")
        if not isinstance(foreground_id, str):
            raise RuntimeError("foreground desktop task returned no job id")
        deadline = time.monotonic() + min(MAX_RUNTIME_SECONDS, self.args.wall_seconds + 900)
        overlap_work = False
        overlap_units = False
        lab_job: dict[str, Any] = {}
        foreground: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            both_working, both_active = self._sample_once(foreground_id, started["job_id"], since)
            overlap_work = overlap_work or both_working
            overlap_units = overlap_units or both_active
            lab_job = self._poll_status(started["job_id"])
            foreground, _ = self._poll_foreground(foreground_id)
            foreground_done = foreground is not None and foreground.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
                "waiting_human",
            }
            if foreground_done and lab_job.get("lab_state") in {"completed", "stopped", "failed"}:
                break
            if self.dispatch_wrapper and self.dispatch_wrapper.poll() not in {None, 0}:
                raise RuntimeError("run-bound Lab dispatcher exited nonzero")
            time.sleep(self.args.poll_seconds)
        if (
            not foreground
            or foreground.get("status") != "succeeded"
            or foreground.get("real_model") is not True
        ):
            raise RuntimeError("foreground hello task did not succeed with the real Decider")
        if (self.workspace / "hello.txt").read_bytes() != b"Hello from the local agent.\n":
            raise RuntimeError("verified foreground workspace content differs")
        if lab_job.get("lab_state") != "completed":
            raise RuntimeError("Lab run did not reach completed state")
        ledger = _gpu_ledger(self.args.gpu_runtime_db, since)
        turns = ledger.get("turns", []) if ledger.get("available") else []
        done_owners = {turn.get("owner") for turn in turns if turn.get("state") == "done"}
        if not {"aos", "lab"}.issubset(done_owners):
            raise RuntimeError("shared GPU ledger lacks completed turns for both owners")
        if not overlap_work or not overlap_units:
            raise RuntimeError("measured AOS/Lab process and work intervals did not overlap")
        if any(sample["lab_job_in_foreground_scheduler"] for sample in self.samples):
            raise RuntimeError("detached Lab run appeared in the foreground desktop scheduler")
        return {
            "session_id": session["session_id"],
            "aos_job_id": started["job_id"],
            "aos_run_id": started["aos_run_id"],
            "lab_run_id": started["lab_run_id"],
            "foreground_job_id": foreground_id,
            "foreground_status": foreground["status"],
            "foreground_real_model": foreground["real_model"],
            "lab_terminal_state": lab_job["lab_state"],
            "overlap_work": overlap_work,
            "overlap_systemd_units": overlap_units,
            "gpu_turns": ledger,
            "workspace_file_sha256": _sha(self.workspace / "hello.txt"),
        }

    def _recover_after_restart(self, started: dict[str, Any]) -> dict[str, Any]:
        assert self.console is not None
        agent = self._api_post(
            "control_return_agent", "/api/control", {"command": "return-control"}
        )
        del agent
        old_session = self._api_get("state", "/api/state")["control"]["session_id"]
        quiesced = self._api_post(
            "restart_quiesce", "/api/restart/quiesce", {"session_id": old_session}
        )
        state = self._poll_status(started["job_id"])
        self.phase_receipts.append(
            {
                "phase": "restart_quiesce",
                "result": quiesced,
                "lab_state_after_drain": state.get("lab_state"),
            }
        )
        self._stop_aos()
        self.token_before = set((self.args.aos_source / "runs").glob("desktop-console-*.token"))
        self._launch_aos()
        new_session = self._api_get("state", "/api/state")["control"]
        if new_session.get("session_id") == old_session:
            raise RuntimeError("AOS process restart did not create a new desktop session")
        human = self._take_human()
        recovered = self._api_post(
            "lab_recover",
            f"/api/lab/jobs/{started['job_id']}/recover",
            {"lease_id": human["lease_id"], "generation": human["generation"]},
        )
        if recovered.get("lab_run_id") != started["lab_run_id"]:
            raise RuntimeError("explicit recovery rebound a different Lab run")
        deadline = time.monotonic() + min(180, self.args.wall_seconds + 60)
        while time.monotonic() < deadline:
            current = self._poll_status(started["job_id"])
            if current.get("lab_state") in {"completed", "stopped", "failed"}:
                report = self._api_get("lab_report", f"/api/lab/jobs/{started['job_id']}/report")
                return {
                    "old_session_id": old_session,
                    "new_session_id": new_session["session_id"],
                    "recovered_lab_run_id": recovered.get("lab_run_id"),
                    "terminal_lab_state": current.get("lab_state"),
                    "report_sha256": report.get("report_sha256"),
                }
            time.sleep(self.args.poll_seconds)
        raise TimeoutError("recovered Lab run did not reach a verified terminal state")

    def _make_html_report(self, run_id: str) -> dict[str, object]:
        env = {
            **os.environ,
            "LAB_DIRECTOR_DSN_FILE": str(self.args.director_dsn_file),
            "LAB_PLANNER_DSN_FILE": str(self.args.planner_dsn_file),
            "LAB_SUITE_REGISTRY_FILE": str(self.args.suite_registry),
            "SWAPP_AOS_GPU_UNIT": self.args.aos_unit,
            "SWAPP_LAB_GPU_UNIT": (self.dispatch_unit or ""),
            "SWAPP_GPU_RUNTIME_DB": str(self.args.gpu_runtime_db),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        result = subprocess.run(
            [str(self.args.lab_python), "-m", "lab.cli", "report", run_id],
            cwd=self.args.lab_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("verified Lab HTML report command failed")
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise RuntimeError("Lab report command returned no receipt") from exc
        report_path = Path(payload["report"])
        if not _inside(report_path, self.args.lab_root / "data/runtime/reports"):
            raise RuntimeError("Lab report escaped the isolated runtime directory")
        if _sha(report_path) != payload.get("sha256"):
            raise RuntimeError("Lab HTML report digest differs from CLI receipt")
        return {
            "run_id": run_id,
            "path": str(report_path),
            "sha256": payload["sha256"],
            "bytes": report_path.stat().st_size,
            "summary": payload.get("summary"),
        }

    def execute(self) -> dict[str, object]:
        started_at = time.time()
        since = time.time()
        try:
            self._launch_aos()
            started = self._start_lab()
            # Start the run-bound Lab process before returning the foreground
            # scheduler slot to AOS; the subsequent hello task can then overlap.
            self._launch_dispatch(started["lab_run_id"])
            if self.args.phase == "coexistence":
                coexistence = self._wait_coexistence(started, since)
                html_report = self._make_html_report(started["lab_run_id"])
                self.outcome = "passed"
                return {
                    "phase": "coexistence",
                    "started": started,
                    "coexistence": coexistence,
                    "html_report": html_report,
                }
            recovery = self._recover_after_restart(started)
            html_report = self._make_html_report(started["lab_run_id"])
            self.outcome = "passed"
            return {
                "phase": "restart-recovery",
                "started": started,
                "restart_recovery": recovery,
                "html_report": html_report,
            }
        except Exception as exc:
            self.outcome = "failed"
            self.failure_type = type(exc).__name__
            raise
        finally:
            self._stop_aos()
            if self.dispatch_unit:
                unit_state = _show(self.dispatch_unit)
                if unit_state.get("ActiveState") == "active":
                    subprocess.run(
                        ["/usr/bin/systemctl", "--user", "stop", self.dispatch_unit],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
            if self.dispatch_wrapper is not None and self.dispatch_wrapper.poll() is None:
                self.dispatch_wrapper.terminate()
                try:
                    self.dispatch_wrapper.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.dispatch_wrapper.kill()
            if self.token_path is not None:
                self.token_path.unlink(missing_ok=True)
            report = {
                "schema": "aos-lab-coexistence-review.v1",
                "started_at": started_at,
                "finished_at": time.time(),
                "phase": self.args.phase,
                "outcome": self.outcome,
                "failure_type": self.failure_type,
                "aos_source": str(self.args.aos_source),
                "aos_unit": self.unit,
                "lab_root": str(self.args.lab_root),
                "suite": self.args.suite,
                "suite_manifest_sha256": self.suite["suite_manifest_sha256"],
                "desktop_manifest_sha256": _sha(self.args.desktop_manifest),
                "decider_manifest_sha256": _sha(self.args.decider_manifest),
                "phase_receipts": self.phase_receipts,
                "api_latency_ms": self.api_ms,
                "resource_samples": self.samples,
                "limitations": [
                    "A real acceptance requires --execute and success in every phase.",
                    "No hidden model reasoning or prompt content is collected.",
                ],
            }
            (self.base / "driver-observations.json").write_text(json.dumps(report, indent=2) + "\n")
            os.chmod(self.base / "driver-observations.json", 0o600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aos-source", type=Path, required=True)
    parser.add_argument("--aos-python", type=Path, required=True)
    parser.add_argument("--desktop-manifest", type=Path, required=True)
    parser.add_argument("--decider-manifest", type=Path, required=True)
    parser.add_argument("--model-python", type=Path, required=True)
    parser.add_argument("--aos-unit", required=True)
    parser.add_argument("--lab-root", type=Path, required=True)
    parser.add_argument("--lab-python", type=Path, required=True)
    parser.add_argument("--lab-api-url", required=True)
    parser.add_argument("--lab-token-file", type=Path, required=True)
    parser.add_argument("--suite-registry", type=Path, required=True)
    parser.add_argument("--suite-runtime-root", type=Path, required=True)
    parser.add_argument("--director-dsn-file", type=Path, required=True)
    parser.add_argument("--planner-dsn-file", type=Path, required=True)
    parser.add_argument("--gpu-runtime-db", type=Path, required=True)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--experiments", type=int, required=True)
    parser.add_argument("--wall-seconds", type=int, required=True)
    parser.add_argument("--model-tokens", type=int, required=True)
    parser.add_argument(
        "--phase", choices=("coexistence", "restart-recovery"), default="coexistence"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--startup-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="start the isolated actual AOS/Lab acceptance services",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runner: CoexistenceRun | None = None
    try:
        args.aos_source = args.aos_source.resolve(strict=True)
        args.lab_root = args.lab_root.resolve(strict=True)
        if args.aos_source == LIVE_AOS_ROOT or _inside(args.aos_source, LIVE_AOS_ROOT):
            raise ValueError("live AOS source is outside this driver scope")
        if args.lab_root == PROJECT_ROOT or not _inside(
            args.lab_root, PROJECT_ROOT / "data/runtime"
        ):
            raise ValueError(
                "Lab must use a separate worktree under this project's private runtime"
            )
        if args.lab_root == LIVE_AOS_ROOT or _inside(args.lab_root, LIVE_AOS_ROOT):
            raise ValueError("Lab worktree cannot be inside live AOS")
        for name in (
            "aos_python",
            "lab_python",
            "model_python",
            "desktop_manifest",
            "decider_manifest",
            "lab_token_file",
            "suite_registry",
            "director_dsn_file",
            "planner_dsn_file",
        ):
            path = getattr(args, name)
            if not path.exists() or (
                name not in {"aos_python", "lab_python", "model_python"} and path.is_symlink()
            ):
                raise ValueError(f"required isolated input unavailable: {name}")
        for name in ("aos_python", "lab_python", "model_python"):
            executable = getattr(args, name).resolve(strict=True)
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError(f"required isolated executable unavailable: {name}")
        _private_file(args.lab_token_file, "Lab API token")
        _private_file(args.director_dsn_file, "Director DSN")
        _private_file(args.planner_dsn_file, "Planner DSN")
        if not _inside(args.suite_runtime_root, args.lab_root / "data/runtime"):
            raise ValueError("suite runtime root must remain under this Lab worktree")
        if (
            args.gpu_runtime_db.is_symlink()
            or args.gpu_runtime_db.parent.resolve()
            != (args.lab_root / "data/runtime/gpu").resolve()
        ):
            raise ValueError("GPU runtime DB path must be a direct isolated Lab runtime child")
        if args.lab_api_url != args.lab_api_url.rstrip("/"):
            raise ValueError("Lab API URL must not have a trailing slash")
        parsed = urlsplit(args.lab_api_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path
        ):
            raise ValueError("Lab API must be an exact loopback origin")
        if re.fullmatch(r"swapp-aos-gpu-[a-z0-9_.@-]+\.service", args.aos_unit) is None:
            raise ValueError("AOS owner unit must match the fixed GPU principal namespace")
        if not 1 <= args.experiments <= 35 or not 1 <= args.wall_seconds <= MAX_RUNTIME_SECONDS:
            raise ValueError("run budget is outside the bounded Lab API contract")
        if not 0 <= args.model_tokens <= 350_000 or not 1 <= args.startup_seconds <= 900:
            raise ValueError("token or startup budget is outside its allowed bound")
        if not 0.5 <= args.poll_seconds <= 10:
            raise ValueError("poll cadence must be between 0.5 and 10 seconds")
        if args.aos_source.resolve() == args.lab_root.resolve():
            raise ValueError("AOS and Lab sources must remain separate")
        for path in (args.desktop_manifest, args.decider_manifest):
            resolved = path.resolve(strict=True)
            if resolved == LIVE_AOS_ROOT or LIVE_AOS_ROOT in resolved.parents:
                raise ValueError("caller-supplied manifests cannot come from live AOS")
        suite = _suite_metadata(args)
        if not args.execute:
            print(
                json.dumps(
                    {
                        "schema": "aos-lab-coexistence-preflight.v1",
                        "ready_for_explicit_execute": True,
                        "aos_source": str(args.aos_source),
                        "lab_root": str(args.lab_root),
                        "aos_unit": args.aos_unit,
                        "suite": suite,
                        "phase": args.phase,
                        "gpu_called": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        output_parent = args.output_dir.parent.resolve(strict=True)
        if not _inside(output_parent, PROJECT_ROOT / "data/runtime"):
            raise ValueError("acceptance outputs must stay inside this project's private runtime")
        output_root = output_parent / args.output_dir.name
        if output_root.is_symlink():
            raise ValueError("acceptance output directory cannot be a symlink")
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output_root, 0o700)
        output_root = output_root.resolve(strict=True)
        runner = CoexistenceRun(args, output_root, suite)
        result = runner.execute()
        result.update(
            {
                "status": "passed",
                "gpu_called": True,
                "receipt_path": str(runner.base / "driver-observations.json"),
                "receipt_sha256": _sha(runner.base / "driver-observations.json"),
            }
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError, KeyError, TypeError) as exc:
        failure = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "gpu_called": bool(args.execute),
        }
        if runner is not None:
            receipt = runner.base / "driver-observations.json"
            if receipt.is_file():
                failure.update({"receipt_path": str(receipt), "receipt_sha256": _sha(receipt)})
        print(json.dumps(failure), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
