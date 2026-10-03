"""Local bare-remote publication boundaries and retry proofs; no GitHub traffic."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "reviewed_publisher", Path(__file__).parents[1] / "scripts/publish_reviewed_snapshot.py"
)
assert SPEC and SPEC.loader
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def run(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], stderr=subprocess.DEVNULL, text=True
    ).strip()


def write_private(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(publisher.canonical(value))
    path.chmod(0o600)


@pytest.fixture
def setup(tmp_path: Path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    remote.mkdir()
    repo.mkdir()
    run(remote, "init", "--bare", "--initial-branch=main")
    run(repo, "init", "--initial-branch=main")
    run(repo, "config", "user.name", "Publication test")
    run(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("initial reviewed public source\n")
    run(repo, "add", "README.md")
    run(repo, "commit", "-m", "Public root")
    root = run(repo, "rev-parse", "HEAD")
    run(repo, "remote", "add", "origin", str(remote))
    run(repo, "push", "--set-upstream", "origin", "main")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    queue = private / "queue"
    (queue / "ready").mkdir(mode=0o700, parents=True)
    queue.chmod(0o700)
    config_path = private / "config.json"
    config = {
        "schema": "reviewed-publication-config.v1",
        "repo": str(repo),
        "queue": str(queue),
        "state_path": str(private / "state.json"),
        "remote_url": str(remote),
        "branch": "main",
        "public_root": root,
        "publisher_sha256": publisher.digest(Path(publisher.__file__).read_bytes()),
    }
    write_private(config_path, config)
    return config_path, config, repo, remote


def seal(setup, files=None, **changes):
    _, config, repo, _ = setup
    files = files or {"README.md": b"new reviewed public source\n"}
    descriptors = {
        name: {"sha256": publisher.digest(raw), "size": len(raw), "mode": "100644"}
        for name, raw in files.items()
    }
    review = {
        "schema": "public-snapshot-review.v1",
        "file_map_sha256": publisher.digest(publisher.canonical(descriptors)),
        "quality_gate_receipt_sha256": "a" * 64,
        "privacy_scan_receipt_sha256": "b" * 64,
        "reviewer": "operator",
        "approved": True,
    }
    manifest = {
        "schema": "reviewed-public-snapshot.v1",
        "expected_remote_head": run(repo, "rev-parse", "HEAD"),
        "files": descriptors,
        "commit_subject": "Publish reviewed snapshot",
        "review_receipt_sha256": publisher.digest(publisher.canonical(review)),
        **changes,
    }
    identity = publisher.digest(publisher.canonical(manifest))
    root = Path(config["queue"]) / "ready" / identity
    (root / "files").mkdir(mode=0o700, parents=True)
    root.chmod(0o700)
    write_private(root / "manifest.json", manifest)
    write_private(root / "review.json", review)
    for name, raw in files.items():
        target = root / "files" / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(raw)
        target.chmod(0o600)
        for parent in target.parents:
            parent.chmod(0o700)
            if parent == root / "files":
                break
    return root, identity


def publish(setup, **kwargs):
    return publisher.publish(setup[0], test_local_remote=True, **kwargs)


def test_empty_and_dry_run_preserve_public_clone(setup):
    assert publish(setup)["status"] == "empty-queue"
    seal(setup)
    old = run(setup[2], "rev-parse", "HEAD")
    assert publish(setup)["status"] == "ready"
    assert run(setup[2], "rev-parse", "HEAD") == old
    assert run(setup[2], "status", "--porcelain") == ""


def test_exact_snapshot_published_once_and_no_change(setup):
    _, identity = seal(setup)
    result = publish(setup, dry_run=False)
    assert result["status"] == "published"
    assert result["manifest_sha256"] == identity
    assert run(setup[3], "rev-parse", "main") == result["commit"]
    assert run(setup[2], "rev-list", "--count", "HEAD") == "2"
    assert publish(setup, dry_run=False)["status"] == "empty-queue"


def test_unchanged_tree_has_no_commit_or_push(setup):
    seal(setup, {"README.md": (setup[2] / "README.md").read_bytes()})
    assert publish(setup, dry_run=False)["status"] == "unchanged"
    assert run(setup[2], "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        ".git/config",
        "data/raw.json",
        ".env",
        "docs/run.private.json",
        "session.jsonl",
        "model.gguf",
    ],
)
def test_forbidden_paths_never_enter_git(setup, path):
    seal(setup, {"README.md": b"okay", path: b"private"})
    with pytest.raises(publisher.PublicationError):
        publish(setup, dry_run=False)
    assert run(setup[2], "rev-list", "--count", "HEAD") == "1"


def test_credentials_never_enter_git(setup):
    seal(setup, {"README.md": b"ghp_" + b"x" * 40})
    with pytest.raises(publisher.PublicationError, match="credential-signature"):
        publish(setup, dry_run=False)
    assert run(setup[2], "status", "--porcelain") == ""


@pytest.mark.parametrize("mutation", ["tamper", "symlink", "extra", "public-permissions"])
def test_sealed_payload_boundary(setup, mutation):
    root, _ = seal(setup)
    file = root / "files/README.md"
    if mutation == "tamper":
        file.write_bytes(b"changed")
    elif mutation == "symlink":
        file.unlink()
        file.symlink_to(setup[2] / "README.md")
    elif mutation == "extra":
        (root / "files/extra.txt").write_text("unreviewed")
    else:
        file.chmod(0o644)
    with pytest.raises(publisher.PublicationError):
        publish(setup, dry_run=False)
    assert run(setup[2], "status", "--porcelain") == ""


def test_dirty_clone_and_remote_drift_reject(setup):
    seal(setup)
    (setup[2] / "unrelated.txt").write_text("user work")
    with pytest.raises(publisher.PublicationError, match="dirty-public-clone"):
        publish(setup, dry_run=False)
    (setup[2] / "unrelated.txt").unlink()
    run(setup[2], "commit", "--allow-empty", "-m", "Unreviewed local commit")
    with pytest.raises(publisher.PublicationError, match="unexpected-remote-head"):
        publish(setup, dry_run=False)


def test_changed_remote_upstream_and_publisher_reject(setup):
    seal(setup)
    config = setup[1]
    config["publisher_sha256"] = "f" * 64
    write_private(setup[0], config)
    with pytest.raises(publisher.PublicationError, match="publisher-source-changed"):
        publish(setup)
    config["publisher_sha256"] = publisher.digest(Path(publisher.__file__).read_bytes())
    write_private(setup[0], config)
    run(setup[2], "config", "remote.origin.pushurl", str(setup[3]) + "-other")
    with pytest.raises(publisher.PublicationError, match="effective-push-url-changed"):
        publish(setup)


def test_failed_push_retries_same_commit_and_unknown_success_ack(setup, monkeypatch):
    seal(setup)
    real_git = publisher.git

    def fail_push(repo, *args, **kwargs):
        if args[0] == "push":
            raise publisher.PublicationError("git-command-failed:push")
        return real_git(repo, *args, **kwargs)

    monkeypatch.setattr(publisher, "git", fail_push)
    with pytest.raises(publisher.PublicationError, match="push"):
        publish(setup, dry_run=False)
    commit = run(setup[2], "rev-parse", "HEAD")
    monkeypatch.setattr(publisher, "git", real_git)
    result = publish(setup, dry_run=False)
    assert result["commit"] == commit
    assert run(setup[2], "rev-list", "--count", "HEAD") == "2"
    state_path = Path(setup[1]["state_path"])
    state = json.loads(state_path.read_text())
    state["status"] = "committed"
    write_private(state_path, state)
    assert publish(setup, dry_run=False)["commit"] == commit


def test_private_ancestry_and_implicit_deletion_reject(setup):
    seal(setup, {"docs/new.md": b"new"})
    with pytest.raises(publisher.PublicationError, match="unapproved-snapshot-deletion"):
        publish(setup, dry_run=False)
    run(setup[2], "checkout", "--orphan", "private")
    run(setup[2], "add", "README.md")
    run(setup[2], "commit", "-m", "Private unrelated root")
    run(setup[2], "branch", "-D", "main")
    run(setup[2], "branch", "-m", "main")
    run(setup[2], "branch", "--set-upstream-to=origin/main")
    with pytest.raises(publisher.PublicationError, match="private-or-unexpected-ancestry"):
        publish(setup)


def test_next_reviewed_snapshot_after_success(setup):
    seal(setup)
    first = publish(setup, dry_run=False)
    seal(setup, {"README.md": b"second reviewed source\n"})
    second = publish(setup, dry_run=False)
    assert first["commit"] != second["commit"]
    assert run(setup[2], "rev-list", "--count", "HEAD") == "3"
    assert len(list((Path(setup[1]["queue"]) / "processed").iterdir())) == 2


def test_commit_before_receipt_crash_adopts_owned_commit(setup, monkeypatch):
    seal(setup)
    original = publisher.atomic_json

    def crash_after_commit(path, value):
        if value.get("status") == "committed":
            raise OSError("simulated interrupted receipt write")
        original(path, value)

    monkeypatch.setattr(publisher, "atomic_json", crash_after_commit)
    with pytest.raises(OSError):
        publish(setup, dry_run=False)
    commit = run(setup[2], "rev-parse", "HEAD")
    monkeypatch.setattr(publisher, "atomic_json", original)
    assert publish(setup, dry_run=False)["commit"] == commit
    assert run(setup[2], "rev-list", "--count", "HEAD") == "2"


def test_transformed_git_staging_is_not_published(setup):
    run(setup[2], "config", "filter.bad.clean", "sed s/reviewed/altered/g")
    seal(setup, {"README.md": b"reviewed source\n", ".gitattributes": b"README.md filter=bad\n"})
    with pytest.raises(publisher.PublicationError, match="staged-tree-differs"):
        publish(setup, dry_run=False)
    assert run(setup[3], "rev-list", "--count", "main") == "1"


def test_explicit_deletion_is_reviewed_and_limited(setup):
    seal(setup, {"docs/new.md": b"new reviewed source\n"}, delete_paths=["README.md"])
    result = publish(setup, dry_run=False)
    assert result["status"] == "published"
    assert not (setup[2] / "README.md").exists()
    assert run(setup[2], "ls-files") == "docs/new.md"


def test_push_succeeded_but_network_result_unknown_no_duplicate(setup, monkeypatch):
    seal(setup)
    real_git = publisher.git

    def ambiguous_push(repo, *args, **kwargs):
        result = real_git(repo, *args, **kwargs)
        if args[0] == "push":
            raise publisher.PublicationError("git-command-failed:push")
        return result

    monkeypatch.setattr(publisher, "git", ambiguous_push)
    with pytest.raises(publisher.PublicationError):
        publish(setup, dry_run=False)
    commit = run(setup[2], "rev-parse", "HEAD")
    assert run(setup[3], "rev-parse", "main") == commit
    monkeypatch.setattr(publisher, "git", real_git)
    assert publish(setup, dry_run=False)["commit"] == commit
    assert run(setup[2], "rev-list", "--count", "HEAD") == "2"


def test_single_publisher_lock(setup):
    import fcntl

    lock_path = Path(setup[1]["state_path"]).with_suffix(".lock")
    write_private(lock_path, {})
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(publisher.PublicationError, match="publisher-already-running"):
            publish(setup, dry_run=False)


@pytest.mark.parametrize("name", ["shallow", "info/grafts", "objects/info/alternates"])
def test_hidden_or_incomplete_ancestry_rejected(setup, name):
    path = setup[2] / ".git" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    with pytest.raises(publisher.PublicationError, match="hidden-or-incomplete-ancestry"):
        publish(setup)


def test_replace_refs_rejected(setup):
    head = run(setup[2], "rev-parse", "HEAD")
    run(setup[2], "update-ref", "refs/replace/" + "f" * 40, head)
    with pytest.raises(publisher.PublicationError, match="replacement-ancestry"):
        publish(setup)


def test_unchanged_previously_public_legacy_blob_only(setup):
    repo = setup[2]
    (repo / "public-contract.jsonl").write_text('{"type":"public-fixture"}\n')
    run(repo, "add", "public-contract.jsonl")
    run(repo, "commit", "-m", "Existing reviewed public contract")
    run(repo, "push", "origin", "main")
    files = {
        "README.md": b"new reviewed source\n",
        "public-contract.jsonl": (repo / "public-contract.jsonl").read_bytes(),
    }
    seal(setup, files)
    assert publish(setup, dry_run=False)["status"] == "published"
    files["public-contract.jsonl"] = b'{"type":"changed-raw-record"}\n'
    seal(setup, files)
    with pytest.raises(publisher.PublicationError, match="private-or-binary-payload"):
        publish(setup, dry_run=False)
    assert run(repo, "status", "--porcelain") == ""


def test_exact_operator_reviewed_synthetic_fixture_pin(setup):
    name = "docs/ai-scientist/contracts/control-v1-draft/admission_binding-positive.jsonl"
    raw = b'{"synthetic_fixture":true}\n'
    setup[1]["reviewed_fixture_files"] = {name: publisher.digest(raw)}
    write_private(setup[0], setup[1])
    seal(setup, {"README.md": b"reviewed source\n", name: raw})
    assert publish(setup, dry_run=False)["status"] == "published"
    changed = b'{"unexpected_raw_record":true}\n'
    seal(setup, {"README.md": b"reviewed source\n", name: changed})
    with pytest.raises(publisher.PublicationError, match="private-or-binary-payload"):
        publish(setup, dry_run=False)
