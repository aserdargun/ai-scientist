"""Keep the isolated prototype patch limited to its seven integration payloads."""

from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
COPY = ROOT / "data/runtime/aos-coexistence/source-current"
EVIDENCE = Path(__file__).resolve().parent
PATCH = EVIDENCE / "aos-lab-external-job-prototype.patch"
PAYLOADS = (
    "database/migrations/0018_lab_external_jobs.sql",
    "schemas/lab_external_job_start.schema.json",
    "schemas/lab_external_run_status.schema.json",
    "schemas/lab_external_verified_report.schema.json",
    "src/aos/desktop_console.py",
    "src/aos/lab_external.py",
    "tests/test_lab_external_jobs.py",
)


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def command(argv, cwd):
    result = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{argv[1:3]} failed: {result.stderr[:500]}")
    return result


def manifest(raw):
    pairs = [line.split("  ", 1) for line in raw.decode().splitlines()]
    result = {name: digest for digest, name in pairs}
    assert len(result) == len(pairs)
    return result


def main():
    initial_patch = PATCH.read_bytes()
    originals = {name: (COPY / name).read_bytes() for name in (*PAYLOADS, "MANIFEST.sha256")}
    staging_inputs = dict(originals)
    previous_path = EVIDENCE / "aos-patch-packaging-review.json"
    if previous_path.exists():
        previous = json.loads(previous_path.read_text())
        if previous["after_patch_sha256"] == sha(initial_patch):
            ordered = "".join(
                f"{digest}  {name}\n"
                for name, digest in sorted(manifest(originals["MANIFEST.sha256"]).items())
            ).encode()
            if sha(ordered) == previous["delivery_manifest_sha256"]:
                staging_inputs["MANIFEST.sha256"] = ordered
    listed = command(["git", "apply", "--numstat", str(PATCH)], ROOT).stdout.splitlines()
    assert {line.split("\t")[2] for line in listed} == set(originals)
    with tempfile.TemporaryDirectory(prefix="aos-patch-review-", dir=ROOT / "data/runtime") as tmp:
        staging = Path(tmp)
        command(["git", "init", "-q"], staging)
        for name, raw in staging_inputs.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        command(["git", "apply", "--reverse", "--check", str(PATCH)], staging)
        command(["git", "apply", "--reverse", str(PATCH)], staging)
        baseline = {name: (staging / name).read_bytes() if (staging / name).exists() else None
                    for name in originals}
        snapshot = json.loads((EVIDENCE / "aos-test-copy-current.json").read_text())
        assert sha(baseline["src/aos/desktop_console.py"]) == (
            snapshot["files"]["src/aos/desktop_console.py"]["sha256"]
        )
        old_manifest = manifest(baseline["MANIFEST.sha256"])
        copy_manifest = manifest(originals["MANIFEST.sha256"])
        inherited_changes = sorted(
            name for name in old_manifest.keys() | copy_manifest.keys()
            if old_manifest.get(name) != copy_manifest.get(name) and name not in PAYLOADS
        )
        desired_manifest = dict(old_manifest)
        desired_manifest.update({name: sha(originals[name]) for name in PAYLOADS})
        # Preserve existing line order, including the parallel team's entries.
        lines = []
        for line in baseline["MANIFEST.sha256"].decode().splitlines(keepends=True):
            _digest, name = line.rstrip("\n").split("  ", 1)
            lines.append(f"{desired_manifest[name]}  {name}\n" if name in PAYLOADS else line)
        lines.extend(f"{desired_manifest[name]}  {name}\n"
                     for name in sorted(set(PAYLOADS) - old_manifest.keys()))
        desired = {**originals, "MANIFEST.sha256": "".join(lines).encode()}
        fragments = []
        for name in sorted(desired):
            before = baseline[name]
            fragments.extend(difflib.unified_diff(
                [] if before is None else before.decode().splitlines(keepends=True),
                desired[name].decode().splitlines(keepends=True),
                fromfile="/dev/null" if before is None else f"a/{name}",
                tofile=f"b/{name}",
            ))
        narrowed = staging / "narrowed.patch"
        narrowed.write_text("".join(fragments))
        command(["git", "apply", "--check", str(narrowed)], staging)
        command(["git", "apply", str(narrowed)], staging)
        roundtrip = {name: sha((staging / name).read_bytes()) == sha(raw)
                     for name, raw in desired.items()}
        assert all(roundtrip.values())
        assert all((COPY / name).read_bytes() == raw for name, raw in originals.items())
        new_patch = narrowed.read_bytes()
    PATCH.write_bytes(new_patch)
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": (
            "Patch packaging only; owned disposable git staging reconstructed the captured "
            "baseline by reversing the old patch, then applied the narrowed patch. Live AOS "
            "was neither read nor changed. The isolated copy's regenerated full manifest is "
            "retained; unrelated inherited metadata changes are excluded from delivery. "
            "This is not full package validation of a patched live AOS checkout."
        ),
        "before_patch_sha256": sha(initial_patch),
        "after_patch_sha256": sha(new_patch),
        "baseline_manifest_sha256": sha(baseline["MANIFEST.sha256"]),
        "delivery_manifest_sha256": sha(desired["MANIFEST.sha256"]),
        "isolated_validation_manifest_sha256": sha(originals["MANIFEST.sha256"]),
        "inherited_manifest_changes_excluded": inherited_changes,
        "payload_sha256": {name: sha(originals[name]) for name in PAYLOADS},
        "roundtrip": roundtrip,
        "only_integration_manifest_entries_changed": all(
            old_manifest.get(name) == desired_manifest.get(name)
            for name in old_manifest.keys() | desired_manifest.keys() if name not in PAYLOADS
        ),
        "source_copy_unchanged": True,
        "all_passed": True,
        "script_sha256": sha(Path(__file__).read_bytes()),
    }
    (EVIDENCE / "aos-patch-packaging-review.json").write_text(json.dumps(record, indent=2) + "\n")
    metadata_path = EVIDENCE / "aos-lab-external-job-prototype.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["patch_sha256"] = sha(new_patch)
    metadata["packaging_review"] = "aos-patch-packaging-review.json"
    restart_gap = (
        "Actual TrajectoryStore restart with an active external job currently raises Unknown "
        "run because typed runtime state is missing; see aos-store-restart-before.json."
    )
    if restart_gap not in metadata["explicit_open_gaps"]:
        metadata["explicit_open_gaps"].append(restart_gap)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps({key: value for key, value in record.items()
                      if key != "payload_sha256"}, indent=2))


if __name__ == "__main__":
    main()
