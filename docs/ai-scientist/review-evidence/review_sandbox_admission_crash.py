"""Real Docker crash windows; kill/remove only this probe's exact identities."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
IMAGE = "sha256:3f9df3002bccd1cb74aa5f6060da74dbdecc4c65fc0c8f712ecf67f0590754df"
WINDOWS = ("intent_before_create", "created_before_id", "id_before_start", "running", "removed_before_clear")


def container_state(name):
    result = subprocess.run(["docker", "container", "inspect", "--format", "{{.State.Status}}", name],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if "No such" in result.stderr or "no such" in result.stderr:
        return "absent"
    raise RuntimeError("Docker inspection failed: " + result.stderr)


def child(directory, mode, window):
    sys.path.insert(0, str(directory))
    from runner_snapshot import LocalDockerRunner, SandboxProfile

    def pause(stage):
        if mode == "first" and window == stage:
            (directory / "ready").write_text(stage)
            while True:
                time.sleep(0.1)

    class RecordingRunner(LocalDockerRunner):
        def _write_admission_intent(self, name, nonce, work_dir):
            if mode == "second":
                old = json.loads((directory / "first.identity").read_text())
                (directory / "handoff.json").write_text(json.dumps({
                    "first_container_state": container_state(old["name"]),
                    "first_workdir_removed": not Path(old["work_dir"]).exists(),
                    "first_root_sibling_preserved": (directory / "first-work" / "keep.txt").read_text() == "keep",
                }))
            super()._write_admission_intent(name, nonce, work_dir)
            (directory / f"{mode}.identity").write_text(json.dumps({"name": name, "work_dir": str(work_dir)}))
            pause("intent_before_create")

        def _create_container(self, command):
            identifier = super()._create_container(command)
            pause("created_before_id")
            return identifier

        def _persist_container_id(self, identifier):
            super()._persist_container_id(identifier)
            pause("id_before_start")

        def _clear_admission_intent(self):
            pause("removed_before_clear")
            return super()._clear_admission_intent()

    runner = RecordingRunner(image=IMAGE, work_root=directory / f"{mode}-work",
                             admission_lock=directory / "shared-admission.lock",
                             profile=SandboxProfile(memory_bytes=128 * 1024**2, cpus=0.25,
                                                    timeout_seconds=60, output_bytes=1024))
    if mode == "first":
        (runner.work_root / "keep.txt").write_text("keep")
    source = (b"import time; time.sleep(45)\n" if mode == "first" and window == "running"
              else b"from pathlib import Path; Path('/output/ok').write_text('ok')\n")
    result = runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"")
    print(json.dumps({"phase": result.phase, "artifacts": len(result.artifacts)}))


def probe(directory, window, source, sentinel):
    directory.mkdir(mode=0o700)
    (directory / "runner_snapshot.py").write_bytes(source)
    result = {"window": window, "cleanup": {}}
    process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "child", str(directory), "first", window],
                               start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            identity_path = directory / "first.identity"
            if identity_path.exists():
                identity = json.loads(identity_path.read_text())
                if ((window == "running" and container_state(identity["name"]) == "running")
                        or (directory / "ready").exists()):
                    break
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                raise RuntimeError("runner exited before crash window: " + (stdout + stderr).decode(errors="replace"))
            time.sleep(0.1)
        else:
            raise RuntimeError("crash window not reached")
        marker = directory / "shared-admission.intent"
        intent = json.loads(marker.read_text())
        result["persisted_full_id_before_kill"] = "container_id" in intent
        result["container_state_before_kill"] = container_state(identity["name"])
        result["workdir_exists_before_kill"] = Path(identity["work_dir"]).exists()
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        result["killed_runner_returncode"] = process.returncode
        second = subprocess.run([sys.executable, str(Path(__file__).resolve()), "child", str(directory), "second", window],
                                capture_output=True, text=True, timeout=35, check=False)
        result["second_runner"] = {"exit_code": second.returncode, "stdout": second.stdout, "stderr": second.stderr}
        handoff = directory / "handoff.json"
        result["handoff"] = json.loads(handoff.read_text()) if handoff.exists() else None
        result["independent_sentinel_running"] = container_state(sentinel) == "running"
        result["intent_cleared"] = not marker.exists()
        expected_state = {"intent_before_create": "absent", "created_before_id": "created", "id_before_start": "created",
                          "running": "running", "removed_before_clear": "absent"}[window]
        expected_id = window in ("id_before_start", "running", "removed_before_clear")
        result["passed"] = (result["container_state_before_kill"] == expected_state
                            and result["persisted_full_id_before_kill"] == expected_id
                            and second.returncode == 0 and result["handoff"] is not None
                            and result["handoff"]["first_container_state"] == "absent"
                            and result["handoff"]["first_workdir_removed"]
                            and result["handoff"]["first_root_sibling_preserved"]
                            and result["independent_sentinel_running"] and result["intent_cleared"])
    except Exception as error:
        result.update(passed=False, error=str(error))
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        for mode in ("first", "second"):
            identity_path = directory / f"{mode}.identity"
            if identity_path.exists():
                name = json.loads(identity_path.read_text())["name"]
                subprocess.run(["docker", "container", "rm", "--force", name], capture_output=True, timeout=10, check=False)
                result["cleanup"][name] = container_state(name) == "absent"
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    return result


def main():
    if len(sys.argv) == 5 and sys.argv[1] == "child":
        child(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
        return
    source_path = ROOT / "lab/sandbox/docker_runner.py"
    source = source_path.read_bytes()
    result = {"checked_at": datetime.now(UTC).isoformat(), "runner_source_sha256": hashlib.sha256(source).hexdigest(),
              "image": IMAGE, "profile": {"memory_bytes": 128 * 1024**2, "cpus": 0.25, "timeout_seconds": 60},
              "scope": "Five controlled lifecycle crash windows, real Docker, distinct prior/current work roots, exact owned cleanup. No GPU or AOS. Does not simulate delayed daemon network requests or power loss.",
              "cases": []}
    with tempfile.TemporaryDirectory(prefix="admission-review-", dir=ROOT / "data/runtime") as temporary:
        directory = Path(temporary)
        sentinel = "swapp-admission-sentinel-" + directory.name.removeprefix("admission-review-")
        command = ["docker", "run", "--detach", "--rm", "--name", sentinel,
                   "--label", "swapp.ai-scientist.sandbox.owner=independent-review-sentinel",
                   "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--user=10001:10001", "--memory=64m", "--memory-swap=64m", "--cpus=0.25", "--pids-limit=16",
                   "--entrypoint", "python", IMAGE, "-c", "import time; time.sleep(180)"]
        started = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        if started.returncode:
            raise RuntimeError("sentinel could not start: " + started.stderr)
        try:
            for window in WINDOWS:
                case = probe(directory / window, window, source, sentinel)
                result["cases"].append(case)
                print(json.dumps({"window": window, "passed": case["passed"], "error": case.get("error")}), flush=True)
        finally:
            subprocess.run(["docker", "container", "rm", "--force", sentinel], capture_output=True, timeout=10, check=False)
            result["sentinel_cleanup"] = container_state(sentinel) == "absent"
    result["source_unchanged_during_probe"] = source_path.read_bytes() == source
    result["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["command"] = ".venv/bin/python docs/ai-scientist/review-evidence/review_sandbox_admission_crash.py"
    result["all_passed"] = all(case["passed"] for case in result["cases"]) and result["sentinel_cleanup"]
    (ROOT / "docs/ai-scientist/review-evidence/sandbox-admission-crash-review.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"all_passed": result["all_passed"], "source_unchanged": result["source_unchanged_during_probe"]}))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
