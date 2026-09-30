"""Cache the official TSB-AD-M archive and record members without extracting data.

Dataset eligibility is separate: this download does not assign Apache-2.0 to the
data, select the four industrial sources, or claim that any data passed M0.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.request
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data/public/tsb-ad-m/source-2026-09-24"
URL = "https://www.thedatum.org/datasets/TSB-AD-M.zip"
EXPECTED_SIZE = 540_383_983  # Observed official Content-Length on 2026-09-24.
OUTPUT = Path(__file__).with_name("tsb-archive-manifest.json")


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "TSB-AD-M.zip"
    started = time.monotonic()
    headers: dict[str, str] = {}
    prior = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else None
    if target.exists() and prior is None:
        raise RuntimeError("cached archive has no provenance; inspect before adopting it")
    if prior is not None:
        headers = prior.get("source_response_headers", {})
    if not target.exists():
        if shutil.disk_usage(ROOT).free < EXPECTED_SIZE + 20 * 1024**3:
            raise RuntimeError("20 GiB disk reserve")
        partial = target.with_suffix(".zip.part")
        if partial.exists():
            raise RuntimeError("inspect existing partial archive before retrying")
        request = urllib.request.Request(URL, headers={"User-Agent": "AI-Scientist-source-review"})
        with urllib.request.urlopen(request, timeout=30) as response, partial.open("xb") as stream:
            if int(response.headers.get("Content-Length", -1)) != EXPECTED_SIZE:
                raise ValueError("official archive changed; review source before downloading")
            headers = {key: value for key, value in response.headers.items()
                       if key.lower() in {"content-length", "last-modified", "etag"}}
            count = 0
            reported = started
            while block := response.read(1024**2):
                count += len(block)
                if count > EXPECTED_SIZE:
                    raise ValueError("archive exceeded source size")
                stream.write(block)
                now = time.monotonic()
                if now - reported > 20:
                    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
                        raise RuntimeError("20 GiB disk reserve reached")
                    print(json.dumps({"downloaded_bytes": count, "expected_bytes": EXPECTED_SIZE}), flush=True)
                    reported = now
                if now - started > 900:
                    raise TimeoutError("15 minute source acquisition budget")
        if count != EXPECTED_SIZE:
            raise ValueError("truncated official archive")
        partial.rename(target)
    if target.stat().st_size != EXPECTED_SIZE:
        raise ValueError("cached archive size mismatch")
    sha256 = hashlib.sha256()
    with target.open("rb") as stream:
        while block := stream.read(4 * 1024**2):
            sha256.update(block)
    if prior is not None and sha256.hexdigest() != prior["sha256"]:
        raise ValueError("archive differs from recorded source bytes")
    with ZipFile(target) as archive:
        entries = [{"path": entry.filename, "uncompressed_bytes": entry.file_size,
                    "compressed_bytes": entry.compress_size, "crc32": f"{entry.CRC:08x}"}
                   for entry in archive.infolist() if not entry.is_dir()]
    manifest = {"schema": "source-discovery.v1", "dataset": "TSB-AD-M",
        "source_url": URL, "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "archive": str(target.relative_to(ROOT)), "archive_bytes": EXPECTED_SIZE,
        "sha256": sha256.hexdigest(), "source_response_headers": headers,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "eligibility": "no dataset selection; licenses must be checked against original sources",
        "extraction_status": "none", "members": entries}
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": str(OUTPUT.relative_to(ROOT)), "sha256": sha256.hexdigest(),
                      "file_count": len(entries)}), flush=True)


if __name__ == "__main__":
    main()
