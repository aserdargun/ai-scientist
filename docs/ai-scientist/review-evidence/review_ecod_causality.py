"""Reproduce the pinned upstream ECOD suffix dependency using audited functions.

Only decision_function, column_ecdf and its equal-value helper are compiled
from the locally inspected BSD-2-Clause source. The PyOD package is not imported.
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from scipy.stats import skew

ROOT = Path(__file__).resolve().parents[3]
REFERENCE = ROOT / "data/runtime/ecod-reference-review"
PIN = "109bdfd14d337875c707ce33c50d168dc84d756c"
EXPECTED = {
    "ecod.py": "fc84ebe8607b93625da201d38c9e566006297209a307e110e9b93cc8070f86cf",
    "stat_models.py": "b9f3cd49d3559dc710b2d7668b430f2a484923675a3b033bd5ff3ac35bd313be",
    "LICENSE": "2aad003d1faca304e013f82e5088e8cb1c364470d4755e1e2175922a9872bf56",
}


def selected_functions():
    for name, digest in EXPECTED.items():
        if hashlib.sha256((REFERENCE / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("reviewed upstream source bytes changed")
    stat_tree = ast.parse((REFERENCE / "stat_models.py").read_text())
    ecod_tree = ast.parse((REFERENCE / "ecod.py").read_text())
    functions = [node for node in stat_tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"column_ecdf", "ecdf_terminate_equals_inplace"}]
    detector = next(node for node in ecod_tree.body if isinstance(node, ast.ClassDef) and node.name == "ECOD")
    functions.append(next(node for node in detector.body if isinstance(node, ast.FunctionDef) and node.name == "decision_function"))
    for function in functions:
        function.decorator_list = []
    namespace = {"np": np, "skew": lambda x, axis: np.nan_to_num(skew(x, axis=axis))}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "reviewed_ecod_functions", "exec"), namespace)
    return namespace["decision_function"]


def main():
    upstream_score = selected_functions()
    train = np.arange(-5.0, 6.0).reshape(-1, 1)
    prefix = np.array([-0.5, 0.5, 1.5]).reshape(-1, 1)
    original = np.vstack((prefix, np.array([2.0, 3.0, 4.0, 5.0]).reshape(-1, 1)))
    changed = np.vstack((prefix, np.array([-50.0, -40.0, -30.0, -20.0]).reshape(-1, 1)))
    first = upstream_score(SimpleNamespace(X_train=train.copy(), n_jobs=1), original.copy())
    second = upstream_score(SimpleNamespace(X_train=train.copy(), n_jobs=1), changed.copy())
    difference = float(np.max(np.abs(first[:len(prefix)] - second[:len(prefix)])))
    scale = float(np.max(np.abs(first[:len(prefix)])))
    source = json.loads((REFERENCE / "source.json").read_text())
    record = {"checked_at": datetime.now(UTC).isoformat(), "upstream": source,
              "scope": "Pinned upstream ECOD decision_function plus its actual ECDF helper, compiled in isolation after source inspection; n_jobs=1. This is a baseline architecture counterexample, not production adapter or full PyOD package acceptance.",
              "fixture": {"train": train.tolist(), "eval_original": original.tolist(), "eval_changed_suffix": changed.tolist(), "unchanged_prefix_rows": len(prefix)},
              "original_prefix_scores": first[:len(prefix)].tolist(), "changed_prefix_scores": second[:len(prefix)].tolist(),
              "max_prefix_difference": difference, "relative_prefix_difference": difference / scale,
              "unchanged_prefix_confirmed": bool(np.array_equal(original[:len(prefix)], changed[:len(prefix)])),
              "causality_counterexample_reproduced": difference > 1e-7 * scale,
              "source_sha256": EXPECTED,
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if source["commit"] != PIN:
        raise RuntimeError("upstream commit metadata changed")
    Path(__file__).with_name("ecod-causality-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({key: record[key] for key in ("original_prefix_scores", "changed_prefix_scores", "relative_prefix_difference", "causality_counterexample_reproduced")}, indent=2))
    if not record["causality_counterexample_reproduced"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
