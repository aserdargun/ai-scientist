"""Export the isolated AOS GPU-broker delta against its captured live snapshot.

This script reads only the repository-local capture and merged public-source
copy. It does not read or write the live AOS checkout. It adds the two broker
workers to the copied source manifest while preserving the captured
services/laya inclusion workaround, emits a unified patch, applies it to a
temporary copy of the captured source, and runs the bounded AOS checks.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent
STAGE = ROOT / "data/runtime/aos-coexistence/rebase-f16d3ced7e6d430eb9b2e11913dac720"
BASE = STAGE / "live"
HEAD = STAGE / "merged"
PATCH_PATH = EVIDENCE / "aos-lab-gpu-broker.patch"
RECORD_PATH = EVIDENCE / "aos-lab-gpu-broker-patch-review.json"
BROKER_WORKERS = (
    "services/bonsai/broker_worker.py",
    "services/decider/broker_worker.py",
)
POST_CAPTURE_MODEL_WORKER_FIXES = (
    "services/decider/worker.py",
    "src/aos/reusable_decider.py",
)
TARGETED = (
    "tests/test_reusable_decider.py",
    "tests/test_decider_worker.py",
    "tests/test_lab_external_jobs.py",
    "tests/test_dataset_audit.py",
    "tests/test_vision.py",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha(path.read_bytes())


def run(command: list[str], *, cwd: Path, timeout: int = 240) -> dict[str, object]:
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return {
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def systemd_command(label: str, args: list[str], *, timeout: int = 240) -> dict[str, object]:
    unit = f"swapp-review-aos-gpu-patch-{label}-{uuid4().hex[:12]}.service"
    python = ROOT / "data/runtime/aos-coexistence/.venv/bin/python"
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
        f"--unit={unit}", "--service-type=exec", f"--working-directory={HEAD}",
        "--property=MemoryMax=2G", "--property=MemorySwapMax=0",
        "--property=CPUQuota=100%", "--property=TasksMax=128",
        f"--property=RuntimeMaxSec={timeout}", "--property=TimeoutStopSec=10",
        "--property=KillMode=control-group", "--setenv=OPENBLAS_NUM_THREADS=1",
        "--setenv=OMP_NUM_THREADS=1", f"--setenv=PYTHONPATH={HEAD / 'src'}",
        str(python), *args,
    ]
    record = run(command, cwd=HEAD, timeout=timeout + 30)
    record["unit"] = unit
    return record


def ensure_manifest() -> dict[str, object]:
    manifest = HEAD / "MANIFEST.sha256"
    original_rows = manifest.read_text().splitlines()
    entries = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in original_rows}
    additions: list[str] = []
    refreshed: list[str] = []
    for relative in tuple(entries):
        source = HEAD / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"manifest path is not a regular copied source file: {relative}")
        current = sha_file(source)
        if entries[relative] != current:
            entries[relative] = current
            refreshed.append(relative)
    for relative in BROKER_WORKERS:
        source = HEAD / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"broker worker is not a regular copied source file: {relative}")
        expected = sha_file(source)
        if relative in entries and entries[relative] != expected:
            raise RuntimeError(f"manifest hash mismatch for {relative}")
        if relative not in entries:
            entries[relative] = expected
            additions.append(f"{expected}  {relative}")
    if not additions and not refreshed:
        return {"changed": False, "added": [], "refreshed": [], "services_laya_preserved": any(
            line.endswith("  services/laya/worker.py") for line in original_rows
        )}

    # Preserve the original ordering and membership, refreshing hashes in
    # place. Insert each new path once; ``entries`` also contains additions.
    new_rows = [
        f"{entries[line.split('  ', 1)[1]]}  {line.split('  ', 1)[1]}"
        for line in original_rows
    ]
    for addition in sorted(additions, key=lambda item: item.split("  ", 1)[1]):
        path = addition.split("  ", 1)[1]
        index = next(
            (i for i, line in enumerate(new_rows)
             if line.split("  ", 1)[1] > path),
            len(new_rows),
        )
        new_rows.insert(index, addition)
    if [line.split("  ", 1)[1] for line in new_rows] != sorted(
        line.split("  ", 1)[1] for line in new_rows
    ):
        raise RuntimeError("manifest ordering is not canonical after insertion")
    manifest.write_text("\n".join(new_rows) + "\n")
    return {
        "changed": True,
        "added": additions,
        "refreshed": refreshed,
        "services_laya_preserved": any(
            line.endswith("  services/laya/worker.py") for line in new_rows
        ),
    }


def main() -> None:
    capture = json.loads((STAGE / "record.json").read_text())
    baseline_hashes: dict[str, str] = capture["live_sources"]
    # Revalidate every captured baseline file before using it for patch output.
    for relative, expected in baseline_hashes.items():
        source = BASE / relative
        if not source.is_file() or source.is_symlink() or sha_file(source) != expected:
            raise RuntimeError(f"captured AOS baseline mismatch: {relative}")

    manifest_result = ensure_manifest()
    head_files = {
        path.relative_to(HEAD).as_posix(): path
        for path in HEAD.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and ".venv" not in path.parts
    }
    # Root's capture record predates the final broker worker files. Check every
    # recorded merge hash except the manifest and the explicitly late additions.
    capture_head_hashes: dict[str, str] = capture["post_merge_sha256"]
    late_paths = set(BROKER_WORKERS) | set(POST_CAPTURE_MODEL_WORKER_FIXES) | {"MANIFEST.sha256"}
    post_capture_drift: dict[str, dict[str, str]] = {}
    for relative, expected in capture_head_hashes.items():
        if relative in late_paths:
            path = HEAD / relative
            if path.is_file() and sha_file(path) != expected:
                post_capture_drift[relative] = {
                    "captured_merged_sha256": expected,
                    "final_merged_sha256": sha_file(path),
                }
            continue
        path = HEAD / relative
        if not path.is_file() or sha_file(path) != expected:
            raise RuntimeError(f"merged snapshot changed since capture: {relative}")

    all_paths = sorted(set(baseline_hashes) | set(head_files))
    deleted = [path for path in all_paths if path in baseline_hashes and path not in head_files]
    if deleted:
        raise RuntimeError(f"unexpected deletion from captured AOS source: {deleted[:5]}")
    changed = [
        path for path in all_paths
        if path not in baseline_hashes or sha_file(head_files[path]) != baseline_hashes[path]
    ]
    changed_bytes: dict[str, bytes] = {path: head_files[path].read_bytes() for path in changed}
    patch_bytes = b"".join(
        "".join(difflib.unified_diff(
            (BASE / path).read_text(encoding="utf-8").splitlines(keepends=True)
            if path in baseline_hashes else [],
            changed_bytes[path].decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{path}" if path in baseline_hashes else "/dev/null",
            tofile=f"b/{path}",
        )).encode("utf-8")
        for path in changed
    )
    PATCH_PATH.write_bytes(patch_bytes)

    with tempfile.TemporaryDirectory(prefix="aos-gpu-roundtrip-", dir=ROOT / "data/runtime") as tmp:
        staging = Path(tmp)
        git_init = run(["git", "init", "-q"], cwd=staging)
        for relative in baseline_hashes:
            src = BASE / relative
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
        check = run(["git", "apply", "--check", str(PATCH_PATH)], cwd=staging)
        apply = run(["git", "apply", str(PATCH_PATH)], cwd=staging)
        parity = {
            relative: (staging / relative).is_file()
            and (staging / relative).read_bytes() == changed_bytes[relative]
            for relative in changed
        }
        # Full public-file parity verifies both patched and untouched capture files.
        untouched = {
            relative: (staging / relative).is_file()
            and sha_file(staging / relative) == baseline_hashes[relative]
            for relative in baseline_hashes if relative not in changed
        }

    targeted = systemd_command("targeted", ["-m", "pytest", "-q", *TARGETED])
    package = systemd_command("package", ["scripts/validate_package.py"])
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Captured-live to isolated merged public AOS source delta; no live checkout access or mutation.",
        "baseline": {
            "stage": str(STAGE.relative_to(ROOT)),
            "captured_live_manifest_sha256": capture["live_manifest_sha256"],
            "captured_live_source_file_count": len(baseline_hashes),
            "captured_live_sources_verified": True,
        },
        "head": str(HEAD.relative_to(ROOT)),
        "post_capture_model_worker_source_drift": post_capture_drift,
        "manifest_update": manifest_result,
        "changed_files": changed,
        "changed_file_count": len(changed),
        "patch_path": str(PATCH_PATH.relative_to(ROOT)),
        "patch_sha256": sha(patch_bytes),
        "patch_bytes": len(patch_bytes),
        "baseline_source_sha256": baseline_hashes,
        "head_source_sha256": {path: sha(changed_bytes[path]) for path in changed},
        "git_init": git_init["exit_code"],
        "git_apply_check": check["exit_code"],
        "git_apply": apply["exit_code"],
        "changed_payload_roundtrip_byte_parity": parity,
        "untouched_baseline_byte_parity": untouched,
        "targeted_aos_tests": targeted,
        "package_validation": package,
        "script_sha256": sha_file(Path(__file__)),
    }
    result["all_passed"] = (
        git_init["exit_code"] == 0
        and check["exit_code"] == 0
        and apply["exit_code"] == 0
        and all(parity.values())
        and all(untouched.values())
        and targeted["exit_code"] == 0
        and package["exit_code"] == 0
        and manifest_result["services_laya_preserved"]
    )
    RECORD_PATH.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({
        "all_passed": result["all_passed"], "changed_file_count": len(changed),
        "patch_sha256": result["patch_sha256"],
        "targeted_exit": targeted["exit_code"],
        "package_exit": package["exit_code"],
    }))
    if not result["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
