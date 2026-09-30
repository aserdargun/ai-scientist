"""Recheck four reproduced evaluator flaws without executing candidate code."""
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc

from lab.sandbox.evaluation import _arrow_bytes, _relative_difference, check_hardcoding

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "lab/sandbox/evaluation.py"


def main():
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    frame = pd.DataFrame({"sensor_a": [1.0, 2.0, 3.0]})
    frame.attrs = {"eval_labels": [0, 1, 0], "private_marker": "owned-review-canary"}
    original_attrs = dict(frame.attrs)
    table = ipc.open_stream(pa.BufferReader(_arrow_bytes(
        frame, allowed_sensor_columns=("sensor_a",)
    ))).read_all()
    delta = _relative_difference(np.array([1e-12, 2e-12]), np.array([2e-12, 4e-12]))
    negative_code = check_hardcoding(
        ("A=[" + ", ".join(str(-float(i)) for i in range(64)) + "]").encode(),
        task_ids=frozenset(), evaluation_instants=frozenset(),
    ).code
    timestamp_code = check_hardcoding(
        b'T="2026-09-24T13:15:00+03:00"', task_ids=frozenset(),
        evaluation_instants=frozenset(),
        evaluation_intervals=(("2026-09-24T10:00:00Z", "2026-09-24T11:00:00Z"),),
    ).code
    categorical = pd.DataFrame({"sensor_a": pd.Categorical(
        ["1", "2"], categories=["1", "2", "private-unused-category-canary"]
    )})
    categorical_payload = _arrow_bytes(categorical, allowed_sensor_columns=("sensor_a",))
    categorical_table = ipc.open_stream(pa.BufferReader(categorical_payload)).read_all()
    checks = {
        "arrow_has_no_metadata_or_restored_private_attrs": (
            table.schema.metadata is None and table.to_pandas().attrs == {}
        ),
        "source_dataframe_is_unmodified": frame.attrs == original_attrs,
        "sensor_values_unchanged": np.array_equal(table.to_pandas().to_numpy(), frame.to_numpy()),
        "doubled_small_scores_reject_at_relative_tolerance": delta == 1.0 and delta > 1e-7,
        "negative_64_literal_sequence_rejects": negative_code == "hardcoding_numeric_sequence",
        "utc_equivalent_interior_timestamp_rejects": timestamp_code == "hardcoding_timestamp",
        "unused_categorical_values_do_not_cross_boundary": (
            b"private-unused-category-canary" not in categorical_payload
            and all(pa.types.is_floating(field.type) for field in categorical_table.schema)
            and categorical_table.to_pydict() == {"sensor_a": [1.0, 2.0]}
        ),
        "source_unchanged": hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_hash,
    }
    record = {
        "schema": "guard-preflight-fixed-review.v1",
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": "Independent post-fix checks for four historical WIP flaws; no candidate execution, GPU, private dataset, or AOS changes.",
        "historical_evidence": "guard-preflight-before.json",
        "source_sha256": {"lab/sandbox/evaluation.py": source_hash},
        "observations": {"relative_error": delta, "negative_sequence_code": negative_code,
                         "timestamp_code": timestamp_code},
        "checks": checks,
        "passed": all(checks.values()),
        "command": ".venv/bin/python docs/ai-scientist/review-evidence/review_guard_preflight_fixed.py",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    Path(__file__).with_name("guard-preflight-fixed-review.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
