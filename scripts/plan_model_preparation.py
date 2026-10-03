"""Print inert model readiness or a hash-bound adapter plan; never runs a model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.training.model_plans import (  # noqa: E402 # pylint: disable=wrong-import-position
    AdapterPlanRequest,
    build_adapter_plan,
    catalog_readiness,
    read_document,
)


def main() -> int:
    """Read operator input files and print JSON without DB or service writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    plan = commands.add_parser("adapter-plan")
    for name in (
        "request",
        "package",
        "base-identity",
        "runtime-lock",
        "permission-evidence",
        "evaluation-policy",
        "promotion-policy",
    ):
        plan.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "catalog":
            result = catalog_readiness()
        else:
            request = AdapterPlanRequest.model_validate_json(read_document(args.request))
            result = build_adapter_plan(
                request,
                package=args.package,
                base_identity=args.base_identity,
                runtime_lock=args.runtime_lock,
                permission_evidence=args.permission_evidence,
                evaluation_policy=args.evaluation_policy,
                promotion_policy=args.promotion_policy,
            )
    except (OSError, ValueError) as exc:
        print(f"Model preparation rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
