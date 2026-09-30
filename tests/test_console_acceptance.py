from __future__ import annotations

import hashlib
import re

from console.acceptance import read_acceptance


def test_acceptance_reads_all_real_gate_rows_and_counts_from_status_words() -> None:
    document, evidence = read_acceptance()

    assert document["total"] == 22
    assert document["total"] == document["passed"] + document["partial"] + document["open"]
    assert {item["id"] for item in document["items"]} == {
        *(f"M0.{index}" for index in range(1, 16)),
        *(f"M0.AOS.{index}" for index in range(1, 8)),
    }
    assert (
        document["source_sha256"]
        == hashlib.sha256(open("docs/ai-scientist/m0-acceptance.md", "rb").read()).hexdigest()
    )
    gate_01 = next(item for item in document["items"] if item["id"] == "M0.1")
    assert "|Δ|≤1e-9" in gate_01["title"]
    assert gate_01["status"] == "passed"
    assert evidence
    assert all(re.fullmatch(r"[a-f0-9]{32}", evidence_id) for evidence_id in evidence)


def test_referenced_html_evidence_is_bounded_text_with_source_digest() -> None:
    from console.acceptance import evidence_content

    document, paths = read_acceptance()
    del document
    html_path = next((path for path in paths.values() if path.suffix == ".html"), None)
    if html_path is None:
        return
    result = evidence_content(html_path)
    assert result["kind"] == "html"
    assert isinstance(result["content"], str)
    assert result["sha256"] == hashlib.sha256(html_path.read_bytes()).hexdigest()
    assert len(result["content"].encode()) <= 256 * 1024
