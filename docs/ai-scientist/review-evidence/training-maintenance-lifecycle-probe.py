"""CPU-only proof of the no-block child result and exact-generation drain."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import uuid


SYSTEMCTL = "/usr/bin/systemctl"
SYSTEMD_RUN = "/usr/bin/systemd-run"


def _show(unit: str) -> tuple[int, dict[str, str]]:
    result = subprocess.run(
        [
            SYSTEMCTL,
            "--user",
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=ControlGroup",
            "--property=InvocationID",
            "--property=MainPID",
            unit,
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    return result.returncode, dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def run(receipt_path: pathlib.Path) -> dict[str, object]:
    unit = f"swapp-lab-train-job-{uuid.uuid4().hex}.service"
    marker = pathlib.Path(tempfile.gettempdir()) / f"training-child-ready-{uuid.uuid4().hex}"
    child_code = "import pathlib,time; pathlib.Path(%r).write_text('ready'); time.sleep(45)" % str(
        marker
    )
    launch = subprocess.run(
        [
            SYSTEMD_RUN,
            "--user",
            "--no-block",
            "--quiet",
            f"--unit={unit}",
            "--slice=swapp-gpu.slice",
            "--property=MemoryMax=32M",
            "--property=MemorySwapMax=0",
            "--property=CPUQuota=10%",
            "--property=TasksMax=8",
            "--property=RuntimeMaxSec=60",
            "--property=KillMode=control-group",
            "--property=NoNewPrivileges=yes",
            "/usr/bin/python3",
            "-c",
            child_code,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    try:
        if launch.returncode != 0:
            raise RuntimeError("transient child launch failed")
        deadline = time.monotonic() + 8
        values: dict[str, str] = {}
        while time.monotonic() < deadline:
            status, values = _show(unit)
            if (
                status == 0
                and values.get("ActiveState") == "active"
                and values.get("MainPID", "0") != "0"
                and marker.is_file()
            ):
                break
            time.sleep(0.1)
        if values.get("LoadState") != "loaded" or values.get("ActiveState") != "active":
            raise RuntimeError("child did not remain loaded after writing its result")
        invocation_id = values.get("InvocationID", "")
        if re.fullmatch(r"[0-9a-f]{32}", invocation_id) is None:
            raise RuntimeError("transient child invocation identity is missing")
        pid = int(values["MainPID"])
        stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        start_ticks = int(stat_text.rsplit(")", 1)[1].split()[19])
        cgroup = values["ControlGroup"].rstrip("/")
        proc_cgroup = pathlib.Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
        unified = next(
            line.split(":", 2)[2].rstrip("/")
            for line in proc_cgroup.splitlines()
            if line.startswith("0::")
        )
        if unified != cgroup or not cgroup.endswith("/" + unit):
            raise RuntimeError("child ControlGroup did not match the exact unit")
        if marker.read_text(encoding="ascii") != "ready":
            raise RuntimeError("child result marker was not readable")
        stopped = subprocess.run(
            [SYSTEMCTL, "--user", "stop", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if stopped.returncode != 0:
            raise RuntimeError("exact transient child stop failed")
        deadline = time.monotonic() + 8
        gone = False
        while time.monotonic() < deadline:
            status, after = _show(unit)
            if status != 0 or after.get("LoadState") == "not-found":
                gone = True
                break
            time.sleep(0.1)
        cgroup_removed = not pathlib.Path("/sys/fs/cgroup" + cgroup).exists()
        if not gone or not cgroup_removed:
            raise RuntimeError("stopped child unit or cgroup remained loaded")
        result: dict[str, object] = {
            "active_result_generation": True,
            "child_cgroup_removed": cgroup_removed,
            "child_invocation_id": invocation_id,
            "child_pid": pid,
            "child_start_ticks": start_ticks,
            "exact_stop_exit_code": stopped.returncode,
            "result_written_while_loaded": True,
            "schema": "training-maintenance-lifecycle-probe.v1",
            "status": "pass",
            "unit_became_not_found": gone,
        }
        receipt_path.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        os.chmod(receipt_path, 0o600)
        return result
    finally:
        subprocess.run(
            [SYSTEMCTL, "--user", "stop", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        marker.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", required=True, type=pathlib.Path)
    args = parser.parse_args()
    print(json.dumps(run(args.receipt), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
