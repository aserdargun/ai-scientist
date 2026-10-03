#!/usr/bin/env python3
"""Read back existing AOS artifacts using its pinned native verification code.

Run with the existing AOS interpreter and CUDA_VISIBLE_DEVICES empty, inside a
bounded, Scientist-owned CPU unit. Supply a separately reviewed receipt. This
does not start a model, broker, allocator or service; package versions are not
an attestation of all dependency bytes or permission for GPU admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import runpy
import stat
import sys
import time
from pathlib import Path

SOURCES = frozenset(
    {
        "services/decider/worker.py",
        "services/bonsai/broker_worker.py",
        "services/broker_runtime.py",
        "services/bonsai_projection.py",
    }
)
MANIFESTS = frozenset({"models/decider-manifest.json", "models/bonsai-manifest.json"})


def read_regular(path, limit):
    path = Path(path).absolute()
    if path.resolve(strict=True) != path:
        raise ValueError("Input path contains a symbolic link")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.getuid()}
            or before.st_mode & 0o022
            or before.st_nlink != 1
            or not 0 < before.st_size <= limit
        ):
            raise ValueError("Input is not a trusted bounded regular file")
        raw = os.read(descriptor, limit + 1)
        after = os.fstat(descriptor)
        linked = path.lstat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode")
        if len(raw) != before.st_size or any(
            getattr(before, key) != getattr(item, key) for item in (after, linked) for key in fields
        ):
            raise ValueError("Input changed during readback")
        return raw
    finally:
        os.close(descriptor)


def verify_inputs(root, expected):
    result = {}
    for name, checksum in expected.items():
        raw = read_regular(root / name, 256 * 1024)
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise ValueError("Reviewed AOS input changed: " + name)
        result[name] = raw
    return result


def verify(args):
    if os.environ.get("CUDA_VISIBLE_DEVICES") != "":
        raise ValueError("Explicitly disabled CUDA visibility is required")
    raw = read_regular(args.receipt, 256 * 1024)
    if hashlib.sha256(raw).hexdigest() != args.expected_receipt_sha256:
        raise ValueError("Independent receipt hash differs")
    receipt = json.loads(raw)
    if (
        set(receipt) != {"schema", "aos_root", "files"}
        or receipt["schema"] != "aos-model-environment-review.v1"
        or type(receipt["files"]) is not dict
        or set(receipt["files"]) != SOURCES | MANIFESTS
    ):
        raise ValueError("Exact reviewed worker and manifest receipt required")
    root = Path(receipt["aos_root"])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("Actual AOS source root must be explicit and canonical")
    before = verify_inputs(root, receipt["files"])
    started = time.monotonic()
    decider = json.loads(before["models/decider-manifest.json"])
    bonsai = json.loads(before["models/bonsai-manifest.json"])
    decider_worker = runpy.run_path(str(root / "services/decider/worker.py"))
    bonsai_worker = runpy.run_path(str(root / "services/bonsai/broker_worker.py"))
    decider_started = time.monotonic()
    decider_worker["verify_environment"](decider)
    decider_seconds = time.monotonic() - decider_started
    bonsai_started = time.monotonic()
    bonsai_worker["verify_manifest"](bonsai)
    projection_verified = bonsai_worker["verify_projection_pin"](bonsai)
    bonsai_seconds = time.monotonic() - bonsai_started
    after = verify_inputs(root, receipt["files"])
    if before != after or "torch" in sys.modules:
        raise ValueError("Inputs changed or verification unexpectedly imported torch")
    return {
        "schema": "aos-model-environment-readback.v1",
        "receipt_sha256": args.expected_receipt_sha256,
        "interpreter": sys.executable,
        "decider_model_files": len(decider["model_files"]),
        "decider_code_files": len(decider["code_files"]),
        "decider_dependency_versions": len(decider["dependencies"]),
        "bonsai_model_files": len(bonsai["model_files"]),
        "bonsai_runtime_files": len(bonsai["runtime_files"]),
        "bonsai_native_libraries": len(bonsai["native_libraries"]),
        "bonsai_projection_verified": projection_verified,
        "decider_seconds": decider_seconds,
        "bonsai_seconds": bonsai_seconds,
        "elapsed_seconds": time.monotonic() - started,
        "source_and_manifests_unchanged": True,
        "artifact_bytes_verified": True,
        "dependency_versions_verified": True,
        "all_dependency_bytes_verified": False,
        "model_instantiated": False,
        "gpu_acquired": False,
        "admission_allowed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-receipt-sha256", required=True)
    parser.add_argument("--closure-input", type=Path)
    parser.add_argument("--expected-closure-input-sha256")
    parser.add_argument("--closure-output", type=Path)
    args = parser.parse_args()
    closure_options = (args.closure_input, args.expected_closure_input_sha256, args.closure_output)
    if any(value is not None for value in closure_options):
        if any(value is None for value in closure_options):
            parser.error("All three closure options are required together")
        result = build_native_receipt(args)
    else:
        result = verify(args)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


def build_native_receipt(args):
    """Bracket fresh verification with identity snapshots for a real live binding."""
    raw = read_regular(args.closure_input, 512 * 1024)
    if hashlib.sha256(raw).hexdigest() != args.expected_closure_input_sha256:
        raise ValueError("Independent closure-input receipt hash differs")
    value = json.loads(raw)
    if (
        type(value) is not dict
        or set(value) != {"schema", "reviewed_bindings", "requirements", "source_inputs"}
        or value["schema"] != "aos-native-artifact-verification-input.v1"
        or type(value["source_inputs"]) is not dict
    ):
        raise ValueError("Exact independently provisioned live closure input required")
    scientist_root = Path(__file__).resolve().parents[1]
    helper = scientist_root / "scripts/aos_native_artifact_receipts.py"
    for path in (helper, Path(__file__).absolute()):
        expected = value["source_inputs"].get(str(path))
        if (
            expected is None
            or hashlib.sha256(read_regular(path, 8 * 1024**2)).hexdigest() != expected
        ):
            raise ValueError("Native receipt producer source is not independently pinned")
    sys.path.insert(0, str(scientist_root))
    from scripts.aos_native_artifact_receipts import MAX_RECEIPT_BYTES, build_receipt, canonical

    output = args.closure_output.absolute()
    allowed = scientist_root / "data/runtime"
    if (
        output.exists()
        or output.is_symlink()
        or not output.parent.resolve(strict=True).is_relative_to(allowed.resolve(strict=True))
        or output.parent.resolve(strict=True) != output.parent
    ):
        raise ValueError("New receipt output in Scientist-owned runtime directory required")
    receipt = build_receipt(
        value["reviewed_bindings"],
        value["requirements"],
        value["source_inputs"],
        lambda: verify(args),
        validity_seconds=300,
    )
    body = canonical(receipt)
    if len(body) > MAX_RECEIPT_BYTES:
        raise ValueError("Native closure receipt exceeds configured provider bound")
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        # A failed write cannot become authority: its independently returned hash
        # is unavailable and the file is retained for diagnosis.
        raise
    return {
        "schema": "aos-native-artifact-verification-output.v1",
        "receipt_sha256": hashlib.sha256(body).hexdigest(),
        "native_readback": receipt["native_readback"],
        "before_sha256": receipt["before_sha256"],
        "after_sha256": receipt["after_sha256"],
        "verified_boottime": receipt["verified_boottime"],
        "expires_boottime": receipt["expires_boottime"],
        "admission_allowed": False,
    }


if __name__ == "__main__":
    main()
