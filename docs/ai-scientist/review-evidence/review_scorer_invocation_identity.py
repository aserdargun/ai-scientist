"""Exercise the production identity verifier in real bounded systemd services."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from lab.scorer.supervisor import _ensure_aggregate_slice, SCORER_SLICE
from lab.scorer.worker import verify_systemd_invocation

ROOT = Path(__file__).resolve().parents[3]
MODES = ("valid", "wrong_expected_unit", "wrong_invocation", "missing_invocation", "unavailable_manager", "wrong_slice")


def child(mode, expected_unit):
    if mode == "wrong_expected_unit":
        expected_unit = "swapp-ai-scientist-scorer-" + uuid4().hex + ".service"
    elif mode == "wrong_invocation":
        actual = os.environ.get("INVOCATION_ID")
        replacement = uuid4().hex
        assert replacement != actual
        os.environ["INVOCATION_ID"] = replacement
    elif mode == "missing_invocation":
        os.environ.pop("INVOCATION_ID", None)
    elif mode == "unavailable_manager":
        unavailable = "/run/user/" + str(os.getuid()) + "/absent-owned-invocation-" + uuid4().hex
        os.environ["XDG_RUNTIME_DIR"] = unavailable
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + unavailable + "/bus"
    try:
        verified = verify_systemd_invocation(expected_unit)
        result = {"accepted": True, "unit": verified.unit, "control_group": verified.control_group,
                  "invocation_id": verified.invocation_id}
    except RuntimeError as error:
        result = {"accepted": False, "error_type": type(error).__name__, "error": str(error)}
    print(json.dumps(result))


def main():
    sources = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
        "lab/scorer/worker.py", "lab/scorer/supervisor.py",
    )}
    record = {"checked_at": datetime.now(UTC).isoformat(), "source_sha256": sources, "cases": [],
              "scope": "Production systemd identity verifier in six actual owned processes. Correct identity and five invalid contexts; no credentials, PostgreSQL claims, Scorer metrics, recovery writer, GPU or AOS runtime used."}
    _ensure_aggregate_slice()
    for mode in MODES:
        unit = "swapp-ai-scientist-scorer-" + uuid4().hex + ".service"
        command = [
            "/usr/bin/systemd-run", "--user", "--wait", "--collect", "--quiet", "--pipe",
            "--unit=" + unit, "--slice=" + ("app.slice" if mode == "wrong_slice" else SCORER_SLICE),
            "--property=Type=exec", "--property=MemoryMax=64M", "--property=MemorySwapMax=0",
            "--property=CPUQuota=25%", "--property=TasksMax=8", "--property=RuntimeMaxSec=10",
            "--property=TimeoutStopSec=1", "--property=KillMode=control-group",
            "--property=NoNewPrivileges=yes", "--working-directory=" + str(ROOT),
            str(ROOT / ".venv/bin/python"), str(Path(__file__).resolve()), "child", mode, unit,
        ]
        try:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=15, check=False)
        except BaseException:
            # The UUID unit belongs solely to this probe. RuntimeMaxSec also
            # bounds it if the review process itself disappears.
            subprocess.run(["/usr/bin/systemctl", "--user", "stop", unit], capture_output=True,
                           text=True, timeout=5, check=False)
            raise
        parsed = json.loads(result.stdout) if result.returncode == 0 else None
        status = subprocess.run([
            "/usr/bin/systemctl", "--user", "show", unit,
            "--property=LoadState", "--property=ActiveState", "--property=MainPID",
        ], capture_output=True, text=True, timeout=5, check=False)
        properties = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
        inactive = properties.get("ActiveState") == "inactive" and properties.get("MainPID") == "0"
        record["cases"].append({"mode": mode, "command": command, "exit_code": result.returncode,
                                "result": parsed, "unit_state": properties,
                                "unit_inactive": inactive,
                                "passed": result.returncode == 0 and parsed["accepted"] == (mode == "valid") and inactive})
    record["source_unchanged"] = all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == sha
                                     for path, sha in sources.items())
    record["all_passed"] = all(item["passed"] for item in record["cases"]) and record["source_unchanged"]
    record["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    Path(__file__).with_name("scorer-invocation-identity-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"all_passed": record["all_passed"], "source_unchanged": record["source_unchanged"],
                      "cases": [{key: value for key, value in item.items() if key != "command"}
                                for item in record["cases"]]}, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "child" and sys.argv[2] in MODES:
        child(sys.argv[2], sys.argv[3])
    else:
        main()
