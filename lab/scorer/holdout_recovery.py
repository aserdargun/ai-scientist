"""Fixed-argv Scorer worker that safely reconciles one dead holdout attempt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from lab.scorer.holdout import recover_holdout_reservation, recover_holdout_run
from lab.scorer.worker import DEFAULT_DSN_FILE, _secret, verify_systemd_invocation


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recover exact holdout worker generations")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--reservation-id", type=UUID)
    target.add_argument("--run-id", type=UUID)
    parser.add_argument("--recovery-unit-id", required=True, type=UUID)
    parser.add_argument("--total-seconds", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.total_seconds <= 120:
        raise ValueError("holdout recovery budget must be 1..120 seconds")
    unit = f"swapp-ai-scientist-scorer-{args.recovery_unit_id.hex}.service"
    verify_systemd_invocation(unit)
    engine = create_engine(
        _secret(Path(DEFAULT_DSN_FILE)), pool_size=1, max_overflow=0, pool_timeout=5
    )
    try:
        if args.reservation_id is not None:
            state = recover_holdout_reservation(
                engine, args.reservation_id, timeout_seconds=args.total_seconds
            ).state
            result_identity = {"reservation_id": str(args.reservation_id)}
        else:
            if args.run_id is None:
                raise ValueError("holdout recovery target is missing")
            state = recover_holdout_run(engine, args.run_id, timeout_seconds=args.total_seconds)
            result_identity = {"run_id": str(args.run_id)}
        print(
            json.dumps(
                {**result_identity, "state": state},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:
        print(f"holdout-recovery-error-type={type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
