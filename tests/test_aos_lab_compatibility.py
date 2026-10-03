"""Source preflight rejects drift and never admits runtime from source markers."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_aos_lab_compatibility as preflight
from scripts.check_aos_lab_compatibility import (
    ADMISSION_FILES,
    ADMISSION_MARKERS,
    ADMISSION_RUNTIME_FILES,
    REQUIRED_FILES,
    RUNTIME_CLASSES,
    RUNTIME_FILES,
    inspect_source,
    main,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "aos"
    root.mkdir()
    _git(root, "init", "-q")
    for relative in REQUIRED_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('raise RuntimeError("AOS source must never execute")\n')
    (root / "scripts/serve_desktop.py").write_text(
        "parser.add_argument('--shared-gpu-turns')\n"
        "parser.add_argument('--lab-external-api-url')\n"
        "parser.add_argument('--lab-external-token-file')\n"
        "parser.add_argument('--lab-external-suite')\n"
    )
    (root / "src/aos/gpu_turn.py").write_text(
        "_BROKER_UNIT = 'swapp-lab-gpu-broker.service'\n"
        "_MAX_FRAME_BYTES = 128 * 1024\n"
        "_PROFILE_IDS = frozenset({'aos.decider.turn.v1', 'aos.bonsai.recovery.v1', "
        "'aos.bonsai.vision.v1'})\n"
        "class GpuInferenceResult:\n    version: Literal[1]\n"
        "class _WireReceipt:\n    version: Literal[1]\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def _commit(root: Path) -> None:
    _git(root, "add", ".")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Synthetic source fixture",
    )


def _codes(report: dict) -> set[str]:
    return {item["code"] for item in report["issues"]}


def test_source_markers_never_confirm_runtime_or_admit(checkout, capsys) -> None:
    root, head = checkout
    report = inspect_source(root, head)
    assert report["status"] == "pending"
    assert report["admission_allowed"] is False
    assert report["runtime_capability_confirmed"] is False
    assert _codes(report) == {"joint_runtime_capability_confirmation_pending"}
    assert (
        main(
            [
                "--aos-root",
                str(root),
                "--expected-head",
                head,
                "--source-profile",
                "historical_hooks",
            ]
        )
        == 3
    )
    capsys.readouterr()


def test_wrong_revision_and_dirty_tracked_source_are_rejected(checkout) -> None:
    root, head = checkout
    assert "wrong_checkout_revision" in _codes(inspect_source(root, "0" * 40))
    (root / "services/decider/broker_worker.py").write_text("# edited worker\n")
    assert "tracked_checkout_dirty" in _codes(inspect_source(root, head))


def test_missing_workers_and_comment_only_cli_options_are_rejected(checkout) -> None:
    root, _ = checkout
    (root / "services/bonsai/broker_worker.py").unlink()
    path = root / "scripts/serve_desktop.py"
    path.write_text("# parser.add_argument('--shared-gpu-turns')\n")
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"))
    assert report["status"] == "unsupported"
    assert "required_source_missing" in _codes(report)
    assert "required_cli_option_missing" in _codes(report)


def test_incompatible_wire_version_is_rejected(checkout) -> None:
    root, _ = checkout
    path = root / "src/aos/gpu_turn.py"
    path.write_text(path.read_text().replace("Literal[1]", "Literal[2]"))
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"))
    assert "shared_gpu_protocol_incompatible" in _codes(report)


def test_symlink_worker_and_untracked_replacement_are_rejected(checkout) -> None:
    root, _ = checkout
    path = root / "services/decider/broker_worker.py"
    path.unlink()
    path.symlink_to(root / "services/bonsai/broker_worker.py")
    _commit(root)
    assert "source_symlink" in _codes(inspect_source(root, _git(root, "rev-parse", "HEAD")))
    path.unlink()
    _commit(root)
    path.write_text("# untracked replacement\n")
    assert "required_source_untracked" in _codes(
        inspect_source(root, _git(root, "rev-parse", "HEAD"))
    )


def test_unrelated_coordination_note_allowed_and_history_labeled(checkout) -> None:
    root, head = checkout
    (root / "AI_SCIENTIST_COORDINATION.md").write_text("Untracked coordination note")
    report = inspect_source(root, head, snapshot_kind="isolated_historical")
    assert report["snapshot_kind"] == "isolated_historical"
    assert report["status"] == "pending"
    assert report["admission_allowed"] is False


def test_nested_wrong_checkout_and_invalid_commit_are_rejected(checkout) -> None:
    root, head = checkout
    assert "wrong_checkout_root" in _codes(inspect_source(root / "src", head))
    assert "invalid_expected_head" in _codes(inspect_source(root, "HEAD"))


def test_oversized_source_is_rejected_before_ast(checkout) -> None:
    root, _ = checkout
    (root / "services/bonsai/broker_worker.py").write_bytes(b"#" * (2 * 1024**2 + 1))
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"))
    assert "source_type_or_size" in _codes(report)


@pytest.fixture
def runtime_checkout(checkout):
    root, _ = checkout
    for relative in RUNTIME_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        classes = RUNTIME_CLASSES.get(relative, set())
        path.write_text("\n".join("class " + name + ": pass" for name in sorted(classes)) + "\n")
    for relative in ("services/decider/broker_worker.py", "services/bonsai/broker_worker.py"):
        (root / relative).write_text("def main(): raise RuntimeError('must not execute')\n")
    (root / "src/aos/scientist_protocol.py").write_text(
        "MAX_FRAME_BYTES = 128 * 1024\n"
        "Profile = Literal['aos.decider.turn.v1', 'aos.bonsai.recovery.v1', "
        "'aos.bonsai.vision.v1']\n"
        "class ScientistTurnRequest:\n    version: Literal[1]\n"
        "class ScientistTurnReceipt:\n    version: Literal[1]\n"
    )
    (root / "src/aos/scientist_transport.py").write_text(
        "BROKER_UNIT = 'swapp-lab-gpu-broker.service'\n"
        "class ScientistTurnClient: pass\nclass SystemdBrokerAuthenticator: pass\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_runtime_profile_does_not_require_historical_cli_flags(runtime_checkout, capsys):
    root, head = runtime_checkout
    report = inspect_source(root, head, source_profile="runtime_v1")
    assert report["status"] == "pending" and report["admission_allowed"] is False
    assert (
        main(["--aos-root", str(root), "--expected-head", head, "--source-profile", "runtime_v1"])
        == 3
    )
    capsys.readouterr()
    assert "required_cli_option_missing" in _codes(inspect_source(root, head))


@pytest.mark.parametrize("replacement", ["Literal[2]", "Literal[True]"])
def test_runtime_wrong_wire_is_unsupported(runtime_checkout, replacement):
    root, _ = runtime_checkout
    path = root / "src/aos/scientist_protocol.py"
    path.write_text(path.read_text().replace("Literal[1]", replacement))
    _commit(root)
    assert "runtime_v1_protocol_incompatible" in _codes(
        inspect_source(root, _git(root, "rev-parse", "HEAD"), source_profile="runtime_v1")
    )


def test_runtime_missing_class_and_worker_entry_are_unsupported(runtime_checkout):
    root, _ = runtime_checkout
    (root / "src/aos/scientist_async.py").write_text("# class ScientistAsyncTurnClient: pass\n")
    (root / "services/bonsai/broker_worker.py").write_text("# def main(): pass\n")
    _commit(root)
    codes = _codes(
        inspect_source(root, _git(root, "rev-parse", "HEAD"), source_profile="runtime_v1")
    )
    assert {"runtime_source_marker_missing", "runtime_worker_entry_missing"} <= codes


def test_runtime_untracked_module_is_hashed_but_unsupported(runtime_checkout):
    root, _ = runtime_checkout
    path = root / "src/aos/scientist_decision.py"
    saved = path.read_text()
    path.unlink()
    _commit(root)
    path.write_text(saved)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"), source_profile="runtime_v1")
    assert report["status"] == "unsupported"
    assert "src/aos/scientist_decision.py" in report["source_sha256"]
    assert "required_source_untracked" in _codes(report)


def test_unknown_source_profile_fails_closed(runtime_checkout):
    root, head = runtime_checkout
    assert "unsupported_source_profile" in _codes(
        inspect_source(root, head, source_profile="guess")
    )


def test_historical_boolean_wire_version_is_not_integer_one(checkout):
    root, _ = checkout
    path = root / "src/aos/gpu_turn.py"
    path.write_text(path.read_text().replace("Literal[1]", "Literal[True]"))
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"))
    assert "shared_gpu_protocol_incompatible" in _codes(report)
    assert report["admission_allowed"] is False


@pytest.fixture
def admission_checkout(runtime_checkout):
    root, _ = runtime_checkout
    for relative in ADMISSION_FILES:
        path = root / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".py"):
            text = "raise RuntimeError('source must never execute')\n"
            text += "\n".join(
                ("class " if name[0].isupper() else "def ")
                + name
                + (": pass" if name[0].isupper() else "(): pass")
                for name in sorted(ADMISSION_MARKERS.get(relative, set()))
            )
        elif relative.endswith(".json"):
            text = '{"type":"object"}\n'
        else:
            text = (
                "-- Read-only fixture: never execute migration\nCREATE TABLE fixture(id INTEGER);\n"
            )
        path.write_text(text)
    (root / "scripts/scientist_source_report.py").write_text(
        "SOURCE_FILES = " + repr(ADMISSION_RUNTIME_FILES) + "\n"
        "SOURCE_PROFILES = {'runtime_v1': SOURCE_FILES, 'admission_v2': SOURCE_FILES + "
        + repr(ADMISSION_FILES[len(ADMISSION_RUNTIME_FILES) :])
        + "}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_admission_v2_exact_aggregate_and_optional_pin_never_admit(admission_checkout, capsys):
    root, head = admission_checkout
    assert len(ADMISSION_FILES) == len(set(ADMISSION_FILES)) == 33
    sources = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ADMISSION_FILES
    }
    expected = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report = inspect_source(
        root, head, source_profile="admission_v2", expected_selected_source_sha256=expected
    )
    assert report["source_sha256"] == sources
    assert report["selected_source_sha256"] == expected
    assert report["status"] == "pending"
    assert report["admission_allowed"] is False
    assert report["runtime_capability_confirmed"] is False
    assert (
        main(
            [
                "--aos-root",
                str(root),
                "--expected-head",
                head,
                "--source-profile",
                "admission_v2",
                "--expected-selected-source-sha256",
                expected,
            ]
        )
        == 3
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--aos-root",
                str(root),
                "--expected-head",
                head,
                "--source-profile",
                "admission_v2",
                "--expected-selected-source-sha256",
                "0" * 64,
            ]
        )
        == 2
    )
    assert "selected_source_pin_mismatch" in capsys.readouterr().out


@pytest.mark.parametrize("pin", ["x" * 64, "a" * 64 + "\n", "A" * 64, "short"])
def test_invalid_selected_pin_rejected_before_checkout(pin, tmp_path):
    report = inspect_source(
        tmp_path / "missing",
        "a" * 40,
        source_profile="admission_v2",
        expected_selected_source_sha256=pin,
    )
    assert _codes(report) == {"invalid_expected_selected_source_sha256"}


@pytest.mark.parametrize(
    "relative",
    [
        "src/aos/scientist_profile_output.py",
        "schemas/scientist_admission_record.schema.json",
        "schemas/scientist_admission_record_v2.schema.json",
        "schemas/scientist_bonsai_wire_projection.schema.json",
        "src/aos/contracts.py",
        "database/migrations/0018_scientist_turn_intents.sql",
        "database/migrations/0023_scientist_admission_history.sql",
    ],
)
def test_admission_requires_tracked_dependencies_without_upgrading_runtime(
    admission_checkout, relative
):
    root, _ = admission_checkout
    path = root / relative
    data = path.read_bytes()
    path.unlink()
    _commit(root)
    head = _git(root, "rev-parse", "HEAD")
    missing = inspect_source(root, head, source_profile="admission_v2")
    assert "required_source_missing" in _codes(missing)
    assert "selected_source_sha256" not in missing
    assert inspect_source(root, head, source_profile="runtime_v1")["status"] == "pending"
    path.write_bytes(data)
    untracked = inspect_source(root, head, source_profile="admission_v2")
    assert "required_source_untracked" in _codes(untracked)
    assert relative in untracked["source_sha256"]
    assert untracked["status"] == "unsupported"


def test_admission_observer_selection_and_guard_markers_are_required(admission_checkout):
    root, _ = admission_checkout
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(observer.read_text().replace("src/aos/vision.py", "src/aos/other.py"))
    (root / "src/aos/scientist_profile_output.py").write_text("# AdmissionProfileOutputValidator\n")
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"), source_profile="admission_v2")
    assert {
        "admission_v2_source_selection_incompatible",
        "admission_v2_source_marker_missing",
    } <= _codes(report)


def test_admission_second_read_change_fails_closed(admission_checkout, monkeypatch):
    root, head = admission_checkout
    original = preflight._source_bytes
    seen = {}

    def changed(root, relative):
        seen[relative] = seen.get(relative, 0) + 1
        data = original(root, relative)
        if relative.endswith("0023_scientist_admission_history.sql") and seen[relative] == 2:
            return data + b"-- changed\n"
        return data

    monkeypatch.setattr(preflight, "_source_bytes", changed)
    report = inspect_source(root, head, source_profile="admission_v2")
    assert "source_changed_during_preflight" in _codes(report)
    assert report["status"] == "unsupported" and report["admission_allowed"] is False


@pytest.fixture
def evidence_checkout(admission_checkout):
    root, _ = admission_checkout
    for relative in preflight.TERMINAL_ADDITIONS + preflight.EVIDENCE_ADDITIONS:
        path = root / relative
        if relative.endswith(".py"):
            text = "raise RuntimeError('must never execute AOS')\n"
            text += "\n".join(
                ("class " + name + ": pass") if name[0].isupper() else ("def " + name + "(): pass")
                for name in sorted(preflight.EVIDENCE_MARKERS[relative])
            )
        else:
            text = '{"type":"object","additionalProperties":false}\n'
        path.write_text(text)
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + "SOURCE_PROFILES['terminal_candidate_v1'] = SOURCE_PROFILES['admission_v2'] + "
        + repr(preflight.TERMINAL_ADDITIONS)
        + "\n"
        + "SOURCE_PROFILES['evidence_transport_candidate_v2'] = "
        "SOURCE_PROFILES['terminal_candidate_v1'] + " + repr(preflight.EVIDENCE_ADDITIONS) + "\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_evidence_profile_exact_selection_pin_and_no_admission(evidence_checkout, capsys):
    root, head = evidence_checkout
    assert len(preflight.EVIDENCE_FILES) == len(set(preflight.EVIDENCE_FILES)) == 38
    sources = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in preflight.EVIDENCE_FILES
    }
    expected = hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    report = inspect_source(
        root,
        head,
        source_profile=preflight.EVIDENCE_PROFILE,
        expected_selected_source_sha256=expected,
    )
    assert report["status"] == "pending" and report["admission_allowed"] is False
    assert report["runtime_capability_confirmed"] is False
    assert report["selected_source_sha256"] == expected
    assert report["source_sha256"] == sources
    arguments = [
        "--aos-root",
        str(root),
        "--expected-head",
        head,
        "--source-profile",
        preflight.EVIDENCE_PROFILE,
    ]
    assert main(arguments) == 3
    capsys.readouterr()
    assert main(arguments + ["--expected-selected-source-sha256", "0" * 64]) == 2
    assert "selected_source_pin_mismatch" in capsys.readouterr().out


@pytest.mark.parametrize("relative", preflight.TERMINAL_ADDITIONS + preflight.EVIDENCE_ADDITIONS)
def test_evidence_dependencies_missing_untracked_and_dirty_are_denied(evidence_checkout, relative):
    root, _ = evidence_checkout
    path = root / relative
    saved = path.read_bytes()
    path.unlink()
    _commit(root)
    head = _git(root, "rev-parse", "HEAD")
    assert "required_source_missing" in _codes(
        inspect_source(root, head, source_profile=preflight.EVIDENCE_PROFILE)
    )
    for old in ("runtime_v1", "admission_v2"):
        assert inspect_source(root, head, source_profile=old)["status"] == "pending"
    path.write_bytes(saved)
    assert "required_source_untracked" in _codes(
        inspect_source(root, head, source_profile=preflight.EVIDENCE_PROFILE)
    )
    _commit(root)
    head = _git(root, "rev-parse", "HEAD")
    path.write_bytes(saved + b"\n")
    assert "tracked_checkout_dirty" in _codes(
        inspect_source(root, head, source_profile=preflight.EVIDENCE_PROFILE)
    )


def test_evidence_observer_extension_drift_denied(evidence_checkout):
    root, _ = evidence_checkout
    path = root / "scripts/scientist_source_report.py"
    path.write_text(path.read_text().replace("src/aos/scientist_terminal.py", "src/aos/other.py"))
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.EVIDENCE_PROFILE
    )
    assert "evidence_source_selection_incompatible" in _codes(report)


@pytest.fixture
def client_checkout(evidence_checkout):
    root, _ = evidence_checkout
    (root / preflight.CLIENT_ADDITIONS[0]).write_text(
        "raise RuntimeError('never execute AOS')\nclass ScientistEvidenceClient: pass\n"
    )
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.CLIENT_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.EVIDENCE_PROFILE!r}] + {preflight.CLIENT_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


@pytest.fixture
def reviewed_checkout(client_checkout):
    root, head = client_checkout
    _git(root, "rm", "--cached", preflight.CLIENT_ADDITIONS[0])
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(observer.read_text() + "# staged snapshot change\n")
    _git(root, "add", str(observer))
    observer.write_text(observer.read_text() + "# unstaged snapshot change\n")
    # A dirty tracked file outside the selected profile tests diff drift even
    # when its porcelain status and all selected source bytes stay unchanged.
    extra = root / "src/aos/gpu_turn.py"
    extra.write_text(extra.read_text() + "# unselected tracked change\n")
    return root, head


def _reviewed_pins(root, selected=preflight.CLIENT_FILES):
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in selected}
    tracked = set(_git(root, "ls-files").splitlines())
    untracked = {name: value for name, value in sources.items() if name not in tracked}

    def mapping_hash(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    return {
        "expected_selected_source_sha256": mapping_hash(sources),
        "expected_selected_untracked_sha256": mapping_hash(untracked),
        "expected_tracked_diff_sha256": hashlib.sha256(
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(root),
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--binary",
                    "HEAD",
                ]
            )
        ).hexdigest(),
    }


def _reviewed(root, head, pins=None, **kwargs):
    return inspect_source(
        root,
        head,
        snapshot_kind="reviewed_snapshot",
        source_profile=preflight.CLIENT_PROFILE,
        **(_reviewed_pins(root) if pins is None else pins),
        **kwargs,
    )


def test_explicit_reviewed_dirty_snapshot_keeps_runtime_pending(reviewed_checkout, capsys):
    root, head = reviewed_checkout
    assert len(preflight.CLIENT_FILES) == len(set(preflight.CLIENT_FILES)) == 39
    default = inspect_source(root, head, source_profile=preflight.CLIENT_PROFILE)
    assert {"tracked_checkout_dirty", "required_source_untracked"} <= _codes(default)
    assert default["source_ready"] is False
    pins = _reviewed_pins(root)
    report = _reviewed(root, head, pins)
    assert report["status"] == "pending" and report["source_ready"] is True
    assert report["admission_allowed"] is False and report["runtime_capability_confirmed"] is False
    assert report["tracked_checkout_dirty"] is True
    assert set(report["selected_untracked_source_sha256"]) == set(preflight.CLIENT_ADDITIONS)
    for name, value in pins.items():
        assert report[name.removeprefix("expected_")] == value
    args = [
        "--aos-root",
        str(root),
        "--expected-head",
        head,
        "--snapshot-kind",
        "reviewed_snapshot",
        "--source-profile",
        preflight.CLIENT_PROFILE,
    ]
    for name, value in pins.items():
        args.extend(["--" + name.replace("_", "-"), value])
    assert main(args) == 3
    assert '"source_ready": true' in capsys.readouterr().out


@pytest.mark.parametrize("name", ["selected_source", "selected_untracked", "tracked_diff"])
@pytest.mark.parametrize("change", ["missing", "wrong", "malformed"])
def test_reviewed_requires_every_exact_explicit_pin(reviewed_checkout, name, change):
    root, head = reviewed_checkout
    pins = _reviewed_pins(root)
    key = "expected_" + name + "_sha256"
    if change == "missing":
        pins.pop(key)
    else:
        pins[key] = "0" * 64 if change == "wrong" else "a" * 64 + "\n"
    report = _reviewed(root, head, pins)
    assert report["status"] == "unsupported" and report["source_ready"] is False
    assert report["admission_allowed"] is False


@pytest.mark.parametrize("head", [None, "HEAD", "0" * 40])
def test_reviewed_requires_exact_head(reviewed_checkout, head):
    root, _ = reviewed_checkout
    assert _reviewed(root, head)["status"] == "unsupported"


@pytest.mark.parametrize("mutation", ["source", "status", "tracking", "diff"])
def test_reviewed_rejects_midread_snapshot_drift(reviewed_checkout, monkeypatch, mutation):
    root, head = reviewed_checkout
    pins = _reviewed_pins(root)
    original = preflight._source_bytes
    calls = 0

    def changed(root, relative):
        nonlocal calls
        if relative == preflight.CLIENT_ADDITIONS[0]:
            calls += 1
            if calls == 2:
                if mutation == "tracking":
                    _git(root, "add", relative)
                elif mutation == "status":
                    (root / "new-unrelated-note").write_text("created during observation")
                else:
                    path = root / (relative if mutation == "source" else "src/aos/gpu_turn.py")
                    path.write_bytes(path.read_bytes() + b"# drift\n")
        return original(root, relative)

    monkeypatch.setattr(preflight, "_source_bytes", changed)
    report = _reviewed(root, head, pins)
    assert report["status"] == "unsupported" and report["source_ready"] is False
    if mutation == "tracking":
        assert "selected_tracking_changed_during_preflight" in _codes(report)
    elif mutation == "source":
        assert "source_changed_during_preflight" in _codes(report)
    else:
        assert "checkout_changed_during_preflight" in _codes(report)


def test_reviewed_does_not_allow_missing_client_or_unknown_profile(reviewed_checkout):
    root, head = reviewed_checkout
    pins = _reviewed_pins(root)
    (root / preflight.CLIENT_ADDITIONS[0]).unlink()
    assert "required_source_missing" in _codes(_reviewed(root, head, pins))
    report = inspect_source(
        root, head, snapshot_kind="reviewed_snapshot", source_profile="guess", **pins
    )
    assert _codes(report) == {"unsupported_source_profile"}


def test_journal_profile_requires_exact_five_additional_sources(client_checkout):
    root, _ = client_checkout
    for relative in preflight.JOURNAL_ADDITIONS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".py"):
            path.write_text(
                "raise RuntimeError('never execute AOS')\nclass ScientistEvidenceJournal: pass\n"
            )
        else:
            path.write_text("{}\n" if relative.endswith(".json") else "-- no SQL execution\n")
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.JOURNAL_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.CLIENT_PROFILE!r}] + {preflight.JOURNAL_ADDITIONS!r}\n"
    )
    _commit(root)
    head = _git(root, "rev-parse", "HEAD")
    assert len(preflight.JOURNAL_FILES) == len(set(preflight.JOURNAL_FILES)) == 44
    report = inspect_source(
        root,
        head,
        source_profile=preflight.JOURNAL_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.JOURNAL_FILES),
    )
    assert report["status"] == "pending" and report["source_ready"] is True
    (root / preflight.JOURNAL_ADDITIONS[0]).unlink()
    report = inspect_source(root, head, source_profile=preflight.JOURNAL_PROFILE)
    assert "required_source_missing" in _codes(report)
    # The explicit earlier profile never silently adopts journal's selected list.
    earlier = inspect_source(root, head, source_profile=preflight.CLIENT_PROFILE)
    assert set(earlier["source_sha256"]) == set(preflight.CLIENT_FILES)


@pytest.mark.parametrize("driver", ["external_diff", "textconv", "clean_filter"])
def test_preflight_never_executes_checkout_git_drivers(checkout, tmp_path, driver):
    root, head = checkout
    marker = tmp_path / "driver-executed"
    program = tmp_path / "git-driver.py"
    program.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text('executed')\n"
        "if sys.argv[2] == 'clean_filter':\n"
        "    sys.stdout.buffer.write(sys.stdin.buffer.read())\n"
        "else:\n"
        "    sys.stdout.write('synthetic diff output\\n')\n"
    )
    command = shlex.join([sys.executable, str(program), str(marker), driver])
    relative = "services/bonsai/broker_worker.py"
    source = root / relative
    source.write_text(source.read_text() + "# force changed content/stat\n")
    # Configure only this synthetic checkout, after its fixture commit. Neither
    # the test setup nor production preflight may need to invoke the driver.
    if driver == "external_diff":
        _git(root, "config", "diff.external", command)
    elif driver == "textconv":
        (root / ".gitattributes").write_text(relative + " diff=proof\n")
        _git(root, "config", "diff.proof.textconv", command)
    else:
        (root / ".gitattributes").write_text(relative + " filter=proof\n")
        _git(root, "config", "filter.proof.clean", command)
    report = inspect_source(root, head)
    assert report["admission_allowed"] is False
    assert not marker.exists(), "Read-only preflight executed a checkout-configured Git driver"
    if driver == "clean_filter":
        assert "git_content_filter_configured" in _codes(report)


@pytest.fixture
def retained_checkout(client_checkout):
    root, _ = client_checkout
    source = Path(__file__).resolve().parents[1] / preflight.SCIENTIST_RETAINED_SCHEMA
    schema = json.loads(source.read_bytes())

    def request_projection(value):
        if isinstance(value, list):
            return [request_projection(item) for item in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"$ref"}:
            return request_projection(schema["$defs"][value["$ref"].removeprefix("#/$defs/")])
        projected = {key: request_projection(item) for key, item in value.items()}
        for width in (32, 64):
            if projected.get("pattern") == "^[a-f0-9]{" + str(width) + "}$":
                projected.update(minLength=width, maxLength=width)
        return projected

    request_schema = {
        "$schema": schema["$schema"],
        **request_projection(schema["$defs"]["evidence_request"]),
    }
    markers = {
        "src/aos/scientist_evidence_journal.py": ["ScientistEvidenceJournal"],
        "src/aos/scientist_release_proof.py": ["ScientistReleaseProofVerifier"],
        "src/aos/scientist_budget_witness.py": [
            "ScientistBudgetWitnessVerifier",
            "ScientistRetainedTerminalVerifier",
        ],
        "src/aos/scientist_retained_evidence_transport.py": ["ScientistRetainedEvidenceCodec"],
    }
    observer = root / "scripts/scientist_source_report.py"
    for profile, parent, additions in (
        (preflight.JOURNAL_PROFILE, preflight.CLIENT_PROFILE, preflight.JOURNAL_ADDITIONS),
        (preflight.RELEASE_PROFILE, preflight.JOURNAL_PROFILE, preflight.RELEASE_ADDITIONS),
        (preflight.RETAINED_PROFILE, preflight.RELEASE_PROFILE, preflight.RETAINED_ADDITIONS),
    ):
        for relative in additions:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith(".py"):
                path.write_text(
                    "raise RuntimeError('never execute AOS')\n"
                    + "\n".join("class " + name + ": pass" for name in markers.get(relative, []))
                )
            elif relative == "schemas/scientist_retained_evidence_transport.schema.json":
                path.write_bytes(source.read_bytes())
            elif relative == "schemas/scientist_retained_evidence_transport_request.schema.json":
                path.write_text(json.dumps(request_schema, indent=2))
            else:
                path.write_text("{}\n" if relative.endswith(".json") else "-- no execution\n")
        observer.write_text(
            observer.read_text()
            + f"SOURCE_PROFILES[{profile!r}] = SOURCE_PROFILES[{parent!r}] + {additions!r}\n"
        )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "profile,count", [(preflight.RELEASE_PROFILE, 48), (preflight.RETAINED_PROFILE, 52)]
)
def test_new_explicit_profiles_keep_source_pending_and_old_scopes(
    retained_checkout, profile, count
):
    root, head = retained_checkout
    report = inspect_source(root, head, source_profile=profile)
    assert report["status"] == "pending" and report["source_ready"] is True
    assert report["admission_allowed"] is False
    assert len(report["source_sha256"]) == count
    if count == 52:
        assert (
            report["retained_schema_sha256"]
            == report["scientist_retained_schema_sha256"]
            == preflight.RETAINED_SCHEMA_SHA256
        )
        assert report["retained_request_schema_sha256"] == preflight.RETAINED_REQUEST_SHA256
    for previous, old_count in (
        (preflight.EVIDENCE_PROFILE, 38),
        (preflight.CLIENT_PROFILE, 39),
        (preflight.JOURNAL_PROFILE, 44),
    ):
        older = inspect_source(root, head, source_profile=previous)
        assert older["status"] == "pending" and len(older["source_sha256"]) == old_count


@pytest.mark.parametrize("relative", preflight.RELEASE_ADDITIONS + preflight.RETAINED_ADDITIONS)
def test_retained_profile_requires_every_new_member(retained_checkout, relative):
    root, _ = retained_checkout
    (root / relative).unlink()
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.RETAINED_PROFILE
    )
    assert "required_source_missing" in _codes(report)
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.mark.parametrize(
    "kind", ["full_schema", "request_schema", "duplicate_schema", "observer", "marker"]
)
def test_retained_contract_or_source_drift_fails_with_reviewed_bytes(retained_checkout, kind):
    root, _ = retained_checkout
    if kind in {"full_schema", "request_schema", "duplicate_schema"}:
        relative = preflight.RETAINED_ADDITIONS[2 if kind == "request_schema" else 1]
        path = root / relative
        value = json.loads(path.read_bytes())
        if kind == "duplicate_schema":
            path.write_text('{"$schema":"ignored",' + path.read_text().lstrip()[1:])
        else:
            variants = (
                value["oneOf"]
                if kind == "request_schema"
                else value["$defs"]["evidence_request"]["oneOf"]
            )
            variants[0]["properties"]["version"]["const"] = 2
            path.write_text(json.dumps(value))
    elif kind == "observer":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(
            path.read_text().replace(
                "database/migrations/0025_scientist_retained_evidence_controls.sql",
                "database/migrations/0025_other.sql",
            )
        )
    else:
        (root / preflight.RELEASE_ADDITIONS[2]).write_text(
            "# class ScientistBudgetWitnessVerifier\n"
        )
    _commit(root)
    report = inspect_source(
        root,
        _git(root, "rev-parse", "HEAD"),
        source_profile=preflight.RETAINED_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.RETAINED_FILES),
    )
    assert report["status"] == "unsupported" and report["source_ready"] is False
    assert "tracked_checkout_dirty" not in _codes(report)
    expected = {
        "full_schema": "retained_full_schema_mismatch",
        "request_schema": "retained_request_schema_mismatch",
        "duplicate_schema": "duplicate_schema_key",
        "observer": "evidence_source_selection_incompatible",
        "marker": "admission_v2_source_marker_missing",
    }[kind]
    assert expected in _codes(report)


@pytest.fixture
def resolution_checkout(retained_checkout):
    root, _ = retained_checkout
    (root / "src/aos/scientist_resolution.py").write_text(
        "raise RuntimeError('never execute AOS')\nclass ScientistResolutionJournal: pass\n"
    )
    (root / "database/migrations/0026_scientist_turn_resolutions.sql").write_text(
        "-- no execution\n"
    )
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.RESOLUTION_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.RETAINED_PROFILE!r}] + {preflight.RESOLUTION_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_resolution54_explicit_selection_preserves_full_schema_pins(resolution_checkout):
    root, head = resolution_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.RESOLUTION_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.RESOLUTION_FILES),
    )
    assert report["status"] == "pending" and report["source_ready"] is True
    assert report["admission_allowed"] is False and report["runtime_capability_confirmed"] is False
    assert len(report["source_sha256"]) == 54
    assert report["retained_schema_sha256"] == preflight.RETAINED_SCHEMA_SHA256
    assert report["retained_request_schema_sha256"] == preflight.RETAINED_REQUEST_SHA256
    for profile, count in ((preflight.RELEASE_PROFILE, 48), (preflight.RETAINED_PROFILE, 52)):
        prior = inspect_source(root, head, source_profile=profile)
        assert prior["status"] == "pending" and len(prior["source_sha256"]) == count


@pytest.mark.parametrize("relative", preflight.RESOLUTION_ADDITIONS)
def test_resolution_requires_both_actual_new_members(resolution_checkout, relative):
    root, _ = resolution_checkout
    (root / relative).unlink()
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.RESOLUTION_PROFILE
    )
    assert "required_source_missing" in _codes(report)
    assert report["source_ready"] is False


@pytest.mark.parametrize("kind", ["full_schema", "request_schema", "observer", "marker"])
def test_resolution_cannot_bypass_schema_or_membership_checks(resolution_checkout, kind):
    root, _ = resolution_checkout
    if kind in {"full_schema", "request_schema"}:
        path = root / preflight.RETAINED_ADDITIONS[1 if kind == "full_schema" else 2]
        value = json.loads(path.read_bytes())
        value["unexpected"] = True
        path.write_text(json.dumps(value))
    elif kind == "observer":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(
            path.read_text().replace(
                "database/migrations/0026_scientist_turn_resolutions.sql",
                "database/migrations/0026_other.sql",
            )
        )
    else:
        (root / "src/aos/scientist_resolution.py").write_text(
            "# class ScientistResolutionJournal: pass\n"
        )
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.RESOLUTION_PROFILE
    )
    assert report["status"] == "unsupported" and report["source_ready"] is False
    assert {
        "full_schema": "retained_full_schema_mismatch",
        "request_schema": "retained_request_schema_mismatch",
        "observer": "evidence_source_selection_incompatible",
        "marker": "admission_v2_source_marker_missing",
    }[kind] in _codes(report)


@pytest.fixture
def native_checkout(resolution_checkout):
    root, _ = resolution_checkout
    observer = root / "scripts/scientist_source_report.py"
    extensions = (
        (
            preflight.RETAINED_HOST_PROFILE,
            preflight.RESOLUTION_PROFILE,
            preflight.RETAINED_HOST_ADDITIONS,
        ),
        (preflight.PHYSICAL_PROFILE, preflight.RETAINED_HOST_PROFILE, preflight.PHYSICAL_ADDITIONS),
        (preflight.BOOTSTRAP_PROFILE, preflight.PHYSICAL_PROFILE, preflight.BOOTSTRAP_ADDITIONS),
        (preflight.PROVIDER_PROFILE, preflight.BOOTSTRAP_PROFILE, preflight.PROVIDER_ADDITIONS),
    )
    for profile, parent, additions in extensions:
        for relative in additions:
            classes = preflight.NATIVE_METHODS.get(relative, {})
            definitions = []
            for name, methods in classes.items():
                definitions.append(f"class {name}:\n")
                definitions.extend(
                    f"    {'async ' if method == 'prepare_async' else ''}"
                    f"def {method}(self, *args, **kwargs): pass\n"
                    for method in sorted(methods)
                )
            if relative == preflight.PHYSICAL_ADDITIONS[0]:
                definitions.append("def run_bounded(*args, **kwargs): pass\n")
            if relative == preflight.PROVIDER_ADDITIONS[0]:
                definitions.append("def prepare_retained_provider_channel(channel): pass\n")
            (root / relative).write_text(
                "raise RuntimeError('never execute AOS')\n" + "".join(definitions)
            )
        observer.write_text(
            observer.read_text()
            + f"SOURCE_PROFILES[{profile!r}] = SOURCE_PROFILES[{parent!r}] + {additions!r}\n"
        )
    desktop = root / "src/aos/scientist_desktop.py"
    desktop.write_text(
        desktop.read_text() + "\nclass ScientistDesktopBinding:\n"
        "    async def prepare_infer(self, request, **kwargs): pass\n    pass\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "profile,selected,count",
    [
        (preflight.RETAINED_HOST_PROFILE, preflight.RETAINED_HOST_FILES, 55),
        (preflight.PHYSICAL_PROFILE, preflight.PHYSICAL_FILES, 56),
        (preflight.BOOTSTRAP_PROFILE, preflight.BOOTSTRAP_FILES, 57),
        (preflight.PROVIDER_PROFILE, preflight.PROVIDER_FILES, 58),
    ],
)
def test_native_profiles_pin_exact_closure_without_runtime_admission(
    native_checkout, profile, selected, count
):
    root, head = native_checkout
    report = inspect_source(
        root,
        head,
        source_profile=profile,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, selected),
    )
    assert report["status"] == "pending" and report["source_ready"] is True
    assert set(report["source_sha256"]) == set(selected) and len(selected) == count
    assert report["admission_allowed"] is False and report["runtime_capability_confirmed"] is False
    assert report["retained_schema_sha256"] == preflight.RETAINED_SCHEMA_SHA256
    assert report["retained_request_schema_sha256"] == preflight.RETAINED_REQUEST_SHA256
    prior = inspect_source(root, head, source_profile=preflight.RESOLUTION_PROFILE)
    assert prior["status"] == "pending" and len(prior["source_sha256"]) == 54


@pytest.mark.parametrize(
    "relative",
    preflight.RETAINED_HOST_ADDITIONS
    + preflight.PHYSICAL_ADDITIONS
    + preflight.BOOTSTRAP_ADDITIONS
    + preflight.PROVIDER_ADDITIONS,
)
def test_provider_profile_requires_every_native_member(native_checkout, relative):
    root, _ = native_checkout
    (root / relative).unlink()
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.PROVIDER_PROFILE
    )
    assert "required_source_missing" in _codes(report)
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.mark.parametrize(
    "relative,class_name,method",
    [
        (preflight.RETAINED_HOST_ADDITIONS[0], "ScientistRetainedHost", "inspect_resolution"),
        (preflight.BOOTSTRAP_ADDITIONS[0], "ScientistBootstrapCodec", "decode_response"),
        (preflight.BOOTSTRAP_ADDITIONS[0], "ScientistBootstrapCapture", "prepare"),
        (preflight.PROVIDER_ADDITIONS[0], "ScientistRetainedProviderClient", "verify_peer"),
        (preflight.PROVIDER_ADDITIONS[0], "ScientistRetainedProviderAdapter", "read_source"),
    ],
)
def test_native_public_api_requires_real_definitions(native_checkout, relative, class_name, method):
    root, _ = native_checkout
    path = root / relative
    path.write_text(path.read_text().replace(f"    def {method}(", f"    # def {method}("))
    _commit(root)
    report = inspect_source(
        root,
        _git(root, "rev-parse", "HEAD"),
        source_profile=preflight.PROVIDER_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.PROVIDER_FILES),
    )
    assert {"code": "native_source_api_missing", "path": relative, "class": class_name} in report[
        "issues"
    ]
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.mark.parametrize("kind", ["observer", "schema", "pin", "missing_pins"])
def test_native_profiles_preserve_fail_closed_selection_and_snapshot_pins(native_checkout, kind):
    root, head = native_checkout
    pins = _reviewed_pins(root, preflight.PROVIDER_FILES)
    if kind == "observer":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(
            path.read_text().replace("src/aos/scientist_bootstrap.py", "src/aos/other_bootstrap.py")
        )
    elif kind == "schema":
        path = root / preflight.RETAINED_ADDITIONS[1]
        value = json.loads(path.read_bytes())
        value["unexpected"] = True
        path.write_text(json.dumps(value))
    elif kind == "pin":
        path = root / preflight.PROVIDER_ADDITIONS[0]
        path.write_text(path.read_text() + "# source bytes changed after review\n")
    else:
        pins = {}
    if kind in {"observer", "schema"}:
        pins = _reviewed_pins(root, preflight.PROVIDER_FILES)
    report = inspect_source(
        root,
        head,
        source_profile=preflight.PROVIDER_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **pins,
    )
    assert {
        "observer": "evidence_source_selection_incompatible",
        "schema": "retained_full_schema_mismatch",
        "pin": "selected_source_pin_mismatch",
        "missing_pins": "missing_expected_selected_source_sha256",
    }[kind] in _codes(report)
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.mark.parametrize("profile", [preflight.BOOTSTRAP_PROFILE, preflight.PROVIDER_PROFILE])
@pytest.mark.parametrize(
    "relative,class_name,method",
    [
        (preflight.BOOTSTRAP_ADDITIONS[0], "ScientistBootstrapCapture", "prepare_async"),
        ("src/aos/scientist_desktop.py", "ScientistDesktopBinding", "prepare_infer"),
    ],
)
@pytest.mark.parametrize("change", ["missing", "sync"])
def test_native_async_preparation_api_is_required(
    native_checkout, profile, relative, class_name, method, change
):
    root, _ = native_checkout
    path = root / relative
    replacement = f"    # async def {method}(" if change == "missing" else f"    def {method}("
    path.write_text(path.read_text().replace(f"    async def {method}(", replacement))
    _commit(root)
    report = inspect_source(root, _git(root, "rev-parse", "HEAD"), source_profile=profile)
    assert {"code": "native_source_api_missing", "path": relative, "class": class_name} in report[
        "issues"
    ]
    assert report["source_ready"] is False and report["admission_allowed"] is False
    earlier = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.PHYSICAL_PROFILE
    )
    assert earlier["status"] == "pending"


@pytest.fixture
def factory_checkout(native_checkout):
    root, _ = native_checkout
    definitions = ["raise RuntimeError('never execute AOS')\n"]
    for name, methods in preflight.NATIVE_METHODS[preflight.FACTORY_ADDITIONS[0]].items():
        definitions.append(f"class {name}:\n")
        definitions.extend(
            f"    def {method}(self, *args, **kwargs): pass\n" for method in sorted(methods)
        )
    (root / preflight.FACTORY_ADDITIONS[0]).write_text("".join(definitions))
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.FACTORY_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.PROVIDER_PROFILE!r}] + {preflight.FACTORY_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_bootstrap_factory59_exact_selection_preserves_native58(factory_checkout):
    root, head = factory_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.FACTORY_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.FACTORY_FILES),
    )
    assert report["status"] == "pending" and report["source_ready"] is True
    assert len(report["source_sha256"]) == 59
    assert report["retained_schema_sha256"] == preflight.RETAINED_SCHEMA_SHA256
    assert report["retained_request_schema_sha256"] == preflight.RETAINED_REQUEST_SHA256
    assert report["admission_allowed"] is False and report["runtime_capability_confirmed"] is False
    prior = inspect_source(root, head, source_profile=preflight.PROVIDER_PROFILE)
    assert prior["status"] == "pending" and len(prior["source_sha256"]) == 58


@pytest.mark.parametrize("change", ["missing", "selection", "method", "async"])
def test_bootstrap_factory59_rejects_source_or_api_drift(factory_checkout, change):
    root, _ = factory_checkout
    if change == "missing":
        (root / preflight.FACTORY_ADDITIONS[0]).unlink()
    elif change == "selection":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(
            path.read_text().replace(
                "src/aos/scientist_bootstrap_factory.py", "src/aos/other_factory.py"
            )
        )
    elif change == "method":
        path = root / preflight.FACTORY_ADDITIONS[0]
        path.write_text(
            path.read_text().replace("    def confirm_runtime(", "    # def confirm_runtime(")
        )
    else:
        path = root / preflight.BOOTSTRAP_ADDITIONS[0]
        path.write_text(
            path.read_text().replace(
                "    async def prepare_async(", "    # async def prepare_async("
            )
        )
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.FACTORY_PROFILE
    )
    assert {
        "missing": "required_source_missing",
        "selection": "evidence_source_selection_incompatible",
        "method": "native_source_api_missing",
        "async": "native_source_api_missing",
    }[change] in _codes(report)
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.fixture
def configured_source_checkout(factory_checkout):
    root, _ = factory_checkout
    definitions = ["raise RuntimeError('never execute AOS')\n"]
    for name, methods in preflight.NATIVE_METHODS[preflight.CONFIGURED_SOURCE_ADDITIONS[0]].items():
        definitions.append(f"class {name}:\n")
        definitions.extend(
            f"    def {method}(self, *args, **kwargs): pass\n" for method in sorted(methods)
        )
    (root / preflight.CONFIGURED_SOURCE_ADDITIONS[0]).write_text("".join(definitions))
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.CONFIGURED_SOURCE_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.FACTORY_PROFILE!r}] + "
        + f"{preflight.CONFIGURED_SOURCE_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_configured_source60_exact_selection_preserves_native59(configured_source_checkout):
    root, head = configured_source_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.CONFIGURED_SOURCE_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.CONFIGURED_SOURCE_FILES),
    )
    assert report["status"] == "pending" and report["source_ready"] is True
    assert len(report["source_sha256"]) == 60
    assert report["retained_schema_sha256"] == preflight.RETAINED_SCHEMA_SHA256
    assert report["retained_request_schema_sha256"] == preflight.RETAINED_REQUEST_SHA256
    assert report["admission_allowed"] is False and report["runtime_capability_confirmed"] is False
    prior = inspect_source(root, head, source_profile=preflight.FACTORY_PROFILE)
    assert prior["status"] == "pending" and len(prior["source_sha256"]) == 59


@pytest.mark.parametrize("change", ["missing", "selection", "method", "factory", "async"])
def test_configured_source60_rejects_source_or_inherited_api_drift(
    configured_source_checkout, change
):
    root, _ = configured_source_checkout
    if change == "missing":
        (root / preflight.CONFIGURED_SOURCE_ADDITIONS[0]).unlink()
    elif change == "selection":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(
            path.read_text().replace(
                "src/aos/scientist_source_authority.py", "src/aos/other_authority.py"
            )
        )
    else:
        relative, method, prefix = {
            "method": (preflight.CONFIGURED_SOURCE_ADDITIONS[0], "_policy_matches", "def"),
            "factory": (preflight.FACTORY_ADDITIONS[0], "confirm_runtime", "def"),
            "async": (preflight.BOOTSTRAP_ADDITIONS[0], "prepare_async", "async def"),
        }[change]
        path = root / relative
        path.write_text(
            path.read_text().replace(f"    {prefix} {method}(", f"    # {prefix} {method}(")
        )
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.CONFIGURED_SOURCE_PROFILE
    )
    assert {
        "missing": "required_source_missing",
        "selection": "evidence_source_selection_incompatible",
        "method": "native_source_api_missing",
        "factory": "native_source_api_missing",
        "async": "native_source_api_missing",
    }[change] in _codes(report)
    assert report["source_ready"] is False and report["admission_allowed"] is False


@pytest.fixture
def lab_readback_checkout(configured_source_checkout):
    root, _ = configured_source_checkout
    for relative in preflight.LAB_READBACK_ADDITIONS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith(".py"):
            path.write_text(
                "raise RuntimeError('never execute AOS')\n"
                "class ScientistLabReadback: pass\nclass ScientistLabReadbacks: pass\n"
            )
        elif relative.endswith(".json"):
            path.write_text("{}\n")
        else:
            path.write_text("-- independently selected migration fixture\n")
    transport = root / "src/aos/scientist_transport.py"
    transport.write_text(
        transport.read_text() + "\nclass SystemdCallerAuthenticator:\n"
        "    def authenticate(self, generation): pass\n"
        "    def still_current(self, generation): pass\n"
    )
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text() + f"SOURCE_PROFILES[{preflight.LAB_READBACK_PROFILE!r}] = "
        f"SOURCE_PROFILES[{preflight.CONFIGURED_SOURCE_PROFILE!r}] + "
        f"{preflight.LAB_READBACK_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_lab_readback63_exact_membership_keeps_admission_disabled(lab_readback_checkout):
    root, head = lab_readback_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.LAB_READBACK_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.LAB_READBACK_FILES),
    )
    assert report["source_ready"] is True
    assert len(report["source_sha256"]) == 63
    assert report["admission_allowed"] is False
    assert (
        inspect_source(root, head, source_profile=preflight.CONFIGURED_SOURCE_PROFILE)[
            "source_ready"
        ]
        is True
    )


@pytest.mark.parametrize("change", ["migration", "selection", "caller"])
def test_lab_readback63_rejects_missing_member_or_native_caller(lab_readback_checkout, change):
    root, _ = lab_readback_checkout
    if change == "migration":
        (root / preflight.LAB_READBACK_ADDITIONS[2]).unlink()
    elif change == "selection":
        path = root / "scripts/scientist_source_report.py"
        path.write_text(path.read_text().replace("0027_scientist_lab_readbacks.sql", "wrong.sql"))
    else:
        path = root / "src/aos/scientist_transport.py"
        path.write_text(
            path.read_text().replace(
                "def authenticate(self, generation)", "def wrong(self, generation)"
            )
        )
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.LAB_READBACK_PROFILE
    )
    assert report["source_ready"] is False
    assert report["admission_allowed"] is False


def test_native_preparer_accepts_reviewed63_and_rejects_source_drift(
    lab_readback_checkout, tmp_path
):
    from scripts.prepare_aos_native_configuration import verified_source_receipt

    root, head = lab_readback_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.LAB_READBACK_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.LAB_READBACK_FILES),
    )
    receipt = tmp_path / "source63-receipt.json"
    raw = json.dumps(report, sort_keys=True).encode()
    receipt.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    assert verified_source_receipt(receipt, expected)["source_ready"] is True
    path = root / preflight.LAB_READBACK_ADDITIONS[0]
    path.write_text(path.read_text() + "\n# changed after review\n")
    with pytest.raises(ValueError, match="changed since review"):
        verified_source_receipt(receipt, expected)


@pytest.fixture
def no_admission_checkout(lab_readback_checkout):
    """Synthetic source closure; importing these modules deliberately raises."""
    root, _ = lab_readback_checkout
    module = root / preflight.NO_ADMISSION_ADDITIONS[0]
    module.write_text(
        "raise RuntimeError('never execute AOS')\n"
        "NAME = 'aos-scientist-no-admission-observation.v1'\nVERSION = 1\n"
        "FEATURE = 'no-admission-observation.v1'\n"
        f"SCHEMA_SHA256 = {preflight.NO_ADMISSION_SCHEMA_SHA256!r}\n"
        "class ScientistNoAdmissionVerifier:\n    def verify(self, observation_bytes): pass\n"
        "class ScientistNoAdmissionJournal:\n    def append(self, observation_bytes): pass\n"
        "def sqlite_store_identity(store): pass\n"
    )
    schema = Path(__file__).resolve().parents[1] / preflight.SCIENTIST_NO_ADMISSION_SCHEMA
    (root / preflight.NO_ADMISSION_ADDITIONS[1]).write_bytes(schema.read_bytes())
    (root / preflight.NO_ADMISSION_ADDITIONS[2]).write_text(
        "-- synthetic append-only no-admission migration fixture\n"
    )
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text() + f"SOURCE_PROFILES[{preflight.NO_ADMISSION_PROFILE!r}] = "
        f"SOURCE_PROFILES[{preflight.LAB_READBACK_PROFILE!r}] + "
        f"{preflight.NO_ADMISSION_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_no_admission66_exact_source_closure_and_schema_pin_never_admit(no_admission_checkout):
    root, head = no_admission_checkout
    assert len(preflight.NO_ADMISSION_FILES) == len(set(preflight.NO_ADMISSION_FILES)) == 66
    assert preflight.NO_ADMISSION_FILES[:63] == preflight.LAB_READBACK_FILES
    report = inspect_source(
        root,
        head,
        source_profile=preflight.NO_ADMISSION_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.NO_ADMISSION_FILES),
    )
    assert report["source_ready"] is True
    assert len(report["source_sha256"]) == 66
    assert report["no_admission_schema_sha256"] == preflight.NO_ADMISSION_SCHEMA_SHA256
    assert report["scientist_no_admission_schema_sha256"] == preflight.NO_ADMISSION_SCHEMA_SHA256
    assert report["no_admission_feature_admitted"] is False
    assert report["admission_allowed"] is report["runtime_capability_confirmed"] is False
    assert _codes(report) == {"joint_runtime_capability_confirmation_pending"}
    prior = inspect_source(root, head, source_profile=preflight.LAB_READBACK_PROFILE)
    assert prior["source_ready"] is True
    assert set(prior["source_sha256"]) == set(preflight.LAB_READBACK_FILES)
    assert "no_admission_schema_sha256" not in prior


@pytest.mark.parametrize("relative", preflight.NO_ADMISSION_ADDITIONS)
def test_no_admission66_requires_each_actual_module_schema_migration(
    no_admission_checkout, relative
):
    root, _ = no_admission_checkout
    (root / relative).unlink()
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.NO_ADMISSION_PROFILE
    )
    assert "required_source_missing" in _codes(report)
    assert report["source_ready"] is report["admission_allowed"] is False


@pytest.mark.parametrize(
    "change",
    [
        "verify",
        "append",
        "async_verify",
        "async_append",
        "identity",
        "old_selection",
        "wrong_migration",
        "old_caller",
        "old_budget",
    ],
)
def test_no_admission66_requires_actual_new_api_and_every_old63_check(
    no_admission_checkout, change
):
    root, _ = no_admission_checkout
    if change in {"verify", "append", "async_verify", "async_append", "identity"}:
        path = root / preflight.NO_ADMISSION_ADDITIONS[0]
        method = change.removeprefix("async_") if change != "identity" else "sqlite_store_identity"
        prefix = "async def " if change.startswith("async_") else "def missing_"
        path.write_text(path.read_text().replace(f"def {method}(", f"{prefix}{method}("))
    elif change in {"old_selection", "wrong_migration"}:
        path = root / "scripts/scientist_source_report.py"
        before, after = (
            (preflight.NO_ADMISSION_PROFILE, "old_observation_profile")
            if change == "old_selection"
            else ("0028_scientist_no_admission_closures.sql", "wrong.sql")
        )
        path.write_text(path.read_text().replace(before, after))
    elif change == "old_caller":
        path = root / "src/aos/scientist_transport.py"
        path.write_text(path.read_text().replace("def authenticate(", "def missing_authenticate("))
    else:
        path = root / "src/aos/scientist_budget_witness.py"
        path.write_text(
            path.read_text().replace(
                "class ScientistBudgetWitnessVerifier:", "class MissingBudgetVerifier:"
            )
        )
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.NO_ADMISSION_PROFILE
    )
    assert report["source_ready"] is report["admission_allowed"] is False
    assert _codes(report) & {
        "native_source_api_missing",
        "evidence_source_selection_incompatible",
        "admission_v2_source_marker_missing",
    }


@pytest.mark.parametrize("change", ["version", "extra", "nested", "duplicate", "nonfinite"])
def test_no_admission66_requires_exact_full_recursive_schema(no_admission_checkout, change):
    root, _ = no_admission_checkout
    path = root / preflight.NO_ADMISSION_ADDITIONS[1]
    value = json.loads(path.read_bytes())
    if change == "version":
        value["properties"]["version"]["const"] = 2
    elif change == "extra":
        value["additionalProperties"] = True
    elif change == "nested":
        value["$defs"]["cleanup_scope"]["additionalProperties"] = True
    if change == "duplicate":
        path.write_bytes(b'{"version":1,"version":1}')
    elif change == "nonfinite":
        path.write_bytes(b'{"number":NaN}')
    else:
        path.write_text(json.dumps(value))
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.NO_ADMISSION_PROFILE
    )
    assert "no_admission_full_schema_mismatch" in _codes(report)
    assert report["source_ready"] is report["admission_allowed"] is False


def _no_admission_receipt(root, head, path):
    report = inspect_source(
        root,
        head,
        source_profile=preflight.NO_ADMISSION_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.NO_ADMISSION_FILES),
    )
    assert report["source_ready"] is True
    raw = json.dumps(report, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("relative", preflight.NO_ADMISSION_ADDITIONS)
def test_native_preparer_rechecks_each_new66_file_before_work(
    no_admission_checkout, tmp_path, relative
):
    from scripts.prepare_aos_native_configuration import verified_source_receipt

    root, head = no_admission_checkout
    receipt = tmp_path / "source66-receipt.json"
    pin = _no_admission_receipt(root, head, receipt)
    assert (
        verified_source_receipt(
            receipt, pin, required_source_profile=preflight.NO_ADMISSION_PROFILE
        )["source_ready"]
        is True
    )
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed since review"):
        verified_source_receipt(
            receipt, pin, required_source_profile=preflight.NO_ADMISSION_PROFILE
        )


def test_explicit_new_profile_rejects_old63_receipt_before_inspection(
    lab_readback_checkout, tmp_path, monkeypatch
):
    from scripts.prepare_aos_native_configuration import verified_source_receipt

    root, head = lab_readback_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.LAB_READBACK_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.LAB_READBACK_FILES),
    )
    receipt = tmp_path / "old63.json"
    raw = json.dumps(report).encode()
    receipt.write_bytes(raw)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("wrong source profile must reject before work")

    monkeypatch.setattr(preflight, "inspect_source", forbidden)
    with pytest.raises(ValueError, match="explicit reviewed source profile differs"):
        verified_source_receipt(
            receipt,
            hashlib.sha256(raw).hexdigest(),
            required_source_profile=preflight.NO_ADMISSION_PROFILE,
        )


@pytest.mark.parametrize(
    "before,after",
    [
        ("VERSION = 1", "VERSION = True"),
        ("VERSION = 1", "VERSION = 1.0"),
        ("VERSION = 1", "VERSION = 2"),
        ("VERSION = 1", "VERSION = 1\nVERSION = 2"),
        ("no-admission-observation.v1", "no-admission-observation.v2"),
        (preflight.NO_ADMISSION_SCHEMA_SHA256, "0" * 64),
    ],
)
def test_no_admission66_actual_module_version_feature_and_full_pin(
    no_admission_checkout, before, after
):
    root, _ = no_admission_checkout
    path = root / preflight.NO_ADMISSION_ADDITIONS[0]
    path.write_text(path.read_text().replace(before, after))
    _commit(root)
    report = inspect_source(
        root, _git(root, "rev-parse", "HEAD"), source_profile=preflight.NO_ADMISSION_PROFILE
    )
    assert "no_admission_protocol_incompatible" in _codes(report)
    assert report["source_ready"] is report["admission_allowed"] is False


@pytest.fixture
def no_admission_recovery_checkout(no_admission_checkout):
    """Synthetic source67 adds retained recovery without removing source66."""
    root, _ = no_admission_checkout
    module = root / preflight.NO_ADMISSION_ADDITIONS[0]
    module.write_text(
        module.read_text()
        + f"RECOVERY_SCHEMA_SHA256 = {preflight.NO_ADMISSION_RECOVERY_SCHEMA_SHA256!r}\n"
        + "class ScientistNoAdmissionRetainedVerifier:\n"
        + "    def verify(self, raw, recovery_raw): pass\n"
        + "class ScientistNoAdmissionRetainedJournal:\n"
        + "    def append(self, raw, recovery_raw): pass\n"
    )
    schema = Path(__file__).resolve().parents[1] / preflight.SCIENTIST_NO_ADMISSION_RECOVERY_SCHEMA
    (root / preflight.NO_ADMISSION_RECOVERY_ADDITIONS[0]).write_bytes(schema.read_bytes())
    observer = root / "scripts/scientist_source_report.py"
    observer.write_text(
        observer.read_text()
        + f"SOURCE_PROFILES[{preflight.NO_ADMISSION_RECOVERY_PROFILE!r}] = "
        + f"SOURCE_PROFILES[{preflight.NO_ADMISSION_PROFILE!r}] + "
        + f"{preflight.NO_ADMISSION_RECOVERY_ADDITIONS!r}\n"
    )
    _commit(root)
    return root, _git(root, "rev-parse", "HEAD")


def test_no_admission_recovery67_preserves_all66_checks_without_runtime_admission(
    no_admission_recovery_checkout,
):
    root, head = no_admission_recovery_checkout
    files = preflight.NO_ADMISSION_RECOVERY_FILES
    assert len(files) == len(set(files)) == 67
    assert files[:66] == preflight.NO_ADMISSION_FILES
    report = inspect_source(
        root,
        head,
        source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, files),
    )
    assert report["source_ready"] is True
    assert set(report["source_sha256"]) == set(files)
    assert report["no_admission_schema_sha256"] == preflight.NO_ADMISSION_SCHEMA_SHA256
    assert (
        report["no_admission_recovery_schema_sha256"]
        == preflight.NO_ADMISSION_RECOVERY_SCHEMA_SHA256
    )
    assert (
        report["scientist_no_admission_recovery_schema_sha256"]
        == preflight.NO_ADMISSION_RECOVERY_SCHEMA_SHA256
    )
    assert report["no_admission_feature_admitted"] is False
    assert report["no_admission_recovery_feature_admitted"] is False
    assert report["admission_allowed"] is report["runtime_capability_confirmed"] is False
    assert _codes(report) == {"joint_runtime_capability_confirmation_pending"}
    previous = inspect_source(root, head, source_profile=preflight.NO_ADMISSION_PROFILE)
    assert previous["source_ready"] is True
    assert len(previous["source_sha256"]) == 66
    assert "no_admission_recovery_schema_sha256" not in previous


@pytest.mark.parametrize(
    "relative,before,after",
    [
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "def verify(self, raw, recovery_raw)",
            "def missing_verify(self, raw, recovery_raw)",
        ),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "def append(self, raw, recovery_raw)",
            "async def append(self, raw, recovery_raw)",
        ),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "def verify(self, observation_bytes)",
            "def missing_verify(self, observation_bytes)",
        ),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "def append(self, observation_bytes)",
            "async def append(self, observation_bytes)",
        ),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "def sqlite_store_identity(",
            "def missing_identity(",
        ),
        ("src/aos/scientist_transport.py", "def authenticate(", "def missing_authenticate("),
        (
            "src/aos/scientist_budget_witness.py",
            "class ScientistBudgetWitnessVerifier:",
            "class MissingBudgetVerifier:",
        ),
        (
            "scripts/scientist_source_report.py",
            "0028_scientist_no_admission_closures.sql",
            "wrong.sql",
        ),
        (preflight.NO_ADMISSION_ADDITIONS[0], "VERSION = 1", "VERSION = True"),
        (preflight.NO_ADMISSION_ADDITIONS[0], preflight.NO_ADMISSION_SCHEMA_SHA256, "0" * 64),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            preflight.NO_ADMISSION_RECOVERY_SCHEMA_SHA256,
            "0" * 64,
        ),
        (
            preflight.NO_ADMISSION_ADDITIONS[0],
            "RECOVERY_SCHEMA_SHA256 =",
            "RECOVERY_SCHEMA_SHA256 = '0'\nRECOVERY_SCHEMA_SHA256 =",
        ),
    ],
)
def test_recovery67_cannot_skip_new_or_inherited_source_guards(
    no_admission_recovery_checkout,
    relative,
    before,
    after,
):
    root, _ = no_admission_recovery_checkout
    path = root / relative
    assert before in path.read_text()
    path.write_text(path.read_text().replace(before, after))
    _commit(root)
    report = inspect_source(
        root,
        _git(root, "rev-parse", "HEAD"),
        source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
    )
    assert report["source_ready"] is report["admission_allowed"] is False
    assert _codes(report) & {
        "native_source_api_missing",
        "admission_v2_source_marker_missing",
        "evidence_source_selection_incompatible",
        "no_admission_protocol_incompatible",
    }


@pytest.mark.parametrize("which", ["old", "new"])
def test_recovery67_requires_both_full_recursive_schema_pins(no_admission_recovery_checkout, which):
    root, _ = no_admission_recovery_checkout
    relative = (
        preflight.NO_ADMISSION_ADDITIONS[1]
        if which == "old"
        else preflight.NO_ADMISSION_RECOVERY_ADDITIONS[0]
    )
    path = root / relative
    document = json.loads(path.read_bytes())
    document["$defs"]["cleanup_scope"]["additionalProperties"] = True
    path.write_text(json.dumps(document))
    _commit(root)
    report = inspect_source(
        root,
        _git(root, "rev-parse", "HEAD"),
        source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
    )
    code = (
        "no_admission_full_schema_mismatch"
        if which == "old"
        else "no_admission_recovery_full_schema_mismatch"
    )
    assert code in _codes(report)
    assert report["source_ready"] is False


def test_recovery67_requires_new_schema_file(no_admission_recovery_checkout):
    root, _ = no_admission_recovery_checkout
    (root / preflight.NO_ADMISSION_RECOVERY_ADDITIONS[0]).unlink()
    _commit(root)
    report = inspect_source(
        root,
        _git(root, "rev-parse", "HEAD"),
        source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
    )
    assert "required_source_missing" in _codes(report)
    assert report["source_ready"] is False


@pytest.mark.parametrize("which", ["aos", "scientist"])
def test_recovery67_rechecks_both_schema_sources(
    no_admission_recovery_checkout, monkeypatch, which
):
    root, head = no_admission_recovery_checkout
    original = preflight._source_bytes
    relative = (
        preflight.NO_ADMISSION_RECOVERY_ADDITIONS[0]
        if which == "aos"
        else preflight.SCIENTIST_NO_ADMISSION_RECOVERY_SCHEMA
    )
    reads = 0

    def unstable(directory, name):
        nonlocal reads
        raw = original(directory, name)
        if name == relative:
            reads += 1
            return raw + b"\n" if reads > 1 else raw
        return raw

    monkeypatch.setattr(preflight, "_source_bytes", unstable)
    report = inspect_source(root, head, source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE)
    expected = (
        "source_changed_during_preflight"
        if which == "aos"
        else "scientist_no_admission_recovery_schema_changed_during_preflight"
    )
    assert expected in _codes(report)
    assert report["source_ready"] is False


def test_preparer_accepts_reviewed67_and_rechecks_new_schema(
    no_admission_recovery_checkout, tmp_path
):
    from scripts.prepare_aos_native_configuration import verified_source_receipt

    root, head = no_admission_recovery_checkout
    report = inspect_source(
        root,
        head,
        source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
        snapshot_kind="reviewed_snapshot",
        **_reviewed_pins(root, preflight.NO_ADMISSION_RECOVERY_FILES),
    )
    receipt = tmp_path / "source67-receipt.json"
    raw = json.dumps(report).encode()
    receipt.write_bytes(raw)
    pin = hashlib.sha256(raw).hexdigest()
    assert (
        verified_source_receipt(
            receipt,
            pin,
            required_source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
        )["source_ready"]
        is True
    )
    path = root / preflight.NO_ADMISSION_RECOVERY_ADDITIONS[0]
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed since review"):
        verified_source_receipt(
            receipt,
            pin,
            required_source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
        )


def test_explicit_recovery_profile_rejects_old66_receipt_before_work(
    no_admission_checkout, tmp_path, monkeypatch
):
    from scripts.prepare_aos_native_configuration import verified_source_receipt

    root, head = no_admission_checkout
    receipt = tmp_path / "old66.json"
    pin = _no_admission_receipt(root, head, receipt)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("old profile must reject before source inspection")

    monkeypatch.setattr(preflight, "inspect_source", forbidden)
    with pytest.raises(ValueError, match="explicit reviewed source profile differs"):
        verified_source_receipt(
            receipt,
            pin,
            required_source_profile=preflight.NO_ADMISSION_RECOVERY_PROFILE,
        )
