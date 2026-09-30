"""Temporary metadata-only fixtures; no real Goal/session/README files are read or written."""

import json

import pytest

from scripts.update_development_metrics import END, START, update


def _fixture(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    readme = f"prefix\r\n{START}\nold\n{END}\r\nsuffix\r\n"
    (repo / "README.md").write_bytes(readme.encode())
    goal = {
        "threadId": "root",
        "status": "active",
        "tokensUsed": 73442878,
        "timeUsedSeconds": 145451,
        "createdAt": 1790237749,
        "updatedAt": 1790504404,
        "objective": "PRIVATE objective must not be exported",
    }
    snapshot = tmp_path / "goal.json"
    snapshot.write_text(json.dumps({"goal": goal, "remainingTokens": None}))
    snapshot.chmod(0o600)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    return repo, snapshot, sessions, goal, readme


def _session(path, identity, parent=None, contexts=(("gpt-6-astra", "xhigh"),)):
    source = (
        "cli"
        if parent is None
        else {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": parent,
                    "depth": 1,
                    "agent_path": "private-agent-path",
                }
            }
        }
    )
    rows = [
        {"type": "session_meta", "payload": {"id": identity, "source": source}},
        {"type": "response_item", "payload": {"text": "SECRET prompt"}},
    ]
    rows += [
        {"type": "turn_context", "payload": {"model": model, "effort": effort}}
        for model, effort in contexts
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_exact_counters_ancestry_all_turns_and_sanitized_output(tmp_path):
    repo, snapshot, sessions, goal, readme = _fixture(tmp_path)
    _session(sessions / "root.jsonl", "root")
    _session(
        sessions / "child.jsonl",
        "child",
        "root",
        (("gpt-6-sol", "high"), ("future-model", "unknown-setting"), ("gpt-6-sol", "high")),
    )
    _session(sessions / "grandchild.jsonl", "grandchild", "child", (("gpt-6-luna", "high"),))
    _session(sessions / "unrelated.jsonl", "other", contexts=(("unrelated", "low"),))
    result = update(repo, snapshot, sessions)
    assert result["tokens_used"] == goal["tokensUsed"]
    assert result["active_seconds"] == 145451
    assert result["active_hours"] == 145451 / 3600
    assert result["calendar_hours"] == (1790504404 - 1790237749) / 3600
    assert result["model_evidence"]["related_sessions"] == 3
    assert result["model_evidence"]["complete"]
    models = {row["model"]: row for row in result["development_models"]}
    assert models["gpt-6-sol"]["observed_sessions"] == 1
    assert models["future-model"]["reasoning_effort"] == "unknown-setting"
    assert "unrelated" not in models
    output = (repo / "README.md").read_bytes().decode()
    assert output.split(START)[0] == readme.split(START)[0]
    assert output.split(END)[1] == readme.split(END)[1]
    assert "2026-09-27 13:20:04 Europe/Istanbul" in output
    exported = (
        output + json.dumps(result) + (repo / "docs/development-metrics-history.jsonl").read_text()
    )
    assert all(secret not in exported for secret in ("PRIVATE", "SECRET", "private-agent-path"))


@pytest.mark.parametrize("markers", ["no markers", START + START + END, END + START])
def test_bad_markers_fail_before_any_output_write(tmp_path, markers):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    (repo / "README.md").write_text(markers)
    with pytest.raises(ValueError, match="marker pair"):
        update(repo, snapshot, sessions)
    assert list((repo / "docs").iterdir()) == []
    assert (repo / "README.md").read_text() == markers


def test_retry_dedup_and_counter_decrease_is_not_hidden(tmp_path):
    repo, snapshot, sessions, goal, _ = _fixture(tmp_path)
    _session(sessions / "root.jsonl", "root")
    update(repo, snapshot, sessions)
    update(repo, snapshot, sessions)
    history = repo / "docs/development-metrics-history.jsonl"
    assert len(history.read_text().splitlines()) == 1
    goal.update(tokensUsed=1, timeUsedSeconds=2, updatedAt=goal["updatedAt"] + 10)
    snapshot.write_text(json.dumps({"goal": goal}))
    result = update(repo, snapshot, sessions)
    assert result["tokens_used"] == 1 and result["active_seconds"] == 2
    assert result["counter_decreases"] == ["tokens_used", "active_seconds"]
    assert update(repo, snapshot, sessions)["counter_decreases"] == result["counter_decreases"]
    assert len(history.read_text().splitlines()) == 2


def test_missing_logs_preserve_previous_model_observations(tmp_path):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    _session(sessions / "root.jsonl", "root")
    first = update(repo, snapshot, sessions)
    (sessions / "root.jsonl").unlink()
    result = update(repo, snapshot, sessions)
    assert not result["model_evidence"]["complete"]
    assert "root_session_missing" in result["model_evidence"]["limitations"]
    assert result["development_models"][0]["observed_sessions"] == 1
    assert result["development_models"][0]["role"] == first["development_models"][0]["role"]
    assert result["development_models"][0]["currently_observed_sessions"] == 0


def test_only_incomplete_final_session_json_is_skipped(tmp_path):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    path = sessions / "root.jsonl"
    _session(path, "root")
    with path.open("a") as stream:
        stream.write('{"type":')
    result = update(repo, snapshot, sessions)
    assert "incomplete_final_json_line" in result["model_evidence"]["limitations"]
    before = {p: p.read_bytes() for p in (repo / "docs").iterdir()}
    with path.open("a") as stream:
        stream.write('\n{"type":"turn_context"}\n')
    with pytest.raises(ValueError, match=r"root.jsonl, line 4") as error:
        update(repo, snapshot, sessions)
    assert "type" not in str(error.value)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_malformed_complete_final_line_is_not_skipped(tmp_path):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    path = sessions / "root.jsonl"
    _session(path, "root")
    with path.open("a") as stream:
        stream.write("garbage\n")
    with pytest.raises(ValueError, match="Invalid JSON"):
        update(repo, snapshot, sessions)
    assert list((repo / "docs").iterdir()) == []


def test_incomplete_history_fails_before_any_write(tmp_path):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    _session(sessions / "root.jsonl", "root")
    update(repo, snapshot, sessions)
    history = repo / "docs/development-metrics-history.jsonl"
    with history.open("a") as stream:
        stream.write('{"tokens_used":')
    watched = [repo / "README.md", *list((repo / "docs").iterdir())]
    before = {path: path.read_bytes() for path in watched}
    with pytest.raises(ValueError, match="complete final JSON line"):
        update(repo, snapshot, sessions)
    assert all(path.read_bytes() == value for path, value in before.items())


def test_other_subagent_source_variant_does_not_break_ancestry_scan(tmp_path):
    repo, snapshot, sessions, _, _ = _fixture(tmp_path)
    _session(sessions / "root.jsonl", "root")
    (sessions / "other.jsonl").write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "other", "source": {"subagent": "review"}}}
        )
        + "\n"
    )
    assert update(repo, snapshot, sessions)["model_evidence"]["related_sessions"] == 1
