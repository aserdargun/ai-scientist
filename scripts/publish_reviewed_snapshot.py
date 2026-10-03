"""Publish only operator sealed public snapshots; never export a source worktree."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat

# Fixed Git argv, no shell; captured diagnostics are withheld.
import subprocess  # nosec B404
import sys
import tempfile
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

REMOTE = "https://github.com/aserdargun/ai-scientist.git"
PUBLIC_ROOT = "6a6b681cbd6ff383687200a4617c973b22c2d375"
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
MAX_FILES = 10000
MAX_BYTES = 100_000_000
FORBIDDEN = {
    ".git",
    ".codex",
    ".ssh",
    ".venv",
    "node_modules",
    "runtime",
    "data",
    "blobs",
    "models",
    "checkpoints",
    "sessions",
    "credentials",
    "private",
    "raw-records",
}
BINARY_SUFFIXES = {
    ".db",
    ".sqlite",
    ".safetensors",
    ".gguf",
    ".pt",
    ".pth",
    ".pkl",
    ".pickle",
    ".parquet",
    ".arrow",
    ".ipc",
    ".log",
    ".jsonl",
    ".pem",
    ".key",
    ".p12",
}
SECRETS = re.compile(
    rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
    rb"|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"
    rb"|\bAKIA[A-Z0-9]{16}\b|\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b"
)


class PublicationError(ValueError):
    """Safe error codes only; never subprocess or payload text."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PublicationError(code)


COST_START = b"<!-- api-cost-summary:start -->"
COST_END = b"<!-- api-cost-summary:end -->"


def update_readme_cost(readme: bytes, report: dict[str, Any]) -> bytes:
    """Replace only one exact marked block with bounded, deterministic report facts."""
    require(readme.count(COST_START) == readme.count(COST_END) == 1, "readme-cost-markers")
    start, end = readme.index(COST_START), readme.index(COST_END)
    require(
        start < end
        and (start == 0 or readme[start - 1 : start] == b"\n")
        and readme[start + len(COST_START) : start + len(COST_START) + 1] == b"\n"
        and readme[end - 1 : end] == b"\n"
        and (
            end + len(COST_END) == len(readme)
            or readme[end + len(COST_END) : end + len(COST_END) + 1] == b"\n"
        ),
        "readme-cost-markers",
    )
    try:
        dev, scenario = report["development_codex"], report["api_price_scenario"]
        require(scenario["actual_bill"] is False, "readme-cost-not-counterfactual")
        observed = dev["tokens"].get("total_tokens")
        priced = scenario["priced_subset_tokens"].get("total_tokens")
        local = report["product_runtime"]["local_model_tokens"].get("total_tokens")
        for value in (observed, priced, local):
            require(
                value is None or type(value) is int and 0 <= value <= 10**18, "readme-cost-counter"
            )
        require(observed is None or priced is None or priced <= observed, "readme-cost-counter")
        unpriced = None if observed is None or priced is None else observed - priced
        amount = scenario["usd_priced_subset"]
        if amount is None:
            cost = "Unknown / Bilinmiyor"
        else:
            require(
                isinstance(amount, str)
                and len(amount) <= 64
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", amount) is not None,
                "readme-cost-amount",
            )
            number = Decimal(amount)
            require(number.is_finite() and 0 <= number <= Decimal("1e18"), "readme-cost-amount")
            cost = f"USD {number:,.2f}"
        stamp = dev["last_observed_at"]
        if stamp is None:
            stamp = "Unknown / Bilinmiyor"
        else:
            require(isinstance(stamp, str) and len(stamp) <= 64, "readme-cost-timestamp")
            parsed = datetime.fromisoformat(stamp)
            require(parsed.tzinfo is not None, "readme-cost-timestamp")
            stamp = parsed.astimezone(UTC).isoformat()
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        if isinstance(error, PublicationError):
            raise
        raise PublicationError("readme-cost-report") from None

    def count(value: int | None) -> str:
        return "Unknown / Bilinmiyor" if value is None else f"{value:,}"

    body = (
        "\n## Hypothetical API cost / Varsayımsal API maliyeti\n\n"
        f"**{cost}** — Standard short-context API scenario; not an actual bill.\n"
        f"Last observed UTC / Son gözlem UTC: **{stamp}**.\n\n"
        f"Observed / Gözlenen: **{count(observed)}** tokens; "
        f"priced / fiyatlandırılan: **{count(priced)}**; "
        f"unpriced / fiyatlandırılamayan: **{count(unpriced)}**.\n"
        f"Separate local runtime / Ayrı yerel runtime: **{count(local)}** tokens; "
        "excluded from this cloud estimate.\n\n"
        "Actual API billing and subscription charges are unknown. "
        "Gerçek API faturası ve abonelik bedeli bilinmiyor; bu tutar varsayımsaldır.\n"
        "Coverage is incomplete; unknown cost is not zero. "
        "Kapsam eksiktir; bilinmeyen maliyet sıfır değildir.\n\n"
        "[Hourly details / Saatlik ayrıntılar](docs/usage/project-usage-latest.md) · "
        "[Latest JSON / Güncel JSON](docs/usage/project-usage-latest.json) · "
        "[Dated calculation / Tarihli hesap](docs/ai-scientist/131-api-cost-summary.md)\n"
    ).encode()
    return readme[: start + len(COST_START)] + body + readme[end:]


