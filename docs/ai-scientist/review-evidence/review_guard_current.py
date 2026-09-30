"""Rerun unchanged adversarial guard cases on frozen 0.26 sources.

The historical driver and its receipt stay immutable. Only the output target
and a shared CPU lock between cases differ in this review wrapper.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path

import review_guard_docker as original

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "guard-docker-026-review.json"
BINDING = HERE / "guard-docker-026-wrapper.json"


def main() -> int:
    if OUTPUT.exists() or BINDING.exists():
        raise FileExistsError("refusing to overwrite a frozen guard review")
    if (ROOT / "harness/VERSION").read_text().strip() != "0.26.0":
        raise RuntimeError("review requires the frozen 0.26 source")
    catalog = tuple(original.cases())

    def locked_cases():
        for case in catalog:
            with (ROOT / "data/runtime/parallel-m0/cpu-check.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield case

    # len(cases()) is also used after the loop; retain its sequence contract.
    class LockedCatalog:
        def __iter__(self):
            return locked_cases()

        def __len__(self):
            return len(catalog)

    original.cases = LockedCatalog
    original.EVIDENCE = OUTPUT
    exit_code = original.main()
    record = {
        "schema": "current-guard-review-wrapper.v1",
        "original_driver_sha256": hashlib.sha256(Path(original.__file__).read_bytes()).hexdigest(),
        "wrapper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "output_path": str(OUTPUT.relative_to(ROOT)),
        "output_sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
        "exit_code": exit_code,
        "case_catalog_unchanged": True,
        "cpu_lock": "Held for each case; released between cases for parallel validation.",
        "scope": "Real Docker negative/positive synthetic guard fixtures only. No GPU, DB, LLM, public-data or AOS execution.",
    }
    BINDING.write_text(json.dumps(record, indent=2) + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
