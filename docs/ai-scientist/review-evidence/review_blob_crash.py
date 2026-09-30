"""Kill only our writer after fsync and inspect recovery of its private blob root."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from lab.scorer.jobs import store_candidate_artifact

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def payload(offset):
    return json.dumps({"schema": "candidate-scores.v1", "sample_indices": [0, 1],
                       "scores": [float(offset), float(offset + 1)]}).encode()


def child(blob_root, marker):
    original_fsync = os.fsync

    def pause_after_fsync(descriptor):
        original_fsync(descriptor)
        marker.write_text(json.dumps({"pid": os.getpid()}))
        time.sleep(30)

    os.fsync = pause_after_fsync
    store_candidate_artifact(payload(1), artifact_root=blob_root)


def main():
    source_path = ROOT / "lab/scorer/jobs.py"
    before = hashlib.sha256(source_path.read_bytes()).hexdigest()
    record = {
        "schema": "blob-writer-crash-review.v1", "checked_at": datetime.now(UTC).isoformat(),
        "scope": "A real SIGKILL of our own writer after file fsync, private temporary artifact root, two tiny numeric documents; no DB, AOS, GPU, shared blob store or public data changes.",
        "source_sha256": {"lab/scorer/jobs.py": before},
        "command": ".venv/bin/python docs/ai-scientist/review-evidence/review_blob_crash.py",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with tempfile.TemporaryDirectory(prefix="owned-blob-crash-review-", dir=ROOT / "data/runtime") as directory:
        base = Path(directory)
        blob_root = base / "blobs"
        marker = base / "ready.json"
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "writer",
                                    str(blob_root), str(marker)], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not marker.exists():
                raise RuntimeError("owned writer did not reach its fsync barrier")
            observed = json.loads(marker.read_text())
            if observed["pid"] != process.pid:
                raise RuntimeError("owned writer identity did not match")
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=5)
            record["writer_exit_code"] = process.returncode
            leftovers = list(blob_root.glob("*/*.tmp"))
            record["fsynced_temporary_files_after_kill"] = len(leftovers)
            record["temporary_bytes"] = sum(p.stat().st_size for p in leftovers)
            try:
                digest = store_candidate_artifact(payload(2), artifact_root=blob_root)
                record["next_distinct_write"] = {"state": "accepted", "sha256": digest}
            except Exception as exc:
                record["next_distinct_write"] = {"state": "rejected", "error_type": type(exc).__name__,
                                                "error_code": str(exc) if isinstance(exc, ValueError) else "unexpected"}
            record["new_write_blocked_by_owned_orphan"] = (
                process.returncode == -signal.SIGKILL and len(leftovers) == 1 and
                record["next_distinct_write"] == {"state": "rejected", "error_type": "ValueError",
                    "error_code": "unexpected artifact entry in content-addressed store"}
            )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    record["private_fixture_cleaned"] = not base.exists()
    record["source_unchanged"] = before == hashlib.sha256(source_path.read_bytes()).hexdigest()
    record["violation_reproduced"] = all(record.get(key, False) for key in (
        "new_write_blocked_by_owned_orphan", "private_fixture_cleaned", "source_unchanged"))
    (HERE / "blob-writer-crash-before.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0 if record["violation_reproduced"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "writer":
        child(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        raise SystemExit(main())
