#!/usr/bin/env python3
"""Resolve and re-verify the current process as an exact run-bound Lab unit."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from lab.llm.gpu_scheduler import SystemdPrincipalResolver, _process_cgroup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lab-unit", required=True)
    parser.add_argument("--aos-unit", default="swapp-aos-gpu-handoff-unused.service")
    args = parser.parse_args()
    expected = r"swapp-ai-scientist-director-dispatch-[0-9a-f]{32}\.service"
    if re.fullmatch(expected, args.lab_unit) is None:
        raise SystemExit("Lab unit is not the exact canonical run-bound dispatch form")
    resolver = SystemdPrincipalResolver({"aos": args.aos_unit, "lab": args.lab_unit})
    receipt = resolver.resolve("lab")
    if not resolver.verify(receipt):
        raise SystemExit("resolved Lab owner receipt failed the second identity check")
    result = {
        "schema": "lab-director-systemd-principal-probe.v1",
        "owner": receipt.owner,
        "unit": receipt.unit,
        "pid": receipt.identity.pid,
        "start_ticks": receipt.identity.start_ticks,
        "boot_id": receipt.identity.boot_id,
        "invocation_id": receipt.invocation_id,
        "control_group": _process_cgroup(os.getpid()),
        "verified": True,
        "database_opened": False,
        "gpu_requested": False,
    }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
