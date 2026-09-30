"""One-time independent golden fixtures from the untouched TSB-AD 1.5 wheel.

Run outside the application's dependency graph:
OPENBLAS_NUM_THREADS=2 uv run --no-project --python 3.12 \
  --with numpy==1.26.4 --with scipy==1.13.1 --with scikit-learn==1.5.2 \
  docs/ai-scientist/review-evidence/build_tsb_oracle.py

Never regenerate these values to accommodate a production implementation.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "tsb-ad-oracle"
WHEEL = Path("/tmp/ai-scientist-review-TSB_AD-1.5-py3-none-any.whl")
WHEEL_SHA = "4db218b330e8daf845447a714e5367d934d41b67bba4d2310dc41225d962127f"
ORACLE_MEMBER = "TSB_AD/evaluation/basic_metrics.py"
URL = "https://files.pythonhosted.org/packages/2c/96/c82d82c21802035a09af7e0b933c69ff5613295dd6a3489d9256120cd02a/TSB_AD-1.5-py3-none-any.whl"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if (OUTPUT / "golden.json").exists():
        raise SystemExit("Golden values already exist; refusing regeneration")
    if np.__version__ != "1.26.4":
        raise SystemExit("Oracle requires numpy 1.26.4, isolated from application numpy 2")
    if sha(WHEEL) != WHEEL_SHA:
        raise SystemExit("Upstream wheel digest mismatch")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(WHEEL) as archive:
        source = archive.read(ORACLE_MEMBER)
        license_bytes = archive.read("TSB_AD-1.5.dist-info/LICENSE")
    (OUTPUT / "UPSTREAM-LICENSE").write_bytes(license_bytes)
    (OUTPUT / "NOTICE.md").write_text(
        "Oracle: unmodified TSB-AD 1.5 evaluation/basic_metrics.py, Apache-2.0.\n"
        "Source: https://github.com/TheDatumOrg/TSB-AD\n"
        "The upstream wheel contains LICENSE and no NOTICE file.\n"
        "This directory holds independent test data and expected values, not a production metric implementation.\n"
    )
    rows = []
    cases = {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tsb-ad-independent-oracle-") as temporary:
        script = Path(temporary) / "basic_metrics.py"
        script.write_bytes(source)
        spec = importlib.util.spec_from_file_location("tsb_ad_15_independent_oracle", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for pattern in ("random", "good", "constant", "multi_event"):
            for window in (10, 15, 20, 30, 40, 60, 80, 100):
                name = f"{pattern}_w{window:03}"
                rng = np.random.default_rng(1000 + window)
                labels = np.zeros(640, dtype=np.int64)
                spans = [(170, 208), (460, 510)]
                if pattern == "multi_event":
                    spans = [(3, 12), (90, 115), (225, 280), (375, 393), (485, 550), (620, 638)]
                for start, end in spans:
                    labels[start:end] = 1
                noise = rng.random(640)
                if pattern == "random":
                    scores = noise
                elif pattern == "good":
                    scores = labels.astype(np.float64) * .8 + noise * .2
                elif pattern == "constant":
                    scores = np.full(640, .5, dtype=np.float64)
                else:
                    scores = labels.astype(np.float64) * .3 + noise * .8
                result = module.generate_curve(labels.copy(), scores.copy(), window, "opt", 250)
                roc, pr = float(result[-2]), float(result[-1])
                if not np.isfinite([roc, pr]).all():
                    raise RuntimeError(f"Non-finite upstream oracle result in {name}")
                cases[f"{name}_labels"] = labels
                cases[f"{name}_scores"] = scores
                row = {"id": name, "pattern": pattern, "sliding_window": window,
                       "version": "opt", "threshold_count": 250,
                       "vus_roc": roc, "vus_pr": pr,
                       "vus_roc_hex": roc.hex(), "vus_pr_hex": pr.hex(),
                       "labels_sha256": hashlib.sha256(labels.astype("<i8").tobytes()).hexdigest(),
                       "scores_sha256": hashlib.sha256(scores.astype("<f8").tobytes()).hexdigest()}
                rows.append(row)
                print(f"{name}: VUS-ROC={roc:.15g} VUS-PR={pr:.15g}", flush=True)
    np.savez_compressed(OUTPUT / "inputs.npz", **cases)
    packages = {name: importlib.metadata.version(name) for name in
                ("numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl")}
    provenance = {"schema": "tsb_ad_oracle.v1", "created_date": "2026-09-24",
                  "python": platform.python_version(), "python_executable": sys.executable,
                  "packages": packages, "wheel_url": URL, "wheel_sha256": WHEEL_SHA,
                  "source_member": ORACLE_MEMBER,
                  "source_sha256": hashlib.sha256(source).hexdigest(),
                  "source_modified": False, "application_imported": False,
                  "generator_sha256": sha(Path(__file__)),
                  "inputs_sha256": sha(OUTPUT / "inputs.npz"),
                  "fixture_count": len(rows), "elapsed_s": time.monotonic() - started,
                  "purpose": "Independent numpy<2 expected VUS values; no application metric used",
                  "command": "OPENBLAS_NUM_THREADS=2 uv run --no-project --python 3.12 --with numpy==1.26.4 --with scipy==1.13.1 --with scikit-learn==1.5.2 docs/ai-scientist/review-evidence/build_tsb_oracle.py"}
    (OUTPUT / "golden.json").write_text(json.dumps(rows, indent=2, allow_nan=False) + "\n")
    provenance["golden_sha256"] = sha(OUTPUT / "golden.json")
    (OUTPUT / "provenance.json").write_text(json.dumps(provenance, indent=2, allow_nan=False) + "\n")
    (OUTPUT / "requirements-oracle.txt").write_text(
        "# Python " + platform.python_version() + "; generated independently of application lockfile\n"
        + "\n".join(f"{name}=={version}" for name, version in packages.items()) + "\n"
    )
    print(f"Wrote {len(rows)} immutable golden fixtures; {time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    main()
