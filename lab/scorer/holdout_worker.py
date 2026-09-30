"""Fixed-argv Scorer process for one private holdout reservation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from lab.scorer.holdout import evaluate_holdout_reservation
from lab.scorer.worker import DEFAULT_DSN_FILE, _secret


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one reserved private holdout check")
    parser.add_argument("--reservation-id", required=True, type=UUID)
    parser.add_argument("--admitted-generation", required=True, type=int)
    parser.add_argument("--execution-sha256", required=True)
    parser.add_argument("--total-seconds", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    engine = create_engine(
        _secret(Path(DEFAULT_DSN_FILE)), pool_size=1, max_overflow=0, pool_timeout=20
    )
    try:
        result = evaluate_holdout_reservation(
            engine,
            reservation_id=args.reservation_id,
            total_seconds=args.total_seconds,
            admitted_generation=args.admitted_generation,
            execution_sha256=args.execution_sha256,
        )
        print(
            json.dumps(
                {
                    "reservation_id": str(args.reservation_id),
                    "state": result["state"],
                    "bit": result["bit"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception as exc:
        # Keep diagnostics categorical: worker errors may contain private metrics,
        # source fragments, or dataset values and must never be emitted to Director.
        print(f"holdout-worker-error-type={type(exc).__name__}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "reservation_id": str(args.reservation_id),
                    "state": "failed",
                    "bit": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
