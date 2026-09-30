"""Fixed private six-container proof; default prints plan and starts nothing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "source"
NODES = [
    "tests/test_sandbox058_boundaries.py::test_actual_network_attempt_is_rejected[resolver]",
    "tests/test_sandbox058_boundaries.py::test_actual_network_attempt_is_rejected[udp53]",
    "tests/test_sandbox058_boundaries.py::test_actual_network_attempt_is_rejected[http]",
    "tests/test_sandbox058_boundaries.py::test_actual_typed_fit_cannot_see_eval_labels_or_metadata",
    "tests/test_sandbox058_boundaries.py::test_actual_candidate_tool_injection_is_untrusted",
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"nodes": NODES, "containers": 6, "inner_seconds": 240,
                          "unit_seconds": 270, "unit_memory_bytes": 768 * 1024**2,
                          "unit_cpu_percent": 50, "unit_tasks": 64,
                          "sandbox_memory_bytes": 512 * 1024**2,
                          "sandbox_cpu": 0.5, "sandbox_pids": 32,
                          "network": "none", "gpu": False, "db": False,
                          "private_admission": str(ROOT / "data/runtime/sandbox058/admission/sandbox.lock"),
                          "cleanup": "only exact returned IDs with original ownership labels",
                          "launch_requires": "root review and shared CPU lock handoff"}, indent=2))
        return 0
    os.umask(0o077)
    os.environ.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                      PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    os.chdir(ROOT); sys.path.insert(0, str(ROOT))
    import pytest
    from lab.sandbox.docker_runner import LocalDockerRunner, DEFAULT_SANDBOX_IMAGE
    capture = json.loads((HERE / "source-capture-r2.json").read_text())
    assert sha(Path(__file__)) == capture["review_driver_sha256"]
    assert sha(ROOT / "tests/test_sandbox058_boundaries.py") == capture["fixed_test_sha256"]
    fixed_hashes = {str(Path(__file__)): sha(Path(__file__)),
                    str(ROOT / "tests/test_sandbox058_boundaries.py"): sha(ROOT / "tests/test_sandbox058_boundaries.py")}
    def verify():
        for rel, digest in capture["source_sha256"].items():
            assert sha(ROOT / rel) == digest, rel
        for path, digest in fixed_hashes.items():
            assert sha(Path(path)) == digest
        for name, module in tuple(sys.modules.items()):
            if name == "lab" or name == "harness" or name.startswith(("lab.", "harness.")):
                assert ROOT in Path(module.__file__).resolve().parents
    verify()
    assert DEFAULT_SANDBOX_IMAGE == capture["sandbox_image"]
    image = subprocess.run(["/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", DEFAULT_SANDBOX_IMAGE],
                           capture_output=True, text=True, timeout=5, check=True)
    assert image.stdout.strip() == DEFAULT_SANDBOX_IMAGE
    security = subprocess.run(["/usr/bin/docker", "info", "--format", "{{json .SecurityOptions}}"],
                              capture_output=True, text=True, timeout=5, check=True)
    assert any("name=seccomp" in s and "profile=builtin" in s for s in json.loads(security.stdout))
    actual = HERE / "actual-r2"
    actual.mkdir(mode=0o700, exist_ok=False)
    record = {"schema": "sandbox058-actual.v1", "source_capture_sha256": sha(HERE / "source-capture-r2.json"),
              "fixed_driver_test_sha256": fixed_hashes, "image": DEFAULT_SANDBOX_IMAGE,
              "containers": [], "reports": [], "collected": [], "cleanup": [], "complete": False}
    def persist():
        data = (json.dumps(record, indent=2) + "\n").encode()
        assert len(data) < 512 * 1024
        temp = actual / "receipt.tmp"
        with temp.open("wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        temp.replace(actual / "receipt.json")
    original = LocalDockerRunner._create_container
    active = {"node": None}
    def audited_create(runner, command):
        assert len(record["containers"]) < 6
        assert runner.admission_lock == ROOT / "data/runtime/sandbox058/admission/sandbox.lock"
        cid = original(runner, command)
        owner = command[command.index("--label") + 1].split("=", 1)[1]
        item = {"id": cid, "owner_label": runner.owner_label, "owner": owner,
                "node": active["node"], "command": command, "valid": False}
        record["containers"].append(item); persist()
        result = subprocess.run(["/usr/bin/docker", "container", "inspect", cid],
                                capture_output=True, timeout=5, check=True)
        assert len(result.stdout) < 65536
        inspected = json.loads(result.stdout)[0]
        host = inspected["HostConfig"]; config = inspected["Config"]
        assert inspected["Id"] == cid and config["Labels"][runner.owner_label] == owner
        binds = [mount for mount in inspected["Mounts"] if mount["Type"] == "bind"]
        expected_destinations = {"/candidate", "/fit-artifact"} if "--mount" in command and any(
            "dst=/fit-artifact" in arg for arg in command
        ) else {"/candidate"}
        assert {mount["Destination"] for mount in binds} == expected_destinations
        assert all(not mount["RW"] and runner.work_root in Path(mount["Source"]).resolve().parents
                   for mount in binds)
        assert all(mount["Type"] in {"bind", "tmpfs"} for mount in inspected["Mounts"])
        item["valid"] = (config["User"] == "10001:10001" and host["NetworkMode"] == "none"
                         and host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
                         and host["SecurityOpt"] == ["no-new-privileges"] and not host["Privileged"]
                         and not host.get("Devices") and not host.get("DeviceRequests")
                         and not host.get("PidMode") and host.get("IpcMode") == "private"
                         and not host.get("CapAdd") and host["Memory"] == 512 * 1024**2
                         and host["MemorySwap"] == host["Memory"] and host["PidsLimit"] == 32
                         and host["NanoCpus"] == 500_000_000)
        item["mounts"] = inspected["Mounts"]
        item["network_mode"] = host["NetworkMode"]
        persist(); assert item["valid"]
        return cid
    class Plugin:
        def pytest_collection_finish(self, session):
            record["collected"] = [item.nodeid for item in session.items]
            persist(); assert record["collected"] == NODES
        def pytest_runtest_logstart(self, nodeid, location):
            active["node"] = nodeid
        def pytest_runtest_logreport(self, report):
            record["reports"].append({"node": report.nodeid, "when": report.when,
                                      "outcome": report.outcome, "seconds": report.duration,
                                      "xfail": hasattr(report, "wasxfail"),
                                      "failure": str(report.longrepr)[:2048] if report.failed else None})
            persist()
    def timeout(_sig, _frame):
        raise TimeoutError("sandbox058 240-second fixed deadline")
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(240); started = time.monotonic(); code = 1
    LocalDockerRunner._create_container = audited_create
    try:
        persist(); code = int(pytest.main(["-x", "-q", "--tb=short", *NODES], plugins=[Plugin()]))
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        LocalDockerRunner._create_container = original
        # Only IDs durably returned to this exact invocation may be inspected or removed.
        for item in record["containers"]:
            result = subprocess.run(["/usr/bin/docker", "container", "inspect", item["id"]],
                                    capture_output=True, timeout=5, check=False)
            if result.returncode == 0:
                inspected = json.loads(result.stdout)[0]
                assert inspected["Id"] == item["id"]
                assert inspected["Config"]["Labels"][item["owner_label"]] == item["owner"]
                subprocess.run(["/usr/bin/docker", "container", "rm", "--force", item["id"]],
                               capture_output=True, timeout=5, check=True)
                result = subprocess.run(["/usr/bin/docker", "container", "inspect", item["id"]],
                                        capture_output=True, timeout=5, check=False)
            absent = (result.returncode == 1 and result.stdout in (b"", b"\n", b"[]\n")
                      and re.fullmatch(
                          rb"(?:Error response from daemon: |Error: )No such (?:container|object): "
                          + re.escape(item["id"].encode()) + rb"\n?", result.stderr
                      ) is not None)
            record["cleanup"].append({"id": item["id"], "absent": absent})
        verify(); record["source_verified_after"] = True
        record["private_admission_intent_absent"] = not (
            ROOT / "data/runtime/sandbox058/admission/sandbox.intent"
        ).exists()
        record["elapsed_seconds"] = time.monotonic() - started
        record["pytest_exit_code"] = code
        expected = [(n, phase) for n in NODES for phase in ("setup", "call", "teardown")]
        record["complete"] = (code == 0 and record["collected"] == NODES
                              and [(r["node"],r["when"]) for r in record["reports"]] == expected
                              and all(r["outcome"] == "passed" and not r["xfail"] for r in record["reports"])
                              and len(record["containers"]) == 6
                              and all(c["valid"] for c in record["containers"])
                              and all(c["absent"] for c in record["cleanup"])
                              and record["private_admission_intent_absent"])
        persist()
    print(json.dumps({"receipt": str(actual / "receipt.json"), "complete": record["complete"]}))
    return 0 if record["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
