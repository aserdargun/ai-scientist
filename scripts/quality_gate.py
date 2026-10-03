"""Run the required quality tools and preserve each real exit code."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_PATH = ROOT / "docs/ai-scientist/evidence/quality-gate-latest.json"

COMMANDS: tuple[tuple[str, ...], ...] = (
    ("ruff", "check", "."),
    ("pylint", "harness", "lab"),
    ("bandit", "-q", "-r", "harness", "lab"),
    ("pytest", "-m", "not gpu and not live"),
    (
        "mypy",
        "--strict",
        "harness",
        "lab/referee",
        "lab/analytics",
        "lab/operating_modes",
        "lab/llm",
        "lab/sandbox",
        "lab/api",
        "lab/db",
        "lab/director",
        "lab/scorer",
        "lab/training",
        "lab/cli.py",
        "lab/reporting.py",
        "lab/replay.py",
    ),
)


def _run_command(command: Sequence[str]) -> dict[str, object]:
    """Capture complete output while printing it and preserving the child exit status."""
    print(f"\n$ {' '.join(command)}", flush=True)
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    print(f"exit_code={result.returncode}", flush=True)
    return {
        "command": list(command),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _wheel_smoke() -> list[dict[str, object]]:
    """Build a wheel and import the vendored metric solely from that artifact."""
    records: list[dict[str, object]] = []
    with TemporaryDirectory(prefix="swapp-ai-scientist-wheel-") as temporary:
        wheel_dir = Path(temporary)
        build_command = ("uv", "build", "--wheel", "--out-dir", str(wheel_dir))
        build = _run_command(build_command)
        records.append(build)
        if build["exit_code"] != 0:
            return records
        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            records.append(
                {
                    "command": ["wheel-count"],
                    "exit_code": 1,
                    "stdout": f"expected one wheel, found {len(wheels)}",
                    "stderr": "",
                }
            )
            return records
        wheel = wheels[0]
        expected = {
            "vendor/__init__.py",
            "vendor/tsb_ad_eval/__init__.py",
            "vendor/tsb_ad_eval/basic_metrics.py",
            "vendor/tsb_ad_eval/basic_metrics.pyi",
            "vendor/tsb_ad_eval/NOTICE.md",
            "vendor/tsb_ad_eval/LICENSE",
            "lab/sandbox/image.lock",
            "lab/llm/aos_profile_output.py",
            "lab/llm/aos_control_contract_v2.py",
            "lab/llm/aos_evidence_transport.py",
            "lab/llm/aos_retained_evidence_transport.py",
            "lab/llm/aos_no_admission_observation.py",
            "lab/llm/aos_no_admission_store.py",
            "lab/llm/aos_no_admission_providers.py",
            "lab/llm/aos_no_admission_recovery.py",
            "lab/llm/aos_no_admission_recovery_providers.py",
            "lab/llm/contracts/no_admission_observation_v1/observation.schema.json",
            "lab/llm/contracts/no_admission_observation_recovery_v1/recovery.schema.json",
            "lab/llm/contracts/profile_output_v2/bundle.json",
            "lab/llm/contracts/control_v2/bundle.json",
        }
        expected.update(
            "lab/llm/contracts/profile_output_v2/" + name
            for name in (
                "decider-response.template.schema.json",
                "decider-usage.schema.json",
                "bonsai-response.schema.json",
                "bonsai-usage.schema.json",
                "bonsai-recovery-content.schema.json",
                "bonsai-vision-content.schema.json",
                "result.schema.json",
                "bonsai-raw.schema.json",
            )
        )
        expected.update(
            "lab/llm/contracts/control_v2/" + name
            for name in (
                "admission-binding.schema.json",
                "capability.schema.json",
                "cleanup-grant.schema.json",
                "legacy-terminal.schema.json",
                "request.schema.json",
                "response.template.schema.json",
                "terminal-evidence.schema.json",
                "terminal.schema.json",
            )
        )
        with ZipFile(wheel) as archive:
            missing = expected.difference(archive.namelist())
        if missing:
            records.append(
                {
                    "command": ["wheel-content"],
                    "exit_code": 1,
                    "stdout": f"missing wheel members: {sorted(missing)}",
                    "stderr": "",
                }
            )
            return records
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(wheel)
        smoke_command = (
            sys.executable,
            "-c",
            "from harness.metrics import vus_metrics; "
            "from vendor.tsb_ad_eval import basic_metrics; "
            "from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE; "
            "from lab.sandbox.evaluation import run_guarded_seed_evaluation; "
            "print('wheel imports ok:', basic_metrics.__file__, DEFAULT_SANDBOX_IMAGE)",
        )
        smoke = subprocess.run(
            smoke_command,
            cwd=temporary,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        print(f"\n$ wheel import smoke ({wheel.name})", flush=True)
        if smoke.stdout:
            print(smoke.stdout, end="", flush=True)
        if smoke.stderr:
            print(smoke.stderr, end="", file=sys.stderr, flush=True)
        print(f"exit_code={smoke.returncode}", flush=True)
        records.append(
            {
                "command": list(smoke_command),
                "exit_code": smoke.returncode,
                "stdout": smoke.stdout,
                "stderr": smoke.stderr,
                "wheel": wheel.name,
                "members": sorted(expected),
            }
        )
    return records


def run(commands: Sequence[Sequence[str]] = COMMANDS) -> int:
    """Run all static/test/package gates and persist exact command results."""
    failed = False
    records: list[dict[str, object]] = []
    for command in commands:
        record = _run_command(command)
        records.append(record)
        failed = failed or record["exit_code"] != 0
    wheel_records = _wheel_smoke()
    records.extend(wheel_records)
    failed = failed or any(record["exit_code"] != 0 for record in wheel_records)
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(
            {
                "schema": "quality_gate.v1",
                "created_at": datetime.now(UTC).isoformat(),
                "python": sys.version,
                "runtime_packages": {
                    name: metadata.version(name)
                    for name in ("numpy", "scipy", "scikit-learn", "pyarrow")
                },
                "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
                "overall_exit_code": int(failed),
                "commands": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
