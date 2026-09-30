"""Build a source-frozen sandbox image and pin only after runtime byte parity."""

from __future__ import annotations

import hashlib
import argparse
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(output: Path | None = None) -> None:
    runtime = ROOT / "data/runtime/director-image-review" / str(uuid4())
    context = runtime / "context"
    context.mkdir(parents=True, mode=0o700)
    paths = [
        path for name in ("harness", "lab", "vendor")
        for path in sorted((ROOT / name).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    ]
    paths += [ROOT / "Dockerfile.sandbox", ROOT / "docker/sandbox/requirements.txt"]
    hashes = {str(path.relative_to(ROOT)): digest(path) for path in paths}
    for path in paths:
        destination = context / path.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    evidence = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Runtime byte parity and constrained imports; no scientific acceptance.",
        "source_sha256": hashes,
        "previous_image": (ROOT / "ops/sandbox-image.lock").read_text().strip(),
        "context": str(context.relative_to(ROOT)),
    }
    try:
        command = [
            "docker", "build", "--network=none", "--file", str(context / "Dockerfile.sandbox"),
            "--iidfile", str(runtime / "image.id"), str(context),
        ]
        built = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
        (runtime / "build.stdout").write_text(built.stdout)
        (runtime / "build.stderr").write_text(built.stderr)
        evidence["build_command"] = command
        evidence["build_exit_code"] = built.returncode
        assert built.returncode == 0, "sandbox image build failed; inspect owned build log"
        image = (runtime / "image.id").read_text().strip()
        evidence["image"] = image
        runtime_hashes = {
            name: value for name, value in hashes.items()
            if name.split("/", 1)[0] in {"harness", "lab", "vendor"}
        }
        program = (
            "import hashlib,json,pathlib\n"
            f"expected=json.loads({json.dumps(json.dumps(runtime_hashes))})\n"
            "root=pathlib.Path('/opt/swapp-ai-scientist')\n"
            "assert all(hashlib.sha256((root/name).read_bytes()).hexdigest()==value "
            "for name,value in expected.items())\n"
            "from harness.baselines import build_baseline\n"
            "for name in ('robust_z','iforest','ecod_train_frozen'): build_baseline(name)\n"
            "print(json.dumps({'runtime_files':len(expected),'byte_parity':True}))\n"
        )
        checked = subprocess.run(
            ["docker", "run", "--rm", "-i", "--network=none", "--read-only",
             "--cpus=1", "--memory=512m", "--memory-swap=512m", "--pids-limit=64",
             "--cap-drop=ALL", "--security-opt=no-new-privileges",
             "--env=OMP_NUM_THREADS=1", "--env=OPENBLAS_NUM_THREADS=1",
             image, "python", "-"],
            input=program, capture_output=True, text=True, timeout=60, check=False,
        )
        evidence["parity_exit_code"] = checked.returncode
        evidence["parity_stdout"] = checked.stdout
        evidence["parity_stderr"] = checked.stderr
        evidence["source_unchanged"] = all(digest(ROOT / p) == h for p, h in hashes.items())
        assert checked.returncode == 0, "sandbox runtime byte parity or baseline import failed"
        assert evidence["source_unchanged"], "host sources changed during sandbox build"
        (ROOT / "ops/sandbox-image.lock").write_text(image + "\n")
        evidence["all_passed"] = True
    except Exception as error:
        evidence["error_type"] = type(error).__name__
        evidence["all_passed"] = False
        raise
    finally:
        evidence["script_sha256"] = digest(Path(__file__))
        (output or Path(__file__).with_name("director-final-image-review.json")).write_text(
            json.dumps(evidence, indent=2, allow_nan=False) + "\n"
        )
    print(json.dumps({k: evidence[k] for k in (
        "image", "build_exit_code", "parity_exit_code", "source_unchanged", "all_passed"
    )}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    if arguments.output is not None and arguments.output.exists():
        parser.error("refusing to overwrite existing image evidence")
    main(arguments.output)
