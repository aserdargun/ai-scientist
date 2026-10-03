"""Render bounded HTML summaries from hash-verified Director ledger artifacts."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Engine, text

from lab.director.artifacts import read_director_artifact, read_registered_calibration
from lab.director.contracts import ExperimentDocument, TrajectoryDocument
from lab.director.ledger import canonical_json_bytes

MAX_REPORT_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LedgerReader(Protocol):
    """The read interface shared by SQL engines and bounded evidence transports."""

    def connect(self) -> AbstractContextManager[Any]:
        """Return a connection scoped by its owning reader."""
        ...


@dataclass(frozen=True, slots=True)
class RunReport:
    """Strict output of report generation; `html` is a complete UTF-8 document."""

    html: bytes
    summary: dict[str, int | float | str]
    sha256: str


@dataclass(frozen=True, slots=True)
class _LedgerExperiment:
    sequence: int
    document: ExperimentDocument
    trajectory: TrajectoryDocument


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"ledger receipt has invalid {name}")
    return value


def _parsed_canonical_object(payload: bytes) -> dict[str, Any]:
    """Check canonical wire bytes before strict models can add new defaults."""

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("immutable JSON artifact contains duplicate keys")
            result[key] = value
        return result

    try:
        parsed = json.loads(payload, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("immutable artifact is not valid JSON") from exc
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != payload:
        raise ValueError("immutable artifact is not canonical JSON")
    return parsed


def _read_verified_experiment(
    engine: LedgerReader,
    experiment_id: str,
    *,
    run_id: UUID,
    status: str,
    artifact_root: Path,
    artifact_reader: Callable[[str], bytes] | None = None,
) -> ExperimentDocument:
    with engine.connect() as connection:
        receipt = connection.execute(
            text("SELECT lab.experiment_record_receipt(:experiment_id)"),
            {"experiment_id": experiment_id},
        ).scalar_one()
    if not isinstance(receipt, dict):
        raise ValueError("ledger returned an invalid experiment receipt")
    if receipt.get("experiment_id") != experiment_id or str(receipt.get("run_id")) != str(run_id):
        raise ValueError("ledger receipt identity differs from requested run")
    if receipt.get("status") != status:
        raise ValueError("ledger status differs from immutable experiment status")
    document_sha = _digest(receipt.get("experiment_sha256"), "experiment_sha256")
    blob_sha = _digest(receipt.get("experiment_blob_sha256"), "experiment_blob_sha256")
    if document_sha != blob_sha:
        raise ValueError("experiment blob is not the canonical document")
    read = artifact_reader or (
        lambda value: read_director_artifact(value, artifact_root=artifact_root)
    )
    payload = read(blob_sha)
    if hashlib.sha256(payload).hexdigest() != document_sha:
        raise ValueError("experiment artifact hash differs from ledger receipt")
    _parsed_canonical_object(payload)
    try:
        document = ExperimentDocument.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise ValueError("experiment artifact does not match its strict schema") from exc
    if document.experiment_id != experiment_id or document.run_id != run_id:
        raise ValueError("experiment artifact identity differs from ledger receipt")
    if document.status != status:
        raise ValueError("experiment artifact status differs from ledger")
    candidate_source = read(document.candidate_blob_sha256)
    if hashlib.sha256(candidate_source).hexdigest() != document.candidate_sha256:
        raise ValueError("candidate source artifact differs from immutable experiment identity")
    return document


def _read_verified_trajectory(
    receipt: dict[str, Any],
    *,
    experiment: ExperimentDocument,
    artifact_root: Path,
    artifact_reader: Callable[[str], bytes] | None = None,
) -> TrajectoryDocument:
    document_sha = _digest(receipt.get("trajectory_sha256"), "trajectory_sha256")
    blob_sha = _digest(receipt.get("trajectory_blob_sha256"), "trajectory_blob_sha256")
    if document_sha != blob_sha:
        raise ValueError("trajectory blob is not the canonical document")
    read = artifact_reader or (
        lambda value: read_director_artifact(value, artifact_root=artifact_root)
    )
    payload = read(blob_sha)
    if hashlib.sha256(payload).hexdigest() != document_sha:
        raise ValueError("trajectory artifact hash differs from ledger receipt")
    _parsed_canonical_object(payload)
    try:
        document = TrajectoryDocument.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        raise ValueError("trajectory artifact does not match its strict schema") from exc
    if (
        document.experiment_id != experiment.experiment_id
        or document.run_id != experiment.run_id
        or document.kind != experiment.kind
        or document.experiment_number != experiment.experiment_number
        or document.baseline_name != experiment.baseline_name
        or document.calibration_sha256 != experiment.calibration_sha256
        or document.infrastructure_stop != experiment.infrastructure_stop
        or document.outcome != experiment.decision
        or document.messages_blob_sha256 != receipt.get("messages_blob_sha256")
    ):
        raise ValueError("trajectory artifact identity differs from experiment receipt")
    messages = read(document.messages_blob_sha256)
    if hashlib.sha256(messages).hexdigest() != document.messages_blob_sha256:
        raise ValueError("trajectory messages artifact failed its digest")
    return document


def read_run_pairs(
    engine: LedgerReader,
    run_id: UUID,
    *,
    artifact_root: Path,
    max_records: int | None = None,
    artifact_reader: Callable[[str], bytes] | None = None,
) -> tuple[_LedgerExperiment, ...]:
    """Read verified pairs; optional limits reject overflow rather than return partial evidence."""
    if max_records is not None and (type(max_records) is not int or max_records < 1):
        raise ValueError("invalid ledger record bound")
    limit = "" if max_records is None else f" LIMIT {max_records + 1}"
    # Only the strictly validated positive integer limit is appended; identities stay bound.
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT experiment_id, sequence, status\n"  # nosec B608
                    "FROM lab.experiments\n"
                    "WHERE run_id = :run_id\n"
                    "ORDER BY sequence, experiment_id" + limit
                ),
                {"run_id": run_id},
            )
            .mappings()
            .all()
        )
    if max_records is not None and len(rows) > max_records:
        raise ValueError("ledger record bound exceeded")
    experiments: list[_LedgerExperiment] = []
    seen: set[str] = set()
    prior_sequence = -1
    for row in rows:
        experiment_id = row.get("experiment_id")
        sequence = row.get("sequence")
        status = row.get("status")
        if (
            not isinstance(experiment_id, str)
            or experiment_id in seen
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence <= prior_sequence
            or not isinstance(status, str)
        ):
            raise ValueError("run ledger contains invalid or duplicate experiment ordering")
        with engine.connect() as connection:
            receipt = connection.execute(
                text("SELECT lab.experiment_record_receipt(:experiment_id)"),
                {"experiment_id": experiment_id},
            ).scalar_one()
        if not isinstance(receipt, dict):
            raise ValueError("ledger returned an invalid experiment receipt")
        document = _read_verified_experiment(
            engine, experiment_id, run_id=run_id, status=status, artifact_root=artifact_root,
            artifact_reader=artifact_reader,
        )
        trajectory = _read_verified_trajectory(
            receipt, experiment=document, artifact_root=artifact_root,
            artifact_reader=artifact_reader,
        )
        experiments.append(
            _LedgerExperiment(sequence=sequence, document=document, trajectory=trajectory)
        )
        prior_sequence = sequence
        seen.add(experiment_id)
    return tuple(experiments)


def read_run_experiments(
    engine: Engine,
    run_id: UUID,
    *,
    artifact_root: Path,
) -> tuple[_LedgerExperiment, ...]:
    """Compatibility name for report consumers; every trajectory is also verified."""
    return read_run_pairs(engine, run_id, artifact_root=artifact_root)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _family_metrics(document: ExperimentDocument) -> str:
    """Summarize only the score family metrics, never task/profile identities."""
    grouped: dict[str, list[Any]] = {}
    for task in document.per_task:
        grouped.setdefault(task.task_family, []).append(task)
    rendered: list[str] = []
    for family in ("EVT", "PDM", "NRM"):
        tasks = grouped.get(family, [])
        if not tasks:
            continue
        if family == "EVT":
            metrics = (
                ("VUS-PR", [task.vus_pr for task in tasks]),
                ("VUS-ROC", [task.vus_roc for task in tasks]),
            )
        elif family == "PDM":
            metrics = (
                ("Task score", [task.task_score for task in tasks]),
                ("FA/day", [task.fa_per_day for task in tasks]),
            )
        else:
            metrics = (
                ("Task score", [task.task_score for task in tasks]),
                ("Duty", [task.duty_fraction for task in tasks]),
            )
        values = []
        for label, items in metrics:
            if items and all(value is not None for value in items):
                average = sum(value for value in items if value is not None) / len(items)
                values.append(f"{label} {_esc(format(average, '.5g'))}")
        rendered.append(f"{_esc(family)} ({_esc(len(tasks))}): {_esc('; '.join(values))}")
    return "<br>".join(rendered) or "—"


def _chart_svg(points: list[tuple[int, float]]) -> str:
    """Produce a compact, script-free SVG using only finite trusted ledger scores."""
    if not points:
        return '<p class="empty">No measured suite scores are available.</p>'
    if any(not math.isfinite(y) for _, y in points):
        raise ValueError("report chart contains a non-finite score")
    low = min(-1.0, min(y for _, y in points))
    high = max(3.0, max(y for _, y in points))
    x0, x1, y0, y1 = 56.0, 944.0, 24.0, 284.0
    first_sequence, last_sequence = points[0][0], points[-1][0]
    x_span = max(1, last_sequence - first_sequence)
    coords = [
        (
            x0 + (x1 - x0) * (sequence - first_sequence) / x_span,
            y1 - (score - low) * (y1 - y0) / (high - low),
        )
        for sequence, score in points
    ]
    commands = [f"M {coords[0][0]:.2f} {coords[0][1]:.2f}"]
    for (_, _previous_y), (x, y) in zip(coords, coords[1:], strict=False):
        commands.append(f"H {x:.2f} V {y:.2f}")
    path = " ".join(commands)
    circles = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4"><title>Sequence '
        f"{_esc(points[i][0])}: score {_esc(format(points[i][1], '.17g'))}</title></circle>"
        for i, (x, y) in enumerate(coords)
    )
    return (
        '<svg viewBox="0 0 1000 320" role="img" aria-label="Dev suite score staircase">'
        '<line x1="56" y1="284" x2="944" y2="284" class="axis"/>'
        f'<path d="{path}" class="staircase"/>{circles}'
        f'<text x="56" y="310">Sequence</text><text x="8" y="18">'
        f"Suite score [{_esc(format(low, '.4g'))}, {_esc(format(high, '.4g'))}]</text></svg>"
    )


def _holdout_report_status(
    engine: Engine, run_id: UUID, *, artifact_root: Path
) -> dict[str, str] | None:
    """Read the latest hash-verified Director state without changing ledger history."""
    with engine.connect() as connection:
        receipt = connection.execute(
            text(
                "SELECT event_json FROM lab.run_events WHERE run_id=:run_id "
                "AND event_type='director.checkpoint' "
                "AND event_json->>'phase'='director_loop_state' "
                "ORDER BY (event_json->>'sequence')::integer DESC LIMIT 1"
            ),
            {"run_id": run_id},
        ).scalar_one_or_none()
    if receipt is None:
        return None
    if not isinstance(receipt, dict) or receipt.get("phase") != "director_loop_state":
        raise ValueError("latest Director state receipt is malformed")
    digest = _digest(receipt.get("payload_sha256"), "Director state payload SHA-256")
    payload = read_director_artifact(digest, artifact_root=artifact_root)
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("Director state checkpoint failed its content hash")
    from lab.director.loop import DirectorLoopState

    try:
        state = DirectorLoopState.model_validate_json(payload, strict=True).verify_consistency()
    except (UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ValueError("Director state checkpoint has an invalid loop-state contract") from exc
    if state.run_id != run_id or not state.champion_experiment_id:
        raise ValueError("Director state checkpoint has an invalid run identity")
    snapshot = state.holdout_approved_snapshot
    approved = snapshot.experiment_id if snapshot is not None else None
    if not isinstance(approved, str) or not approved:
        raise ValueError("Director state has no durable retained reference candidate")
    status = state.holdout_last_status
    if status not in {"not_run", "passed", "reverted", "quota_exhausted", "failed"}:
        raise ValueError("Director state has an invalid holdout status")
    manual_review = (
        status in {"not_run", "quota_exhausted", "failed"}
        or approved != state.champion_experiment_id
    )
    return {
        "development_champion_id": state.champion_experiment_id,
        "approved_champion_id": approved,
        "status": status,
        "manual_review_required": "yes" if manual_review else "no",
    }


def render_run_report(
    run_id: UUID,
    experiments: tuple[_LedgerExperiment, ...],
    *,
    baseline_score: float,
    holdout_status: dict[str, str] | None = None,
) -> RunReport:
    """Render a privacy-minimal report from already hash-verified ledger documents."""
    proposals = [item.document for item in experiments if item.document.kind == "proposal"]
    verdicts = {name: 0 for name in ("KEEP", "KEEP_SIMPLER", "DISCARD", "REJECT")}
    chart_points: list[tuple[int, float]] = []
    current_score: float | None = None
    rows: list[str] = []
    for item in experiments:
        doc = item.document
        score = doc.suite_score
        if doc.kind == "baseline" and doc.baseline_name == "robust_z":
            current_score = score if score is not None else baseline_score
            chart_points.append((item.sequence, current_score))
        elif doc.kind == "proposal":
            if doc.decision is None and doc.infrastructure_stop is None:
                raise ValueError("proposal ledger artifact has no Referee decision")
            if doc.decision is not None:
                verdicts[doc.decision.verdict] += 1
            if (
                doc.decision is not None
                and doc.decision.verdict in {"KEEP", "KEEP_SIMPLER"}
                and score is not None
            ):
                current_score = score
            if current_score is not None:
                chart_points.append((item.sequence, current_score))
        move_name = doc.move_type if doc.kind == "proposal" else doc.baseline_name or "—"
        status_text = "infrastructure abandonment" if doc.infrastructure_stop else doc.status
        rows.append(
            "<tr>"
            f"<td>{item.sequence}</td><td>{_esc(doc.kind)}</td>"
            f"<td>{_esc(status_text)}</td>"
            f"<td>{_esc(doc.decision.verdict if doc.decision else '—')}</td>"
            f"<td>{_esc(format(score, '.17g') if score is not None else '—')}</td>"
            f"<td>{_esc(move_name)}</td>"
            f"<td>{_family_metrics(doc) if doc.kind == 'proposal' else '—'}</td>"
            "</tr>"
        )
    verdict_summary = ", ".join(f"{key}: {value}" for key, value in verdicts.items())
    summary: dict[str, int | float | str] = {
        "run_id": str(run_id),
        "experiment_count": len(proposals),
        "baseline_count": len(experiments) - len(proposals),
        "keep_count": verdicts["KEEP"],
        "keep_simpler_count": verdicts["KEEP_SIMPLER"],
        "discard_count": verdicts["DISCARD"],
        "reject_count": verdicts["REJECT"],
    }
    holdout_section = "<p>Holdout registration and approval state are unavailable.</p>"
    if holdout_status is not None:
        summary.update({f"holdout_{key}": value for key, value in holdout_status.items()})
        holdout_section = (
            "<dl><dt>Development champion</dt><dd>"
            f"<code>{_esc(holdout_status['development_champion_id'])}</code></dd>"
            "<dt>Retained reference candidate</dt><dd>"
            f"<code>{_esc(holdout_status['approved_champion_id'])}</code></dd>"
            "<dt>Holdout status</dt><dd>"
            f"{_esc(holdout_status['status'])}</dd>"
            "<dt>Manual review required</dt><dd>"
            f"{_esc(holdout_status['manual_review_required'])}</dd></dl>"
        )
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>AI Scientist run {_esc(run_id)}</title>
<style>
body{{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;
padding:0 1rem;color:#18212b}}
h1,h2{{color:#102a43}}
.summary{{display:flex;gap:1rem;flex-wrap:wrap}}
.card{{background:#f1f5f9;padding:.8rem 1rem;border-radius:.4rem}}
table{{border-collapse:collapse;width:100%;margin-top:1rem}}
th,td{{border-bottom:1px solid #d9e2ec;padding:.55rem;text-align:left}}
svg{{width:100%;height:auto;max-height:360px}}
.axis{{stroke:#64748b;stroke-width:1}}
.staircase{{fill:none;stroke:#087e8b;stroke-width:3}}
circle{{fill:#087e8b}}.empty{{color:#52606d}}
</style></head><body>
<h1>Research run report</h1><p>Run <code>{_esc(run_id)}</code></p>
<section class="summary"><div class="card">Experiments: {len(proposals)}</div>
<div class="card">Baselines: {len(experiments) - len(proposals)}</div>
<div class="card">Verdicts: {_esc(verdict_summary)}</div></section>
<h2>Dev suite score staircase</h2>{_chart_svg(chart_points)}
<h2>Champion approval status</h2>{holdout_section}
<h2>Ledger experiments</h2><table><thead><tr><th>Sequence</th><th>Kind</th>
<th>Status</th><th>Verdict</th><th>Dev suite score</th><th>Move / baseline</th>
<th>Family metrics</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table>
<p>Historical values come from hash-verified experiment documents. The staircase remains the
immutable dev history; approval status comes from the latest verified Director checkpoint.</p>
</body></html>"""
    payload = document.encode("utf-8")
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("HTML report exceeds its size bound")
    return RunReport(payload, summary, hashlib.sha256(payload).hexdigest())


