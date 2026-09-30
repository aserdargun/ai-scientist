"""Read and hash AOS model/runtime artifacts without loading or changing them."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
AOS_MODELS = Path("/home/cachyos/aos/models")


def sha(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def worker(output):
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    started = time.monotonic()
    record = {"schema": "aos-local-model-pin-review.v1", "checked_at": datetime.now(UTC).isoformat(),
              "scope": "Read-only streaming hashes against existing AOS public manifests. "
                       "No model load, copies, code import, service restart, live AOS edit "
                       "or CPU/RAM/VRAM capacity acceptance.",
              "models": {}, "checks": {}, "script_sha256": sha(Path(__file__))}
    try:
        for name in ("decider", "bonsai"):
            manifest_path = AOS_MODELS / f"{name}-manifest.json"
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            entry = {"manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "artifacts": []}
            record["models"][name] = entry
            for files_key, root_key in (("model_files", "model_path"), ("code_files", "code_path"),
                                        ("runtime_files", "runtime_path")):
                if files_key not in manifest:
                    continue
                base = Path(manifest[root_key]).resolve(strict=True)
                if not base.is_relative_to(AOS_MODELS.resolve(strict=True)):
                    raise ValueError("manifest root escapes the AOS model directory")
                for filename, expected in sorted(manifest[files_key].items()):
                    relative = Path(filename)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("manifest contains invalid relative artifact path")
                    path = base / relative
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(base):
                        raise ValueError("model artifact resolves outside its manifest root")
                    before = path.stat()
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("model artifact is not a regular file")
                    actual = sha(path)
                    after = path.stat()
                    unchanged = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                                 before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size,
                                                        after.st_mtime_ns, after.st_ctime_ns)
                    entry["artifacts"].append({"group": files_key, "filename": filename,
                        "bytes": before.st_size, "sha256": actual, "expected_sha256": expected,
                        "matches_manifest": actual == expected, "unchanged_during_read": unchanged})
                    if actual != expected or not unchanged:
                        raise ValueError(f"AOS {name} artifact pin mismatch: {filename}")
            record["checks"][f"{name}_all_artifact_hashes_match"] = bool(entry["artifacts"])
            record["checks"][f"{name}_manifest_unchanged"] = manifest_path.read_bytes() == manifest_bytes
            print(json.dumps({"stage": "verified_artifacts", "model": name,
                              "files": len(entry["artifacts"]),
                              "bytes": sum(row["bytes"] for row in entry["artifacts"])}), flush=True)
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    code = 0 if not record.get("error") and len(record["checks"]) == 4 and all(record["checks"].values()) else 1
    record["elapsed_seconds"] = time.monotonic() - started
    record["worker_exit_code"] = code
    record["process"] = {"pid": os.getpid(), "cgroup": Path("/proc/self/cgroup").read_text().strip()}
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    if args.worker:
        return worker(output)
    unit = f"swapp-review-aos-model-pins-{uuid4().hex}.service"
    command = ["/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
               f"--unit={unit}", "--description=SWAPP read-only AOS model pin review",
               f"--working-directory={ROOT}", "--property=MemoryMax=256M", "--property=MemorySwapMax=0",
               "--property=CPUQuota=100%", "--property=TasksMax=16", "--property=RuntimeMaxSec=180",
               "--property=TimeoutStopSec=5", "--property=KillMode=control-group", "--property=Nice=19",
               "--property=IOSchedulingClass=idle", "--property=UMask=0077",
               "--property=LimitFSIZE=1048576", str(ROOT / ".venv/bin/python"), str(Path(__file__)),
               "--worker", "--output", str(output)]
    completed = subprocess.run(command, timeout=190, check=False)
    summary = {"output": str(output.relative_to(ROOT)), "unit": unit,
               "systemd_run_exit_code": completed.returncode}
    if output.exists():
        record = json.loads(output.read_text())
        summary.update({"checks": record["checks"], "worker_exit_code": record["worker_exit_code"],
                        "elapsed_seconds": record["elapsed_seconds"], "error": record.get("error")})
    # Keep the worker output immutable and record the supervisor's observed exit separately.
    with output.with_suffix(".launch.json").open("x") as stream:
        json.dump({**summary, "command": command}, stream, indent=2)
        stream.write("\n")
    print(json.dumps(summary))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
