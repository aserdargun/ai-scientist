"""Fixed supervised Scorer entrypoint for immutable synthetic snapshot installation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from lab.scorer.worker import (
    DEFAULT_DSN_FILE,
    PROJECT_ROOT,
    _secret,
    _try_process_admission_lock,
    verify_systemd_invocation,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-sha256", required=True)
    args = parser.parse_args(argv)
    digest = args.snapshot_sha256
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        parser.error("snapshot must be a SHA-256 identity")
    descriptor = None
    engine = None
    try:
        verify_systemd_invocation(f"swapp-ai-scientist-mode-install-{digest[:32]}.service")
        descriptor = _try_process_admission_lock()
        if descriptor is None:
            print(json.dumps({"state": "capacity_busy"}))
            return 0
        from sqlalchemy import create_engine

        from lab.api.mode_experiments import ModeSnapshotStore
        from lab.scorer.mode_snapshot import install_snapshot

        engine = create_engine(_secret(DEFAULT_DSN_FILE), pool_size=1, max_overflow=0)
        store = ModeSnapshotStore(PROJECT_ROOT / "data/runtime/mode-snapshots")
        result = install_snapshot(engine, store, digest)
        print(json.dumps({"state": "installed", "snapshot_sha256": result["snapshot_sha256"]}))
        return 0
    except Exception as exc:
        print(f"snapshot installation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
