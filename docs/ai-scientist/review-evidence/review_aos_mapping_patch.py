"""Retain the API-mapping adapter as a patch against the captured AOS baseline.

The already reviewed prototype patch supplies the exact unchanged baseline
contexts. This only refreshes its added files and corresponding manifest hashes.
It neither reconstructs nor patches the concurrently edited live AOS checkout.
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
COPY = ROOT / "data/runtime/aos-coexistence/source-current"


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> None:
    prototype = EVIDENCE / "aos-lab-external-job-prototype.patch"
    previous = json.loads((EVIDENCE / "aos-patch-packaging-review.json").read_text())
    original = prototype.read_bytes()
    assert sha(original) == previous["after_patch_sha256"]
    sections: dict[str, list[str]] = {}
    current = None
    lines = original.decode().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            assert lines[index + 1].startswith("+++ b/")
            current = lines[index + 1][6:].strip()
            sections[current] = []
        assert current is not None
        sections[current].append(line)
    expected = previous["payload_sha256"]
    assert set(sections) == set(expected) | {"MANIFEST.sha256"}
    payloads = {name: (COPY / name).read_bytes() for name in expected}
    hashes = {name: sha(payload) for name, payload in payloads.items()}
    changed = [name for name in expected if hashes[name] != expected[name]]
    assert "src/aos/lab_external.py" in changed
    updated = dict(sections)
    manifest = "".join(sections["MANIFEST.sha256"])
    added = []
    for name, section in sections.items():
        if name == "MANIFEST.sha256":
            continue
        if section[0] == "--- /dev/null\n":
            old_payload = "".join(line[1:] for line in section[3:] if line.startswith("+")).encode()
            assert sha(old_payload) == expected[name]
            updated[name] = list(difflib.unified_diff(
                [], payloads[name].decode().splitlines(keepends=True),
                fromfile="/dev/null", tofile=f"b/{name}",
            ))
            added.append(name)
        else:
            # Existing baseline files need a fresh captured baseline if edited.
            assert hashes[name] == expected[name]
        if name in changed:
            before = f"+{expected[name]}  {name}\n"
            after = f"+{hashes[name]}  {name}\n"
            assert manifest.count(before) == 1
            manifest = manifest.replace(before, after)
    updated["MANIFEST.sha256"] = manifest.splitlines(keepends=True)
    payload = "".join("".join(updated[name]) for name in sections).encode()
    path = EVIDENCE / "aos-lab-api-mapping.patch"
    path.write_bytes(payload)
    with tempfile.TemporaryDirectory(prefix="aos-mapping-patch-", dir=ROOT / "data/runtime") as tmp:
        subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, capture_output=True)
        command = ["git", "apply", *[f"--include={name}" for name in added], str(path)]
        subprocess.run(command, cwd=tmp, check=True, capture_output=True)
        parity = {name: sha((Path(tmp) / name).read_bytes()) == hashes[name] for name in added}
        assert all(parity.values())
    numstat = subprocess.run(["git", "apply", "--numstat", str(path)], cwd=ROOT,
                             check=True, capture_output=True, text=True).stdout
    assert {line.split("\t")[2] for line in numstat.splitlines()} == set(sections)
    assert all((COPY / name).read_bytes() == raw for name, raw in payloads.items())
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": (
            "Patch packaging only. New-file sections applied in disposable git staging "
            "and matched isolated source bytes. Existing-file hunks are byte-identical to "
            "the previously reviewed prototype; only added payloads and their manifest "
            "hashes changed. No full current AOS package validation or live application."
        ),
        "baseline_patch_sha256": sha(original),
        "baseline_manifest_sha256": previous["baseline_manifest_sha256"],
        "patch_sha256": sha(payload), "payload_sha256": hashes,
        "changed_payloads": changed, "added_files_roundtrip": parity,
        "existing_file_sections_unchanged": all(
            updated[name] == sections[name] for name in expected if name not in added
        ),
        "targets": sorted(sections), "source_copy_unchanged": True, "all_passed": True,
        "script_sha256": sha(Path(__file__).read_bytes()),
    }
    path.with_suffix(".json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in
                      ("all_passed", "patch_sha256", "changed_payloads", "added_files_roundtrip")}))


if __name__ == "__main__":
    main()
