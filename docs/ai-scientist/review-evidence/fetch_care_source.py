"""Download pinned CARE archive within disk limits; never extract all datasets.

This provenance utility is separate from the production Dataset Service. It
retains upstream compressed bytes in ignored data/ and writes only a source
manifest to review-evidence/. Partial downloads can resume with checked Range.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import time
import urllib.request
from datetime import datetime, timezone
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[3]
CACHE = ROOT / "data/public/care/15846963"
ARCHIVE = CACHE / "CARE_To_Compare.zip"
EXPECTED_SIZE = 5_503_439_673
EXPECTED_MD5 = "2547b58c21ac8c242d13232860cf500c"
URL = "https://zenodo.org/api/records/15846963/files/CARE_To_Compare.zip/content"
OUTPUT = Path(__file__).with_name("care-source-manifest.json")
RESERVE = 20 * 1024**3


def hashes(path: Path) -> tuple[str, str]:
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024**2):
            md5.update(block)
            sha256.update(block)
    return md5.hexdigest(), sha256.hexdigest()


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    partial = ARCHIVE.with_suffix(".zip.part")
    if not ARCHIVE.exists():
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > EXPECTED_SIZE:
            raise ValueError("oversized partial archive; inspect before retrying")
        if shutil.disk_usage(ROOT).free < EXPECTED_SIZE - offset + RESERVE:
            raise RuntimeError("archive would breach 20 GiB disk reserve")
        if offset < EXPECTED_SIZE:
            headers = {"User-Agent": "AI-Scientist-source-review"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urllib.request.Request(URL, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                if offset and (response.status != 206 or response.headers.get("Content-Range", "")
                               != f"bytes {offset}-{EXPECTED_SIZE - 1}/{EXPECTED_SIZE}"):
                    raise ValueError("server did not honor exact resume range")
                declared = response.headers.get("Content-Length")
                if declared is not None and int(declared) != EXPECTED_SIZE - offset:
                    raise ValueError("upstream archive size changed")
                received = offset
                last_report = 0.0
                with partial.open("ab" if offset else "xb") as target:
                    while block := response.read(1024**2):
                        received += len(block)
                        if received > EXPECTED_SIZE:
                            raise ValueError("upstream exceeds pinned archive size")
                        target.write(block)
                        now = time.monotonic()
                        if now - last_report >= 20:
                            if shutil.disk_usage(ROOT).free < RESERVE:
                                raise RuntimeError("20 GiB disk reserve reached; resume later")
                            print(json.dumps({"downloaded_bytes": received,
                                              "expected_bytes": EXPECTED_SIZE,
                                              "elapsed_seconds": round(now - started, 2)}), flush=True)
                            last_report = now
                        if now - started > 1800:
                            raise TimeoutError("30 minute acquisition budget; resume later")
        if partial.stat().st_size != EXPECTED_SIZE:
            raise ValueError("truncated CARE archive")
        md5, sha256 = hashes(partial)
        if md5 != EXPECTED_MD5:
            raise ValueError("upstream MD5 mismatch; preserve partial for inspection")
        partial.rename(ARCHIVE)
    else:
        if ARCHIVE.stat().st_size != EXPECTED_SIZE:
            raise ValueError("cached CARE archive size mismatch")
        md5, sha256 = hashes(ARCHIVE)
        if md5 != EXPECTED_MD5:
            raise ValueError("cached CARE MD5 mismatch")
    with ZipFile(ARCHIVE) as archive:
        entries = archive.infolist()
        files = [{"path": entry.filename, "uncompressed_bytes": entry.file_size,
                  "compressed_bytes": entry.compress_size, "crc32": f"{entry.CRC:08x}"}
                 for entry in entries if not entry.is_dir()]
        for entry in entries:
            path = Path(entry.filename)
            if path.is_absolute() or ".." in path.parts or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("unsafe archive member; nothing extracted")
        # Only read explanatory text; labels and held-out data remain archived.
        descriptions = []
        for entry in entries:
            if "readme" in entry.filename.lower() and entry.file_size <= 1024**2:
                destination = CACHE / ("source-" + Path(entry.filename).name)
                payload = archive.read(entry)
                if destination.exists() and destination.read_bytes() != payload:
                    raise ValueError("conflicting source documentation")
                destination.write_bytes(payload)
                descriptions.append({"member": entry.filename,
                                     "local_path": str(destination.relative_to(ROOT)),
                                     "sha256": hashlib.sha256(payload).hexdigest()})
    manifest = {"schema": "source-discovery.v1", "dataset": "CARE to Compare",
        "record_id": 15846963, "source_url": URL, "doi": "10.5281/zenodo.15846963",
        "license": "CC-BY-SA-4.0 (Zenodo metadata)", "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "archive": str(ARCHIVE.relative_to(ROOT)), "archive_bytes": EXPECTED_SIZE,
        "upstream_md5": md5, "sha256": sha256,
        "acquisition_elapsed_seconds": round(time.monotonic() - started, 2),
        "uncompressed_bytes": sum(entry["uncompressed_bytes"] for entry in files),
        "extraction_status": "only bounded README text; datasets still compressed",
        "descriptions": descriptions, "members": files}
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"manifest": str(OUTPUT.relative_to(ROOT)), "archive_bytes": EXPECTED_SIZE,
                      "sha256": sha256, "uncompressed_bytes": manifest["uncompressed_bytes"]}), flush=True)


if __name__ == "__main__":
    main()
