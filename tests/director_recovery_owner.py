"""Minimal owned dispatch process used by PostgreSQL recovery review tests."""

from __future__ import annotations

import argparse
import os
import stat
import time
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, text

from lab.director.recovery import capture_current_owner


def _dsn(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("recovery review DSN must be private")
    return path.read_text(encoding="utf-8").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True, type=UUID)
    parser.add_argument("--payload-sha256", required=True)
    parser.add_argument("--child-seconds", type=float, default=0.0)
    parser.add_argument("--parent-seconds", type=float, default=0.0)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    engine = create_engine(
        _dsn(root / "data/runtime/postgres/director.dsn"),
        pool_size=1,
        max_overflow=0,
    )
    try:
        owner = capture_current_owner(args.payload_sha256, args.run_id)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO lab.director_run_owners(run_id,payload_sha256,worker_pid,"
                    "worker_start_ticks,worker_boot_id,worker_unit,worker_invocation_id,"
                    "worker_cgroup) "
                    "VALUES (:run_id,:payload_sha256,:worker_pid,:worker_start_ticks,"
                    ":worker_boot_id,"
                    ":worker_unit,:worker_invocation_id,:worker_cgroup)"
                ),
                {
                    "run_id": args.run_id,
                    "payload_sha256": owner.payload_sha256,
                    "worker_pid": owner.worker_pid,
                    "worker_start_ticks": owner.worker_start_ticks,
                    "worker_boot_id": owner.worker_boot_id,
                    "worker_unit": owner.worker_unit,
                    "worker_invocation_id": owner.worker_invocation_id,
                    "worker_cgroup": owner.worker_cgroup,
                },
            )
        if args.child_seconds:
            pid = os.fork()
            if pid == 0:
                os.closerange(0, 3)
                engine.dispose()
                time.sleep(args.child_seconds)
                os._exit(0)
        if args.parent_seconds:
            time.sleep(args.parent_seconds)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
