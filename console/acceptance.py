"""Read the current acceptance table and expose bounded, referenced evidence."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = ROOT / "docs/ai-scientist"
ACCEPTANCE_PATH = DOCS_ROOT / "m0-acceptance.md"
MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_CONTENT_BYTES = 256 * 1024
EXPECTED_GATE_IDS = {
    *(f"M0.{index}" for index in range(1, 16)),
    *(f"M0.AOS.{index}" for index in range(1, 8)),
}
ALLOWED_EVIDENCE_SUFFIXES = {".md", ".json", ".html", ".txt", ".csv"}
TABLE_ROW = re.compile(r"^\|\s*(M0(?:\.AOS)?\.\d+)\s*\|", re.IGNORECASE)
INLINE_CODE = re.compile(r"`([^`]+)`")
MARKDOWN_PIPE = re.compile(r"(?<!\\)\|")
_EVIDENCE_SALT = secrets.token_bytes(32)


def read_acceptance() -> tuple[dict[str, Any], dict[str, Path]]:
    """Parse actual markdown rows and register safe evidence paths by opaque id."""
    raw = ACCEPTANCE_PATH.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    rows: list[dict[str, Any]] = []
    evidence_paths: dict[str, Path] = {}
    allowed_root = DOCS_ROOT.resolve(strict=True)
    for line in raw.decode("utf-8").splitlines():
        match = TABLE_ROW.match(line)
        if not match:
            continue
        fields = [
            part.strip().replace(r"\|", "|") for part in MARKDOWN_PIPE.split(line.strip())[1:-1]
        ]
        if len(fields) != 3:
            continue
        gate_id, title, detail = fields
        if gate_id != match.group(1) or gate_id in {row["id"] for row in rows}:
            raise ValueError(f"acceptance table has malformed or duplicate row {gate_id}")
        status_word = detail.split(":", maxsplit=1)[0].strip().casefold()
        status_map = {"geçti": "passed", "kısmi": "partial", "açık": "open"}
        state = next(
            (value for key, value in status_map.items() if status_word.startswith(key)), None
        )
        if state is None:
            raise ValueError(f"acceptance row {gate_id} has an unknown status")
        evidence: list[dict[str, str]] = []
        for token in INLINE_CODE.findall(detail):
            relative = _resolve_evidence_path(token)
            if relative is None:
                continue
            candidate = DOCS_ROOT / relative
            try:
                if not _safe_nonsymlink_path(candidate) or not candidate.is_file():
                    continue
                resolved = candidate.resolve(strict=True)
                if (
                    allowed_root not in resolved.parents
                    or resolved.stat().st_size > MAX_EVIDENCE_BYTES
                ):
                    continue
            except OSError:
                continue
            evidence_id = hmac.new(
                _EVIDENCE_SALT, relative.as_posix().encode(), hashlib.sha256
            ).hexdigest()[:32]
            evidence_paths[evidence_id] = resolved
            evidence.append(
                {
                    "name": relative.as_posix(),
                    "url": f"/console-api/evidence/{evidence_id}",
                    "kind": evidence_kind(resolved),
                }
            )
        rows.append(
            {"id": gate_id, "title": title, "status": state, "detail": detail, "evidence": evidence}
        )
    if len(rows) != 22 or {row["id"] for row in rows} != EXPECTED_GATE_IDS:
        raise ValueError("acceptance table must contain all 22 unique M0 and M0.AOS gates")
    counts = {
        state: sum(row["status"] == state for row in rows)
        for state in ("passed", "partial", "open")
    }
    return (
        {
            "total": len(rows),
            **counts,
            "source": "docs/ai-scientist/m0-acceptance.md",
            "source_sha256": source_sha,
            "items": rows,
        },
        evidence_paths,
    )


def _resolve_evidence_path(token: str) -> Path | None:
    value = token.strip()
    if len(value) > 220 or "\n" in value or "\\" in value:
        return None
    if value.startswith("review-evidence/"):
        candidates = [Path(value)]
    elif Path(value).name == value and Path(value).suffix.lower() in ALLOWED_EVIDENCE_SUFFIXES:
        candidates = [Path(value), Path("review-evidence") / value]
    else:
        return None
    if any(".." in candidate.parts for candidate in candidates):
        return None
    existing = [
        candidate
        for candidate in candidates
        if (DOCS_ROOT / candidate).is_file() and _safe_nonsymlink_path(DOCS_ROOT / candidate)
    ]
    return existing[0] if len(existing) == 1 else None


def _safe_nonsymlink_path(path: Path) -> bool:
    current = path
    while current != DOCS_ROOT:
        if current.is_symlink():
            return False
        current = current.parent
    return not DOCS_ROOT.is_symlink()


def evidence_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".html":
        return "html"
    if suffix in {".txt", ".csv"}:
        return "text"
    return "document"


def evidence_content(path: Path) -> dict[str, Any]:
    """Return hash-bound text; large evidence stays capped at 256 KiB."""
    if not _safe_nonsymlink_path(path) or not path.is_file():
        raise FileNotFoundError("evidence is no longer a regular project document")
    metadata = path.stat()
    if metadata.st_size > MAX_EVIDENCE_BYTES:
        raise ValueError("evidence exceeds the 4 MiB console limit")
    raw = path.read_bytes()
    content = raw[:MAX_EVIDENCE_CONTENT_BYTES].decode("utf-8", errors="replace")
    return {
        "name": path.relative_to(DOCS_ROOT).as_posix(),
        "kind": evidence_kind(path),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "truncated": len(raw) > MAX_EVIDENCE_CONTENT_BYTES,
    }
