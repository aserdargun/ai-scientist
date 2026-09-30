"""Bounded, model-free probe for the systemd worker import root."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-root", required=True, type=pathlib.Path)
    args = parser.parse_args()

    import lab.training.worker as worker
    from lab.training.maintenance import GPU_RUNTIME, PROJECT_ROOT
    from lab.training.runtime_paths import validated_gpu_runtime_database

    expected_root = args.expected_root.resolve(strict=True)
    module_path = pathlib.Path(worker.__file__).resolve(strict=True)
    module_root = expected_root / "lab/training/worker.py"
    if module_path != module_root:
        raise RuntimeError("systemd imported the worker from another source root")
    unexpected_libraries = sorted(
        name for name in ("torch", "transformers", "unsloth", "vllm") if name in sys.modules
    )
    if unexpected_libraries:
        raise RuntimeError("worker import unexpectedly loaded a model library")
    gpu_runtime_root = PROJECT_ROOT / "data/runtime/gpu"
    made_gpu_runtime = not gpu_runtime_root.exists()
    gpu_runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if made_gpu_runtime:
        gpu_runtime_root.chmod(0o700)
    test_database = gpu_runtime_root / "arbiter.sqlite3"
    validated_gpu_runtime_database(
        test_database,
        project_root=PROJECT_ROOT,
        gpu_runtime_root=GPU_RUNTIME,
    )
    if test_database.exists() or test_database.is_symlink():
        raise RuntimeError("path-only DB validation unexpectedly touched the database")
    if made_gpu_runtime:
        gpu_runtime_root.rmdir()

    shared_database = pathlib.Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
    if shared_database.parent.exists():
        try:
            validated_gpu_runtime_database(
                shared_database,
                project_root=PROJECT_ROOT,
                gpu_runtime_root=GPU_RUNTIME,
            )
            shared_status = "provisioned_and_valid"
        except ValueError:
            shared_status = "provisioned_but_rejected"
    else:
        try:
            validated_gpu_runtime_database(
                shared_database,
                project_root=PROJECT_ROOT,
                gpu_runtime_root=GPU_RUNTIME,
            )
        except ValueError:
            shared_status = "not_provisioned_rejected"
        else:
            raise RuntimeError("absent shared DB directory unexpectedly validated")
    print(
        json.dumps(
            {
                "cwd": str(pathlib.Path.cwd()),
                "model_libraries_imported": unexpected_libraries,
                "module_path": str(module_path),
                "private_project_database_path_valid": True,
                "shared_runtime_database_status": shared_status,
                "schema": "training-runtime-import-probe.v1",
                "status": "pass",
                "sys_executable": sys.executable,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