def private_path(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode), "symlink-private-path")
    require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "invalid-private-path-type",
    )
    require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "private-path-permissions")


def read_json(path: Path) -> dict[str, Any]:
    private_path(path)
    raw = path.read_bytes()
    require(len(raw) <= 4_000_000, "oversized-json")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "duplicate-json-key")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(PublicationError("nonfinite-json")),
        )
    except (UnicodeError, json.JSONDecodeError):
        raise PublicationError("invalid-json") from None
    require(isinstance(value, dict), "invalid-json-object")
    return value


def safe_name(name: Any, *, legacy: bool = False) -> str:
    require(isinstance(name, str) and bool(name), "invalid-payload-path")
    path = PurePosixPath(name)
    require(
        not path.is_absolute()
        and str(path) == name
        and all(part not in {".", ".."} for part in path.parts)
        and not any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in name),
        "unsafe-payload-path",
    )
    require(not any(part.lower() == ".git" for part in path.parts), "forbidden-git-path")
    if legacy:
        return name
    require(not any(part.lower() in FORBIDDEN for part in path.parts), "forbidden-payload-path")
    base = path.name.lower()
    require(not base.startswith(".env") or base == ".env.example", "dotenv-payload")
    require(
        ".private." not in base
        and not base.endswith(".private")
        and path.suffix.lower() not in BINARY_SUFFIXES,
        "private-or-binary-payload",
    )
    return name


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    private_path(path.parent, directory=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publication-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    # Fixed executable/options; explicit validated paths.
    result = subprocess.run(  # nosec B603
        [
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "gc.auto=0",
            "-C",
            str(repo),
            *args,
        ],
        input=input_bytes,
        capture_output=True,
        env=env,
        timeout=120,
        check=False,
    )
    require(result.returncode == 0, "git-command-failed:" + args[0])
    return result.stdout


def text_git(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8").strip()


def check_repo(config: dict[str, Any], *, test_local_remote: bool) -> tuple[Path, str]:
    require(config.get("schema") == "reviewed-publication-config.v1", "config-schema")
    require(config.get("branch") == "main", "branch-pin")
    remote = config.get("remote_url")
    root = config.get("public_root")
    require(
        isinstance(remote, str) and isinstance(root, str) and bool(COMMIT.fullmatch(root)),
        "invalid-remote-root",
    )
    if not test_local_remote:
        require(remote == REMOTE and root == PUBLIC_ROOT, "production-remote-root-pin")
    else:
        require(Path(remote).is_absolute() and Path(remote).is_dir(), "test-local-remote-only")
    require(
        config.get("publisher_sha256") == digest(Path(__file__).read_bytes()),
        "publisher-source-changed",
    )
    repo = Path(config["repo"])
    require(repo.is_absolute() and repo.is_dir() and not repo.is_symlink(), "invalid-repo")
    require(
        (repo / ".git").is_dir() and not (repo / ".git").is_symlink(),
        "ordinary-isolated-clone-required",
    )
    require(repo.resolve() == repo, "symlink-repo-ancestor")
    require(
        not any(
            (repo / ".git" / name).exists()
            for name in ("shallow", "info/grafts", "objects/info/alternates")
        ),
        "hidden-or-incomplete-ancestry",
    )
    require(
        not text_git(repo, "for-each-ref", "--format=%(refname)", "refs/replace/"),
        "replacement-ancestry",
    )
    require(
        Path(text_git(repo, "rev-parse", "--show-toplevel")) == repo
        and Path(text_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        == repo / ".git",
        "unexpected-git-directory",
    )
    require(text_git(repo, "branch", "--show-current") == "main", "unexpected-branch")
    require(text_git(repo, "remote") == "origin", "unexpected-remotes")
    require(
        text_git(repo, "config", "--get-all", "remote.origin.url") == remote, "remote-url-changed"
    )
    require(
        text_git(repo, "remote", "get-url", "--push", "--all", "origin") == remote,
        "effective-push-url-changed",
    )
    require(
        text_git(repo, "remote", "get-url", "--all", "origin") == remote,
        "effective-fetch-url-changed",
    )
    require(
        text_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        == "origin/main",
        "unexpected-upstream",
    )
    require(
        text_git(repo, "rev-list", "--max-parents=0", "HEAD") == root,
        "private-or-unexpected-ancestry",
    )
    require(not git(repo, "status", "--porcelain", "--untracked-files=all"), "dirty-public-clone")
    remote_head = text_git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main")
    pieces = remote_head.split()
    require(
        len(pieces) == 2 and pieces[1] == "refs/heads/main" and bool(COMMIT.fullmatch(pieces[0])),
        "invalid-remote-ref",
    )
    return repo, pieces[0]


def bundle(
    queue: Path, identity: str, repo: Path | None = None, config: dict[str, Any] | None = None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    require(bool(SHA.fullmatch(identity)), "invalid-manifest-id")
    root = queue / "ready" / identity
    for path in (queue, queue / "ready", root, root / "files"):
        private_path(path, directory=True)
    manifest = read_json(root / "manifest.json")
    require(digest(canonical(manifest)) == identity, "manifest-hash-mismatch")
    require(manifest.get("schema") == "reviewed-public-snapshot.v1", "manifest-schema")
    review = read_json(root / "review.json")
    require(
        digest(canonical(review)) == manifest.get("review_receipt_sha256"), "review-hash-mismatch"
    )
    files = manifest.get("files")
    require(isinstance(files, dict) and 0 < len(files) <= MAX_FILES, "invalid-file-map")
    require(
        review.get("schema") == "public-snapshot-review.v1"
        and review.get("approved") is True
        and review.get("reviewer") in {"operator", "authorized-deterministic-usage-pipeline"}
        and review.get("file_map_sha256") == digest(canonical(files)),
        "review-not-approved",
    )
    for field in ("quality_gate_receipt_sha256", "privacy_scan_receipt_sha256"):
        require(
            isinstance(review.get(field), str) and bool(SHA.fullmatch(review[field])),
            "missing-review-proof",
        )
    require(
        isinstance(manifest.get("expected_remote_head"), str)
        and bool(COMMIT.fullmatch(manifest["expected_remote_head"])),
        "invalid-base-head",
    )
    subject = manifest.get("commit_subject")
    require(
        isinstance(subject, str)
        and 1 <= len(subject) <= 120
        and all(32 <= ord(c) < 127 for c in subject),
        "invalid-commit-subject",
    )
    require(not SECRETS.search(subject.encode()), "secret-in-subject")
    deleted = manifest.get("delete_paths", [])
    require(isinstance(deleted, list) and len(set(deleted)) == len(deleted), "invalid-delete-paths")
    for name in deleted:
        safe_name(name)
        require(name not in files, "file-delete-overlap")
    published: dict[str, tuple[str, bytes]] = {}
    if repo is not None:
        for entry in git(repo, "ls-tree", "-r", "-z", "HEAD").split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, _ = metadata.decode().split()
            require(kind == "blob" and mode in {"100644", "100755"}, "unsupported-public-tree")
            published[name.decode()] = (mode, (repo / name.decode()).read_bytes())
    payload: dict[str, bytes] = {}
    size = 0
    for name, descriptor in files.items():
        safe_name(name, legacy=True)
        require(
            isinstance(descriptor, dict) and set(descriptor) == {"sha256", "size", "mode"},
            "invalid-file-descriptor",
        )
        require(
            isinstance(descriptor["sha256"], str)
            and bool(SHA.fullmatch(descriptor["sha256"]))
            and type(descriptor["size"]) is int
            and 0 <= descriptor["size"] <= MAX_BYTES
            and descriptor["mode"] in {"100644", "100755"},
            "invalid-file-metadata",
        )
        source = root / "files" / name
        for parent in source.parents:
            if parent == root / "files":
                break
            private_path(parent, directory=True)
        private_path(source)
        require(source.stat().st_size == descriptor["size"], "payload-size-mismatch")
        raw = source.read_bytes()
        require(
            len(raw) == descriptor["size"] and digest(raw) == descriptor["sha256"],
            "payload-hash-mismatch",
        )
        unchanged_public = published.get(name) == (descriptor["mode"], raw)
        if not unchanged_public:
            fixture_pins = config.get("reviewed_fixture_files", {}) if config else {}
            fixture = (
                review["reviewer"] == "operator"
                and re.fullmatch(
                    r"docs/ai-scientist/contracts/control-v1-draft/"
                    r"[A-Za-z0-9_-]+\.jsonl",
                    name,
                )
                and isinstance(fixture_pins, dict)
                and fixture_pins.get(name) == descriptor["sha256"]
            )
            safe_name(name, legacy=bool(fixture))
            require(not SECRETS.search(raw), "credential-signature-in-payload")
        size += len(raw)
        require(size <= MAX_BYTES, "payload-total-size")
        payload[name] = raw
    if review["reviewer"] == "authorized-deterministic-usage-pipeline":
        require(config is not None and repo is not None, "generated-review-without-config")
        pins = config.get("usage_pipeline")
        require(
            isinstance(pins, dict)
            and set(pins) == {"collector_sha256", "projector_sha256", "hourly_config_sha256"},
            "generated-pipeline-not-authorized",
        )
        require(
            all(isinstance(value, str) and SHA.fullmatch(value) for value in pins.values()),
            "invalid-pipeline-pins",
        )
        require(
            all(review.get(key) == value for key, value in pins.items())
            and review.get("publisher_sha256") == config["publisher_sha256"]
            and review.get("base_commit") == manifest["expected_remote_head"],
            "generated-review-pins-mismatch",
        )
        output_paths = {
            "docs/usage/project-usage-latest.json",
            "docs/usage/project-usage-latest.md",
            "README.md",
        }
        changed = {
            name
            for name, raw in payload.items()
            if published.get(name) != (files[name]["mode"], raw)
        }
        require(
            not deleted
            and changed <= output_paths
            and output_paths <= set(payload)
            and set(published) <= set(payload),
            "generated-overlay-path-violation",
        )
        require("README.md" in published, "generated-readme-missing-base")
        report = read_json(root / "files/docs/usage/project-usage-latest.json")
        require(
            manifest["files"]["README.md"]["mode"] == published["README.md"][0]
            and payload["README.md"] == update_readme_cost(published["README.md"][1], report),
            "generated-readme-outside-deterministic-block",
        )
        outputs = {name: files[name] for name in sorted(output_paths)}
        require(
            review.get("output_file_map_sha256") == digest(canonical(outputs)),
            "generated-output-map-mismatch",
        )
    actual = set()
    for path, dirs, names in os.walk(root / "files", followlinks=False):
        for name in dirs:
            private_path(Path(path) / name, directory=True)
        for name in names:
            item = Path(path) / name
            private_path(item)
            actual.add(item.relative_to(root / "files").as_posix())
    require(actual == set(payload), "unreviewed-extra-payload")
    return manifest, payload


def consume(queue: Path, identity: str) -> None:
    processed = queue / "processed"
    if not processed.exists():
        processed.mkdir(mode=0o700)
    private_path(processed, directory=True)
    source = queue / "ready" / identity
    if source.exists():
        private_path(source, directory=True)
        require(not (processed / identity).exists(), "duplicate-processed-bundle")
        os.replace(source, processed / identity)


def verify_index(repo: Path, manifest: dict[str, Any], payload: dict[str, bytes]) -> None:
    staged: dict[str, tuple[str, str]] = {}
    for entry in git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not entry:
            continue
        metadata, name = entry.split(b"\t", 1)
        mode, blob, stage = metadata.decode().split()
        require(stage == "0", "unmerged-index")
        staged[name.decode()] = (mode, blob)
    require(
        set(staged) == set(payload)
        and all(staged[name][0] == manifest["files"][name]["mode"] for name in payload),
        "staged-tree-differs-from-reviewed-payload",
    )
    names = sorted(staged)
    response = git(
        repo,
        "cat-file",
        "--batch",
        input_bytes=("\n".join(staged[name][1] for name in names) + "\n").encode(),
    )
    offset = 0
    for name in names:
        newline = response.index(b"\n", offset)
        blob, kind, size_text = response[offset:newline].decode().split()
        size = int(size_text)
        offset = newline + 1
        require(
            blob == staged[name][1]
            and kind == "blob"
            and size >= 0
            and size == manifest["files"][name]["size"]
            and digest(memoryview(response)[offset : offset + size])
            == manifest["files"][name]["sha256"],
            "staged-tree-differs-from-reviewed-payload",
        )
        offset += size
        require(response[offset : offset + 1] == b"\n", "invalid-git-batch-response")
        offset += 1
    require(offset == len(response), "invalid-git-batch-response")


def push_receipt(
    repo: Path, state: dict[str, Any], state_path: Path, remote_head: str, dry_run: bool
) -> dict[str, Any]:
    require(state.get("schema") == "reviewed-publication-state.v1", "invalid-state")
    require(
        state.get("remote_url") == text_git(repo, "remote", "get-url", "origin"),
        "state-remote-mismatch",
    )
    current = text_git(repo, "rev-parse", "HEAD")
    require(
        current == state.get("commit")
        and text_git(repo, "rev-parse", "HEAD^{tree}") == state.get("tree")
        and text_git(repo, "rev-parse", "HEAD^") == state.get("old_head"),
        "pending-commit-mismatch",
    )
    require(remote_head in {state["old_head"], current}, "remote-drift-pending")
    if dry_run:
        return {
            "status": "pending-push" if remote_head != current else "pending-ack",
            "commit": current,
        }
    if remote_head != current:
        git(repo, "push", "origin", "HEAD:refs/heads/main")
        observed = text_git(repo, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()
        require(len(observed) == 2 and observed[0] == current, "push-readback-mismatch")
    state["status"] = "published"
    atomic_json(state_path, state)
    return {"status": "published", "commit": current, "manifest_sha256": state["manifest"]}


def publish(
    config_path: Path,
    *,
    dry_run: bool = True,
    test_local_remote: bool = False,
    _lock_handle: Any = None,
) -> dict[str, Any]:
    config = read_json(config_path)
    state_path = Path(config["state_path"])
    queue = Path(config["queue"])
    require(
        queue.is_absolute() and state_path.is_absolute() and queue not in state_path.parents,
        "invalid-state-queue-layout",
    )
    private_path(queue, directory=True)
    private_path(state_path.parent, directory=True)
    lock_path = state_path.with_suffix(".lock")
    if lock_path.exists():
        private_path(lock_path)
    if _lock_handle is not None:
        require(
            os.fstat(_lock_handle.fileno()).st_ino == lock_path.stat().st_ino
            and os.fstat(_lock_handle.fileno()).st_dev == lock_path.stat().st_dev,
            "wrong-shared-lock",
        )
    lock_context = (
        nullcontext(_lock_handle)
        if _lock_handle is not None
        else os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a")
    )
    with lock_context as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicationError("publisher-already-running") from None
        repo, remote_head = check_repo(config, test_local_remote=test_local_remote)
        state = read_json(state_path) if state_path.exists() else None
        if state and state.get("status") == "committed":
            result = push_receipt(repo, state, state_path, remote_head, dry_run)
            if not dry_run:
                consume(queue, state["manifest"])
            return result
        if state and state.get("status") == "preparing":
            # Adopt a commit only when a crash occurred after commit and before receipt write.
            head = text_git(repo, "rev-parse", "HEAD")
            if head != state.get("old_head"):
                require(
                    text_git(repo, "rev-parse", "HEAD^") == state.get("old_head")
                    and text_git(repo, "rev-parse", "HEAD^{tree}") == state.get("tree")
                    and text_git(repo, "log", "-1", "--format=%B").endswith(
                        "Reviewed-Snapshot: " + state["manifest"]
                    ),
                    "unowned-pending-commit",
                )
                state.update(status="committed", commit=head)
                if not dry_run:
                    atomic_json(state_path, state)
                result = push_receipt(repo, state, state_path, remote_head, dry_run)
                if not dry_run:
                    consume(queue, state["manifest"])
                return result
        require(remote_head == text_git(repo, "rev-parse", "HEAD"), "unexpected-remote-head")
        private_path(queue / "ready", directory=True)
        if state and state.get("status") in {"published", "unchanged"} and not dry_run:
            consume(queue, state["manifest"])
        candidates = sorted(p.name for p in (queue / "ready").iterdir())
        if state and state.get("status") in {"published", "unchanged"}:
            candidates = [name for name in candidates if name != state.get("manifest")]
        require(len(candidates) <= 1, "multiple-ready-snapshots")
        if not candidates:
            return {"status": "empty-queue"}
        identity = candidates[0]
        manifest, payload = bundle(queue, identity, repo, config)
        require(manifest["expected_remote_head"] == remote_head, "review-base-head-changed")
        tracked = git(repo, "ls-files", "-z").decode().split("\0")[:-1]
        for name in tracked:
            require(not (repo / name).is_symlink(), "symlink-public-file")
        deleted = manifest.get("delete_paths", [])
        require(
            set(deleted) <= set(tracked) and set(tracked) <= set(payload) | set(deleted),
            "unapproved-snapshot-deletion",
        )
        unchanged = (
            not deleted
            and set(tracked) == set(payload)
            and all(
                (repo / name).read_bytes() == raw
                and ("100755" if (repo / name).stat().st_mode & 0o111 else "100644")
                == manifest["files"][name]["mode"]
                for name, raw in payload.items()
            )
        )
        if unchanged:
            if not dry_run:
                atomic_json(
                    state_path,
                    {
                        "schema": "reviewed-publication-state.v1",
                        "status": "unchanged",
                        "manifest": identity,
                        "commit": remote_head,
                    },
                )
                consume(queue, identity)
            return {"status": "unchanged", "commit": remote_head}
        if dry_run:
            return {"status": "ready", "manifest_sha256": identity, "files": len(payload)}
        # All bytes verified in memory; never copy from an unsealed working tree.
        for name in deleted:
            target = repo / name
            require(not target.is_symlink(), "symlink-public-file")
            target.unlink()
        for name, raw in payload.items():
            target = repo / name
            for parent in target.parents:
                if parent == repo:
                    break
                require(not parent.is_symlink(), "symlink-public-parent")
            require(not target.is_symlink(), "symlink-public-file")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o755 if manifest["files"][name]["mode"] == "100755" else 0o644)
        git(repo, "add", "--all", "--force", "--", *sorted(set(payload) | set(deleted)))
        verify_index(repo, manifest, payload)
        tree = text_git(repo, "write-tree")
        state = {
            "schema": "reviewed-publication-state.v1",
            "status": "preparing",
            "manifest": identity,
            "old_head": remote_head,
            "tree": tree,
            "remote_url": config["remote_url"],
        }
        atomic_json(state_path, state)
        git(repo, "commit", "-m", manifest["commit_subject"] + "\n\nReviewed-Snapshot: " + identity)
        state.update(status="committed", commit=text_git(repo, "rev-parse", "HEAD"))
        atomic_json(state_path, state)
        require(
            not git(repo, "status", "--porcelain", "--untracked-files=all"),
            "dirty-after-owned-commit",
        )
        result = push_receipt(repo, state, state_path, remote_head, False)
        consume(queue, identity)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = publish(args.config, dry_run=not args.execute)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PublicationError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": str(error)
                    if isinstance(error, PublicationError)
                    else type(error).__name__,
                }
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
