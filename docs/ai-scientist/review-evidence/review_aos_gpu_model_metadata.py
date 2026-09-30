"""Read only the AOS model metadata and GPU integration source identities.

No model bytes, private databases or credentials are copied. Stat observations
are not a model artifact verification or a measured memory/capacity result.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AOS = Path("/home/cachyos/aos")
COPY = ROOT / "data/runtime/aos-coexistence/source-gpu"
PUBLIC_FIELDS = (
    "source", "checkpoint_revision", "tokenizer_revision", "projector_revision",
    "code_revision", "release_tag", "weights_file", "projector_file", "context_tokens",
    "gpu_layers", "max_output_tokens", "parallel", "temperature", "reasoning_budget",
)
SOURCE_FILES = (
    "src/aos/decision.py", "src/aos/reusable_decider.py", "src/aos/supervisor.py",
    "src/aos/vision.py", "services/decider/worker.py", "scripts/serve_desktop.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite metadata evidence")
    files = [AOS / "models" / name for name in ("decider-manifest.json", "bonsai-manifest.json")]
    files.extend(base / name for base in (AOS, COPY) for name in SOURCE_FILES)
    before = {str(path): sha(path) for path in files}
    record = {
        "schema": "aos-gpu-model-metadata-review.v1", "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Read-only public model metadata and source SHA comparison; no model launch, "
                 "artifact-byte verification, copied model cache, AOS edit or coexistence acceptance.",
        "models": {}, "integration_sources": {}, "source_sha256": before,
        "script_sha256": sha(Path(__file__)),
    }
    for name in ("decider", "bonsai"):
        path = AOS / "models" / f"{name}-manifest.json"
        data = json.loads(path.read_bytes())
        model_root = Path(data["model_path"])
        artifacts = []
        for filename, expected_hash in sorted(data["model_files"].items()):
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("model manifest contains an invalid relative filename")
            model = model_root / relative
            artifacts.append({
                "filename": filename, "expected_sha256_from_manifest": expected_hash,
                "exists": model.is_file(), "bytes_from_stat": model.stat().st_size if model.is_file() else None,
                "artifact_bytes_verified": False,
            })
        record["models"][name] = {
            "manifest_sha256": before[str(path)],
            "public_metadata": {key: data[key] for key in PUBLIC_FIELDS if key in data},
            "model_files": artifacts, "runtime_file_count": len(data.get("runtime_files", {})),
            "model_cache_copied": False, "runtime_started": False,
        }
    for name in SOURCE_FILES:
        live, isolated = before[str(AOS / name)], before[str(COPY / name)]
        record["integration_sources"][name] = {"live_sha256": live, "isolated_sha256": isolated,
                                               "same_bytes": live == isolated}
    record["sources_unchanged_during_read"] = all(sha(path) == before[str(path)] for path in files)
    record["metadata_read_completed"] = record["sources_unchanged_during_read"]
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"metadata_read_completed": record["metadata_read_completed"],
                      "model_launches": 0, "artifact_bytes_verified": False,
                      "different_integration_sources": [name for name, values in record["integration_sources"].items()
                                                        if not values["same_bytes"]]}))
    return 0 if record["metadata_read_completed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
