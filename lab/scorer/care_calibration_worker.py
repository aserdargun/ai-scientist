"""Fixed-argv Scorer worker for one durable Farm B calibration cell claim."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from uuid import UUID


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one reserved Farm B baseline cell")
    parser.add_argument("--calibration-id", required=True, type=UUID)
    parser.add_argument("--claim-id", required=True, type=UUID)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--total-seconds", required=True, type=int)
    return parser


def _worker_identity() -> tuple[str, str]:
    raw = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
    close = raw.rfind(")")
    fields = raw[close + 2 :].split()
    if close < 0 or len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("CARE worker process identity is unavailable")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip().lower()
    if re.fullmatch(r"[0-9a-f-]{36}", boot) is None:
        raise RuntimeError("CARE worker boot identity is malformed")
    return fields[19], boot


def _emit(args: argparse.Namespace, state: str) -> None:
    print(
        json.dumps(
            {
                "calibration_id": str(args.calibration_id),
                "claim_id": str(args.claim_id),
                "generation": args.generation,
                "state": state,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _acquire_process_admission_lock(
    deadline_monotonic: float,
    try_lock: Callable[[], int | None],
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int | None:
    """Wait for host P=1 only within this already charged claim's deadline."""
    while True:
        remaining = deadline_monotonic - monotonic()
        if remaining <= 0:
            return None
        process_lock = try_lock()
        if process_lock is not None:
            return process_lock
        sleep(min(0.1, remaining))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.total_seconds <= 100 or args.generation < 1:
        raise ValueError("CARE worker deadline or generation is invalid")
    deadline = time.monotonic() + args.total_seconds
    unit = f"swapp-ai-scientist-scorer-{args.claim_id.hex}.service"
    from lab.scorer.worker import (
        DEFAULT_DSN_FILE,
        _secret,
        _try_process_admission_lock,
        verify_systemd_invocation,
    )

    invocation = verify_systemd_invocation(unit)
    process_lock = _acquire_process_admission_lock(deadline, _try_process_admission_lock)
    if process_lock is None:
        _emit(args, "capacity_busy")
        return 75
    engine = None
    try:
        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError("CARE worker startup exhausted its claim deadline")
        start_ticks, boot_id = _worker_identity()
        from sqlalchemy import create_engine, text

        from lab.scorer.care_calibration import (
            bind_care_baseline_worker,
            run_care_baseline_claim,
        )

        engine = create_engine(
            _secret(Path(DEFAULT_DSN_FILE)), pool_size=1, max_overflow=0, pool_timeout=5
        )
        bind_care_baseline_worker(
            engine,
            args.claim_id,
            args.generation,
            pid=os.getpid(),
            start_ticks=start_ticks,
            boot_id=boot_id,
            unit=invocation.unit,
            invocation_id=invocation.invocation_id,
            cgroup=invocation.control_group,
        )
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT c.calibration_id,c.generation,c.state,c.worker_invocation_id,"
                        "c.worker_unit,c.worker_cgroup,control.state AS control_state "
                        "FROM scorer.care_baseline_calibration_claims c "
                        "JOIN scorer.care_baseline_calibration_control control "
                        "USING(calibration_id) "
                        "WHERE c.claim_id=:claim_id"
                    ),
                    {"claim_id": args.claim_id},
                )
                .mappings()
                .one_or_none()
            )
        if (
            row is None
            or row["calibration_id"] != args.calibration_id
            or row["generation"] != args.generation
            or row["state"] != "running"
            or row["control_state"] != "running"
            or row["worker_invocation_id"] != invocation.invocation_id
            or row["worker_unit"] != invocation.unit
            or row["worker_cgroup"] != invocation.control_group
        ):
            raise RuntimeError("CARE worker binding does not match the active generation")

        remaining = int(deadline - time.monotonic())
        if remaining < 1:
            raise TimeoutError("CARE worker binding exhausted its claim deadline")
        import stat

        from lab.sandbox.docker_runner import (
            DEFAULT_SANDBOX_IMAGE,
            PROJECT_ROOT,
            LocalDockerRunner,
            SandboxProfile,
        )

        work_root = PROJECT_ROOT / "data/runtime/care-calibration" / args.claim_id.hex
        if work_root.is_symlink():
            raise RuntimeError("CARE calibration worker directory cannot be a symlink")
        work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = work_root.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError("CARE calibration worker directory must be private")
        runner = LocalDockerRunner(
            image=DEFAULT_SANDBOX_IMAGE,
            work_root=work_root,
            profile=SandboxProfile(
                memory_bytes=2 * 1024**3,
                cpus=1.0,
                pids=64,
                timeout_seconds=min(90, remaining),
            ),
        )
        run_care_baseline_claim(
            engine,
            runner,
            calibration_id=args.calibration_id,
            claim_id=args.claim_id,
            generation=args.generation,
            invocation_id=invocation.invocation_id,
            deadline_monotonic=deadline,
        )
        _emit(args, "succeeded")
        return 0
    except Exception as exc:
        print(f"care-calibration-worker-error-type={type(exc).__name__}", file=sys.stderr)
        _emit(args, "failed")
        return 1
    finally:
        if engine is not None:
            engine.dispose()
        os.close(process_lock)


if __name__ == "__main__":
    raise SystemExit(main())
