"""Package the isolated typed AOS changes against the frozen transport copy.

This patch targets the exact source-current test copy, including its recorded
validation manifest. That manifest differs from the delivery manifest in the
older transport patch, so these patch files must not be blindly chained. No
live AOS path is read or modified. Changed-file hunks are applied and byte-checked
in a temporary Git directory; the full package gate is separate evidence.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent
BASE = ROOT / "data/runtime/aos-coexistence/source-current"
HEAD = ROOT / "data/runtime/aos-coexistence/source-typed"
TARGETS = (
    "src/aos/computer.py", "src/aos/contracts.py", "src/aos/dataset_audit.py",
    "src/aos/desktop_console.py", "src/aos/lab_external.py", "src/aos/storage.py",
    "database/migrations/0019_lab_external_job_rebinding.sql",
    "tests/test_dataset_audit.py", "tests/test_lab_external_jobs.py",
    "services/laya/worker.py", "MANIFEST.sha256",
)


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def main() -> None:
    names = list(TARGETS)
    for optional in ("models/browser-manifest.json",):
        if (HEAD / optional).is_file():
            names.append(optional)
    before = {name: (BASE / name).read_bytes() if (BASE / name).is_file() else None for name in names}
    after = {name: (HEAD / name).read_bytes() for name in names}
    changed = [name for name in names if before[name] != after[name]]
    payload = "".join("".join(difflib.unified_diff(
        [] if before[name] is None else before[name].decode().splitlines(keepends=True),
        after[name].decode().splitlines(keepends=True),
        fromfile="/dev/null" if before[name] is None else f"a/{name}", tofile=f"b/{name}",
    )) for name in changed).encode()
    patch = EVIDENCE / "aos-lab-typed-policy.patch"
    patch.write_bytes(payload)
    with tempfile.TemporaryDirectory(prefix="aos-typed-patch-", dir=ROOT / "data/runtime") as directory:
        staging = Path(directory)
        subprocess.run(["git", "init", "-q"], cwd=staging, check=True, capture_output=True)
        for name in changed:
            if before[name] is not None:
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(before[name])
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=staging, check=True, capture_output=True)
        subprocess.run(["git", "apply", str(patch)], cwd=staging, check=True, capture_output=True)
        parity = {name: (staging / name).read_bytes() == after[name] for name in changed}
    unchanged = all((HEAD / name).read_bytes() == after[name] and (
        ((BASE / name).read_bytes() if (BASE / name).is_file() else None) == before[name]) for name in names)
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": __doc__, "related_transport_patch": "aos-lab-api-mapping.patch",
        "related_transport_patch_sha256": sha((EVIDENCE / "aos-lab-api-mapping.patch").read_bytes()),
        "baseline": str(BASE.relative_to(ROOT)), "head": str(HEAD.relative_to(ROOT)),
        "baseline_sha256": {name: None if value is None else sha(value) for name, value in before.items()},
        "payload_sha256": {name: sha(after[name]) for name in changed},
        "changed_files": changed, "patch_sha256": sha(payload),
        "roundtrip_byte_parity": parity, "source_unchanged": unchanged,
        "script_sha256": sha(Path(__file__).read_bytes()),
        "all_passed": unchanged and bool(parity) and all(parity.values()),
    }
    patch.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("all_passed", "patch_sha256", "changed_files")}))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
