"""Fetch immutable upstream SKAB CSVs and record source checksums, without splitting.

Run from the repository root with Python 3.12+. Raw public data and its license
stay in ignored data/public/skab; only the manifest is suitable for Git.
This discovery utility does not implement the production Dataset Service.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import urllib.request
from datetime import datetime, timezone

REVISION = "b2c0d46c2971dcbfe71e26087b6d231998bb91c2"
ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data/public/skab" / REVISION
OUTPUT = Path(__file__).with_name("skab-source-manifest.json")


def fetch(url: str, ceiling: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "AI-Scientist-source-review"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read(ceiling + 1)
    if len(payload) > ceiling:
        raise ValueError(f"source exceeds bounded download: {url}")
    return payload


def main() -> None:
    if shutil.disk_usage(ROOT).free < 20 * 1024**3 + 100 * 1024**2:
        raise RuntimeError("20 GiB free disk reserve would be breached")
    tree_url = f"https://api.github.com/repos/waico/SKAB/git/trees/{REVISION}?recursive=1"
    tree = json.loads(fetch(tree_url, 2_000_000))
    if tree.get("truncated"):
        raise ValueError("incomplete upstream tree")
    entries = [entry for entry in tree["tree"] if entry["path"] == "LICENSE" or (
        entry["path"].startswith("data/") and entry["path"].endswith(".csv"))]
    records = []
    for entry in sorted(entries, key=lambda row: row["path"]):
        path = Path(entry["path"])
        if path.is_absolute() or ".." in path.parts or entry["type"] != "blob":
            raise ValueError("invalid upstream path")
        target = CACHE / path
        url = f"https://raw.githubusercontent.com/waico/SKAB/{REVISION}/{path.as_posix()}"
        payload = target.read_bytes() if target.exists() else fetch(url, 2_000_000)
        blob_hash = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        if blob_hash != entry["sha"] or len(payload) != entry["size"]:
            raise ValueError(f"upstream blob mismatch: {path}")
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(payload)
        record = {"path": str(target.relative_to(ROOT)), "source_url": url,
                  "bytes": len(payload), "git_blob_sha1": blob_hash,
                  "sha256": hashlib.sha256(payload).hexdigest()}
        if path.suffix == ".csv":
            reader = csv.DictReader(io.StringIO(payload.decode()), delimiter=";")
            rows = list(reader)
            columns = reader.fieldnames or []
            times = [datetime.fromisoformat(row["datetime"]) for row in rows]
            sensors = [column for column in columns if column not in {"datetime", "anomaly", "changepoint"}]
            anomaly_values = [float(row["anomaly"]) for row in rows] if "anomaly" in columns else None
            if anomaly_values is not None and any(value not in (0.0, 1.0) for value in anomaly_values):
                raise ValueError(f"nonbinary labels: {path}")
            anomaly = [int(value) for value in anomaly_values] if anomaly_values is not None else None
            record.update({"rows": len(rows), "columns": columns, "sensor_columns": sensors,
                "first_timestamp_original": rows[0]["datetime"],
                "last_timestamp_original": rows[-1]["datetime"],
                "timezone_in_source": None,
                "duration_seconds": (times[-1] - times[0]).total_seconds(),
                "strictly_increasing_timestamps": all(a < b for a, b in zip(times, times[1:])),
                "nonfinite_sensor_values": sum(not math.isfinite(float(row[col])) for row in rows for col in sensors),
                "anomaly_positive_rows": sum(anomaly) if anomaly is not None else None,
                "anomaly_events": sum(value == 1 and (index == 0 or anomaly[index - 1] == 0)
                                      for index, value in enumerate(anomaly)) if anomaly is not None else None})
        records.append(record)
    manifest = {"schema": "source-discovery.v1", "dataset": "SKAB", "revision": REVISION,
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "tree_url": tree_url,
        "repository_license": "GPL-3.0 (upstream LICENSE)",
        "training_eligibility": "not inferred from repository license; preserve provenance",
        "source_timezone": "unspecified; no assertion of physical UTC instants",
        "split_status": "not split, no train/eval/holdout selection by this utility",
        "files": records}
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"manifest": str(OUTPUT.relative_to(ROOT)), "csv_count": len(records) - 1,
                      "source_bytes": sum(row["bytes"] for row in records)}, allow_nan=False))


if __name__ == "__main__":
    main()
