"""Publish selected Goal counters and model metadata, never raw session content."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

START = "<!-- development-metrics:start -->"
END = "<!-- development-metrics:end -->"
ROLES = {
    ("gpt-6-astra", "xhigh"): "Ana Codex oturumu",
    ("gpt-6-astra", "high"): "Teknik orkestrasyon ve mimari inceleme",
    ("gpt-6-luna", "high"): "İlk uygulama ve odaklı doğrulama işleri",
    ("gpt-6-sol", "high"): "Kodlama, entegrasyon ve inceleme işleri",
}
SCOPE = (
    "Goal aracının raporladığı sayaçlar. Faturalandırma miktarı veya insan işçiliği değildir; "
    "alt ajan/cache hesaplama kapsamı araç tarafından açıklanmıyor."
)


def _incomplete(error: json.JSONDecodeError, line: str) -> bool:
    tail = line[error.pos :].strip()
    return (
        error.pos >= len(line.rstrip())
        or error.msg.startswith("Unterminated string")
        or tail in {"t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul"}
    )


def _records(path: Path, incomplete: list[str]) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                if not line.endswith("\n") and _incomplete(error, line):
                    incomplete.append("incomplete_final_json_line")
                    return
                raise ValueError(f"Invalid JSON: {path.name}, line {number}") from None
            if not isinstance(record, dict):
                raise ValueError(f"Invalid record: {path.name}, line {number}")
            yield record


def _goal(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("Goal snapshot must be a private regular file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["goal"]
        fields = ("tokensUsed", "timeUsedSeconds", "createdAt", "updatedAt")
        if not isinstance(raw, dict) or any(type(raw[key]) is not int for key in fields):
            raise ValueError
        if any(raw[key] < 0 for key in fields) or raw["updatedAt"] < raw["createdAt"]:
            raise ValueError
        if not all(isinstance(raw[key], str) and raw[key] for key in ("threadId", "status")):
            raise ValueError
        return {key: raw[key] for key in (*fields, "threadId", "status")}
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid Goal snapshot counters or identity") from None


def _models(root: Path, thread: str, previous: dict[str, Any]) -> tuple[list[dict], dict]:
    # Read just metadata first. Full histories are streamed only for this goal's ancestry.
    sessions: dict[str, tuple[str | None, list[Path]]] = {}
    gaps: list[str] = []
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else ():
        for record in _records(path, []):
            if record.get("type") != "session_meta":
                continue
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                raise ValueError(f"Invalid session metadata: {path.name}")
            identity = payload.get("id")
            source = payload.get("source")
            parent = None
            if isinstance(source, dict):
                subagent = source.get("subagent", {})
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                if isinstance(spawn, dict):
                    parent = spawn.get("parent_thread_id")
            if not isinstance(identity, str) or (
                parent is not None and not isinstance(parent, str)
            ):
                raise ValueError(f"Invalid session metadata: {path.name}")
            if identity in sessions:
                if sessions[identity][0] != parent:
                    raise ValueError(f"Conflicting session metadata: {path.name}")
                sessions[identity][1].append(path)
            else:
                sessions[identity] = (parent, [path])
            break
    related = {thread}
    while True:
        found = {identity for identity, (parent, _) in sessions.items() if parent in related}
        if found <= related:
            break
        related.update(found)
    if thread not in sessions:
        gaps.append("root_session_missing")
    observed: dict[tuple[str, str], set[str]] = {}
    for identity in sorted(related & sessions.keys()):
        has_context = False
        for path in sessions[identity][1]:
            for record in _records(path, gaps):
                if record.get("type") != "turn_context":
                    continue
                payload = record.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError(f"Invalid turn metadata: {path.name}")
                model, effort = payload.get("model"), payload.get("effort")
                if not isinstance(model, str) or not isinstance(effort, str):
                    gaps.append("model_or_effort_missing")
                    continue
                observed.setdefault((model, effort), set()).add(identity)
                has_context = True
        if not has_context:
            gaps.append("session_has_no_model_context")
    prior = {
        (item["model"], item["reasoning_effort"]): item
        for item in previous.get("development_models", [])
    }
    models = []
    for pair in sorted(observed.keys() | prior.keys()):
        current = len(observed.get(pair, set()))
        old = prior.get(pair, {})
        count = max(current, old.get("observed_sessions", 0))
        if current < old.get("observed_sessions", 0):
            gaps.append("previous_model_observations_not_fully_available")
        models.append(
            {
                "model": pair[0],
                "reasoning_effort": pair[1],
                "role": old.get("role", ROLES.get(pair, "Rol doğrulanmadı")),
                "observed_sessions": count,
                "currently_observed_sessions": current,
            }
        )
    count = len(related & sessions.keys())
    if count < previous.get("model_evidence", {}).get("related_sessions", 0):
        gaps.append("previous_sessions_not_fully_available")
    return models, {
        "source": "session_meta ancestry; all associated turn_context model/effort",
        "related_sessions": count,
        "complete": not gaps,
        "limitations": sorted(set(gaps)),
        "raw_prompts_or_secrets_exported": False,
    }


def _identity(record: dict[str, Any]) -> tuple:
    return tuple(
        record.get(key)
        for key in ("goal_thread_id", "goal_updated_at_epoch", "tokens_used", "active_seconds")
    )


def _cell(value: str) -> str:
    return value.replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("`", "'")


def _render(data: dict[str, Any]) -> str:
    seconds = data["active_seconds"]
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    stamp = datetime.fromtimestamp(data["goal_updated_at_epoch"], ZoneInfo("Europe/Istanbul"))
    lines = [
        "",
        f"Sayaç güncellemesi: **{stamp:%Y-%m-%d %H:%M:%S} Europe/Istanbul**.",
        "",
        "| Ölçüm | Değer |",
        "|---|---:|",
        f"| Aktif süre | {hours} saat {minutes} dakika {seconds} saniye |",
        f"| Aktif süre (saniye) | {data['active_seconds']} |",
        f"| Token | {data['tokens_used']} |",
        f"| Takvim süresi | {data['calendar_hours']:.6f} saat |",
        "",
        SCOPE,
        "",
        "| Model | Ayar | Rol | Gözlenen oturum |",
        "|---|---|---|---:|",
    ]
    for item in data["development_models"]:
        lines.append(
            f"| {_cell(item['model'])} | {_cell(item['reasoning_effort'])} | "
            f"{_cell(item['role'])} | {item['observed_sessions']} |"
        )
    evidence = data["model_evidence"]
    lines += ["", f"Bu taramada ilişkili oturum: {evidence['related_sessions']}."]
    if not evidence["complete"]:
        lines.append(
            "Model günlükleri eksik; önceki gözlemler korunmuştur: "
            + ", ".join(evidence["limitations"])
            + "."
        )
    if data["counter_decreases"]:
        lines.append(
            "Sayaç azalması bildirildi (değerler değiştirilmedi): "
            + ", ".join(data["counter_decreases"])
            + "."
        )
    lines += ["", "[Sayaç ve köken kaydı](docs/development-metrics.json).", ""]
    return "\n".join(lines)


def _atomic(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".metrics-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def update(repository: Path, snapshot: Path, sessions: Path) -> dict[str, Any]:
    """Validate all inputs and markers before writing any public artifact."""
    readme_path = repository / "README.md"
    readme = readme_path.read_bytes().decode("utf-8")
    if (
        readme.count(START) != 1
        or readme.count(END) != 1
        or readme.index(START) >= readme.index(END)
    ):
        raise ValueError("README requires one ordered development-metrics marker pair")
    goal = _goal(snapshot)
    output = repository / "docs/development-metrics.json"
    history = repository / "docs/development-metrics-history.jsonl"
    previous = json.loads(output.read_text()) if output.exists() else {}
    if previous.get("goal_thread_id", goal["threadId"]) != goal["threadId"]:
        raise ValueError("Existing metrics belong to another goal")
    models, evidence = _models(sessions, goal["threadId"], previous)
    data = {
        "schema": "development-goal-usage.v1",
        "source": "Codex get_goal",
        "captured_at": datetime.now(UTC).isoformat(),
        "goal_thread_id": goal["threadId"],
        "goal_created_at_epoch": goal["createdAt"],
        "goal_updated_at_epoch": goal["updatedAt"],
        "tokens_used": goal["tokensUsed"],
        "active_seconds": goal["timeUsedSeconds"],
        "active_hours": goal["timeUsedSeconds"] / 3600,
        "calendar_hours": (goal["updatedAt"] - goal["createdAt"]) / 3600,
        "status": goal["status"],
        "scope_note": SCOPE,
        "development_models": models,
        "model_evidence": evidence,
        "counter_decreases": [
            key
            for key, value in (
                ("tokens_used", goal["tokensUsed"]),
                ("active_seconds", goal["timeUsedSeconds"]),
            )
            if key in previous and value < previous[key]
        ],
    }
    duplicate = False
    if _identity(previous) == _identity(data):
        data["counter_decreases"] = previous.get("counter_decreases", [])
    if history.exists():
        with history.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    raise ValueError("Metrics history requires a complete final JSON line")
        for record in _records(history, []):
            duplicate |= _identity(record) == _identity(data)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    rendered = (
        readme[: readme.index(START) + len(START)] + _render(data) + readme[readme.index(END) :]
    )
    if not duplicate:
        with history.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    _atomic(output, encoded)
    _atomic(readme_path, rendered)
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal-snapshot", required=True, type=Path)
    parser.add_argument("--session-root", type=Path, default=Path.home() / ".codex/sessions")
    args = parser.parse_args()
    try:
        data = update(Path(__file__).resolve().parents[1], args.goal_snapshot, args.session_root)
    except (OSError, ValueError, TypeError):
        print(
            "Metrics update failed; check snapshot, session JSON and README markers.",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "tokens_used": data["tokens_used"],
                "active_seconds": data["active_seconds"],
                "model_evidence_complete": data["model_evidence"]["complete"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
