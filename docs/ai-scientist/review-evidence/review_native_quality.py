"""Run the mandatory full quality gate in a bounded, owned systemd service."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]


def sources() -> dict[str, str]:
    paths = [path for directory in ("harness", "lab", "vendor", "tests", "scripts")
             for path in (ROOT / directory).rglob("*.py") if "__pycache__" not in path.parts]
    paths += [ROOT / name for name in ("harness/VERSION", "pyproject.toml", "uv.lock",
                                      "ops/sandbox-image.lock", "Dockerfile.sandbox",
                                      "docker/sandbox/requirements.txt")]
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def main(label: str) -> int:
    if not label or not all(c.isalnum() or c == "-" for c in label):
        raise ValueError("label must contain only letters, numbers and hyphens")
    archive = ROOT / "docs/ai-scientist/evidence" / f"quality-gate-{label}.json"
    receipt = Path(__file__).with_name(f"{label}-quality-command.json")
    if archive.exists() or receipt.exists():
        raise ValueError("refusing to overwrite existing quality evidence")
    identifier = uuid4().hex
    private = ROOT / "data/runtime/native-quality-review" / identifier
    private.mkdir(parents=True, mode=0o700)
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
        f"--unit=swapp-review-native-quality-{identifier}.service", "--service-type=exec",
        f"--working-directory={ROOT}", "--property=MemoryMax=3G",
        "--property=MemorySwapMax=0", "--property=CPUQuota=200%",
        "--property=TasksMax=128", "--property=RuntimeMaxSec=300",
        "--property=TimeoutStopSec=10", "--property=KillMode=control-group",
        "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1",
        "--setenv=MKL_NUM_THREADS=1",
        f"--setenv=PATH={ROOT}/.venv/bin:/home/cachyos/.local/bin:/usr/local/bin:/usr/bin:/bin",
        str(ROOT / ".venv/bin/python"), "scripts/quality_gate.py",
    ]
    before = sources()
    started_ns = time.time_ns()
    with (private / "gate.log").open("xb") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                check=False, timeout=330)
    latest = ROOT / "docs/ai-scientist/evidence/quality-gate-latest.json"
    fresh = latest.exists() and latest.stat().st_mtime_ns >= started_ns
    document = json.loads(latest.read_text()) if fresh else {}
    if fresh:
        with archive.open("xb") as output:
            output.write(latest.read_bytes())
    after = sources()
    checks = {
        "process_exit_zero": result.returncode == 0,
        "fresh_gate_record": fresh,
        "seven_commands_exit_zero": len(document.get("commands", [])) == 7 and all(
            row["exit_code"] == 0 for row in document.get("commands", [])),
        "record_overall_exit_zero": document.get("overall_exit_code") == 0,
        "source_unchanged": before == after,
    }
    record = {
        "command": command, "exit_code": result.returncode,
        "runtime_directory": str(private.relative_to(ROOT)),
        "source_sha256_before": before, "source_sha256_after": after,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "gate_archive": str(archive.relative_to(ROOT)) if fresh else None,
        "gate_sha256": hashlib.sha256(archive.read_bytes()).hexdigest() if fresh else None,
        "checks": checks, "all_passed": all(checks.values()),
    }
    with receipt.open("x") as output:
        json.dump(record, output, indent=2)
        output.write("\n")
    print(json.dumps({"receipt": str(receipt.relative_to(ROOT)), "checks": checks,
                      "commands": [{"name": row["command"][0], "exit_code": row["exit_code"]}
                                   for row in document.get("commands", [])]}))
    return 0 if record["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    sys.exit(main(parser.parse_args().label))
