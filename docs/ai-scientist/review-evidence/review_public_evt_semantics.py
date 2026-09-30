"""Read-only validation of the five actual v2 public EVT Scorer registrations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from lab.scorer.task_semantics import validate_evt_semantics

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "docs/ai-scientist/review-evidence"
PRIVATE_DATABASE = "swapp_lab_m0_public_suite_3ddaa2d8"


def main() -> None:
    receipt_path = EVIDENCE / "default-public-suite-v2-024-batched-check.json"
    receipt = json.loads(receipt_path.read_text())
    destination = EVIDENCE / "public-evt-semantics-024-review.json"
    assert not destination.exists()
    source = ROOT / "lab/scorer/task_semantics.py"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    dsn_path = ROOT / "data/runtime/parallel-m0/public-suite/data/runtime/postgres/scorer.dsn"
    url = make_url(dsn_path.read_text().strip())
    assert url.database == PRIVATE_DATABASE
    engine = create_engine(url, hide_parameters=True, pool_size=1, max_overflow=0)
    outcomes = []
    try:
        with engine.connect() as connection:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            for digest in receipt["source_profile_hashes"]:
                row = connection.execute(text(
                    "SELECT p.*,s.sampling_s,s.evaluation_times_json,s.masked_samples_json,"
                    "s.failure_windows_json,s.semantics_sha256 "
                    "FROM scorer.dataset_profiles p LEFT JOIN scorer.dataset_task_semantics s "
                    "USING(dataset_id,split_id,session_id) WHERE p.profile_sha256=:digest"
                ), {"digest": digest}).mappings().one()
                if row["task_family"] != "EVT":
                    continue
                if row["semantics_sha256"] is None:
                    assert row["dataset_id"] == "TSB-AD-M-Genesis"
                    outcomes.append({"dataset_id": row["dataset_id"],
                                     "validated": True, "cadence": "unknown",
                                     "scope": "No temporal record; sample-index-only EVT profile."})
                    continue
                masks = validate_evt_semantics(
                    sample_count=row["sample_count"], sampling_s=row["sampling_s"],
                    evaluation_times=row["evaluation_times_json"],
                    masked_samples=row["masked_samples_json"],
                    failure_windows=row["failure_windows_json"],
                    semantics_sha256=row["semantics_sha256"],
                )
                outcomes.append({"dataset_id": row["dataset_id"], "validated": True,
                                 "sample_count": row["sample_count"], "masked_count": sum(masks)})
    finally:
        engine.dispose()
    assert len(outcomes) == 5
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
    result = {"schema": "actual-public-evt-semantics.v1", "passed": True,
              "scope": "Read-only temporal digest/axis/mask validation; no scoring or model.",
              "private_database": PRIVATE_DATABASE, "outcomes": outcomes,
              "helper_sha256": source_sha,
              "manifest_sha256": receipt["manifest_sha256"],
              "registration_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