def build_run_report(
    engine: Engine,
    run_id: UUID,
    *,
    artifact_root: Path,
) -> RunReport:
    """Load all trusted run artifacts and return an HTML report and digest."""
    with engine.connect() as connection:
        baseline_row = (
            connection.execute(
                text(
                    "SELECT r.state,r.request_json,p.report_json,p.report_sha256 "
                    "FROM lab.runs r JOIN lab.reports p ON p.run_id=r.run_id "
                    "WHERE r.run_id=:run_id AND r.state IN ('completed','failed','stopped')"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
    if (
        baseline_row is not None
        and isinstance(baseline_row["request_json"], dict)
        and (baseline_row["request_json"].get("purpose") == "baseline")
    ):
        return render_baseline_report(
            run_id,
            baseline_row["report_json"],
            report_sha256=baseline_row["report_sha256"],
            run_state=baseline_row["state"],
        )
    calibration = read_registered_calibration(engine, run_id=run_id, artifact_root=artifact_root)
    if calibration.champion_seed_scores is None:
        raise ValueError("registered calibration does not include measured champion scores")
    baseline_score = sum(calibration.champion_seed_scores) / len(calibration.champion_seed_scores)
    return render_run_report(
        run_id,
        read_run_experiments(engine, run_id, artifact_root=artifact_root),
        baseline_score=baseline_score,
        holdout_status=_holdout_report_status(engine, run_id, artifact_root=artifact_root),
    )


def render_baseline_report(
    run_id: UUID,
    report: object,
    *,
    report_sha256: str,
    run_state: str,
) -> RunReport:
    """Render the Scorer's content-verified standalone calibration report."""
    if not isinstance(report, dict) or report.get("schema") != "lab.baseline-report.v1":
        raise ValueError("baseline run is missing its typed Scorer report")
    digest = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    if digest != report_sha256 or report.get("run_id") != str(run_id):
        raise ValueError("baseline Scorer report failed its content hash or identity")
    if report.get("status") != run_state:
        raise ValueError("baseline report state differs from its run state")
    status_text = "Tamamlandı" if report.get("status") == "completed" else str(report.get("status"))
    calibration_text = (
        "Kalibrasyon doğrulandı"
        if report.get("calibration_complete") is True
        else "Kalibrasyon tamamlanmadı"
    )
    algorithms = report.get("algorithms")
    seeds = report.get("seeds")
    scores = report.get("baseline_records")
    if (
        not isinstance(algorithms, list)
        or not isinstance(seeds, list)
        or not isinstance(scores, list)
    ):
        raise ValueError("baseline report matrix summary is malformed")
    rows = "".join(
        "<tr><td>"
        + html.escape(str(item.get("experiment_id", "")))
        + "</td><td>"
        + html.escape(str(item.get("task_id", "")))
        + "</td><td>"
        + html.escape(str(item.get("seed", "")))
        + "</td></tr>"
        for item in scores
        if isinstance(item, dict)
    )
    body = (
        '<!doctype html><html lang="tr"><meta charset="utf-8"><title>Lab baseline</title>'
        "<style>body{font:16px system-ui;max-width:1100px;margin:3rem auto;padding:0 1rem;"
        "color:#1d2939}table{border-collapse:collapse;width:100%}"
        "td,th{border:1px solid #d0d5dd;padding:.55rem;text-align:left}"
        "code{overflow-wrap:anywhere}</style><body>"
        "<h1>Baseline kalibrasyon raporu</h1><p>Bu rapor bağımsız baseline ölçümünü gösterir;"
        " araştırma sonucu veya model başarısı değildir.</p><dl>"
        f"<dt>Durum</dt><dd>{html.escape(status_text)}</dd>"
        f"<dt>Kalibrasyon</dt><dd>{html.escape(calibration_text)}</dd>"
        f"<dt>Algoritmalar</dt><dd>{html.escape(', '.join(map(str, algorithms)))}</dd>"
        f"<dt>Seedler</dt><dd>{html.escape(', '.join(map(str, seeds)))}</dd>"
        f"<dt>Görev sayısı</dt><dd>{html.escape(str(report.get('task_count', '—')))}</dd>"
        f"<dt>Skor sayısı</dt><dd>{html.escape(str(report.get('score_count', len(scores))))}</dd>"
        f"<dt>Kalibrasyon özeti</dt><dd><code>"
        f"{html.escape(str(report.get('calibration_sha256') or 'yok'))}</code></dd>"
        f"<dt>Rapor SHA-256</dt><dd><code>{digest}</code></dd></dl>"
        "<h2>Baseline ölçümleri</h2><table><thead><tr><th>Deney</th><th>Görev</th>"
        f"<th>Seed</th></tr></thead><tbody>{rows}</tbody></table></body></html>"
    ).encode()
    if len(body) > MAX_REPORT_BYTES:
        raise ValueError("HTML report exceeds its size bound")
    return RunReport(
        body,
        {"run_id": str(run_id), "status": status_text, "scores": len(scores)},
        hashlib.sha256(body).hexdigest(),
    )
