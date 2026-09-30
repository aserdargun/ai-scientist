"""Offline bounded four-source installer. No service/model/research is started.

Root runs this reviewed source in a 2GiB/one-CPU/240s scope. Migrator credentials
are consumed only here, never by API/Director; Planner independently reads back.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import resource
import shutil
import signal
import tempfile
import zipfile
from pathlib import Path
from typing import Any

for _thread_variable in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
):
    os.environ[_thread_variable] = "1"

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402

from lab.api.registry import (  # noqa: E402
    SuiteEntry,
    SuiteRegistry,
    SuiteRegistryFile,
    load_suite_registry,
)
from lab.director.journal import canonical_bytes  # noqa: E402
from lab.director.public_suite import (  # noqa: E402
    MaterializedPublicTask,
    finalize_public_suite_weights,
    install_scorer_task,
    materialize_smd_development,
    materialize_tsb_development,
)
from lab.director.suite_manifest import load_suite_manifest, write_suite_manifest  # noqa: E402
from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[1]
DATASETS = {"TSB-AD-M-Genesis", "TSB-AD-M-GECCO", "TSB-AD-M-CATSv2", "SMD"}
LIMITS = dict(
    source_bytes=2 * 1024**3,
    archive_member_bytes=40 * 1024**2,
    matrix_bytes=40 * 1024**2,
    manifest_bytes=64 * 1024**2,
    output_bytes=128 * 1024**2,
    minimum_free_disk_bytes=20 * 1024**3,
    memory_max_bytes=2 * 1024**3,
    cpu_quota_percent=100,
    wall_seconds=240,
    tasks_max=128,
    blas_threads=1,
)


def read_config(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 64 * 1024:
        raise ValueError("public preparation configuration is unsafe or oversized")
    config = json.loads(path.read_bytes())
    if (
        config.get("schema") != "public-four-source-baseline050.v1"
        or config.get("suite_id") != "public-four-source-evt-v1"
        or config.get("suite_version") != 3
        or config.get("allowed_purposes") != ["baseline"]
        or config.get("usage_profile") != "noncommercial_research"
        or config.get("expected_database") != "swapp_lab"
        or config.get("limits") != LIMITS
        or config.get("materialization")
        != dict(
            tsb_evaluation_rows=None,
            smd_entity="machine-1-1",
            smd_train_rows=12000,
            smd_evaluation_rows=12000,
        )
        or {t["dataset_id"] for t in config["tasks"]} != DATASETS
        or len(config["tasks"]) != 4
        or LIMITS["manifest_bytes"] > MAX_SUITE_MANIFEST_BYTES
    ):
        raise ValueError("public preparation policy differs from the approved four-source scope")
    return dict(config)


def source_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("source must be a clean repository-relative path")
    candidate = root / path
    if any((root / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts) + 1)):
        raise ValueError("source symlinks are not accepted")
    if not candidate.is_file() or root.resolve() not in candidate.resolve().parents:
        raise ValueError("required local source is missing")
    return candidate


def verify_inputs(root: Path, config: dict[str, Any]) -> None:
    records = [*config["metadata_files"], *config["raw_files"]]
    if len(records) != 14 or sum(item["bytes"] for item in records) > LIMITS["source_bytes"]:
        raise ValueError("pinned input inventory exceeds the fixed byte/count scope")
    for item in records:
        path = source_path(root, item["path"])
        if path.stat().st_size != item["bytes"]:
            raise ValueError("source byte size changed")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                raise ValueError("source content differs from pinned license/split/raw evidence")
    members = config["archive_members"]
    if (
        len(members) != 3
        or sum(m["uncompressed_bytes"] for m in members) > LIMITS["archive_member_bytes"]
    ):
        raise ValueError("selected archive members exceed the bounded scope")
    archive = source_path(root, config["raw_files"][0]["path"])
    with zipfile.ZipFile(archive) as source:
        for member in members:
            info = source.getinfo(member["path"])
            if (
                info.file_size != member["uncompressed_bytes"]
                or f"{info.CRC:08x}" != member["crc32"]
            ):
                raise ValueError("selected member identity changed")
    # Existing loaders repeat the original source/split checks. No archive extraction.


def require_bounded_scope() -> None:
    rows = Path("/proc/self/cgroup").read_text().splitlines()
    groups = [line[3:] for line in rows if line.startswith("0::")]
    if len(groups) != 1 or ".." in Path(groups[0]).parts:
        raise ValueError("bounded unified cgroup required")
    group = Path("/sys/fs/cgroup") / groups[0].lstrip("/")
    memory = (group / "memory.max").read_text().strip()
    swap = (group / "memory.swap.max").read_text().strip()
    cpu = (group / "cpu.max").read_text().split()
    pids = (group / "pids.max").read_text().strip()
    if (
        not memory.isdecimal()
        or int(memory) > LIMITS["memory_max_bytes"]
        or swap != "0"
        or len(cpu) != 2
        or not all(v.isdecimal() for v in cpu)
        or int(cpu[0]) > int(cpu[1])
        or not pids.isdecimal()
        or int(pids) > LIMITS["tasks_max"]
    ):
        raise ValueError("requires MemoryMax=2G, MemorySwapMax=0, CPUQuota=100%, TasksMax=128")
    signal.alarm(LIMITS["wall_seconds"])
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMITS["output_bytes"], LIMITS["output_bytes"]))


def verify_tasks(items: tuple[MaterializedPublicTask, ...], config: dict[str, Any]) -> None:
    expected = {item["dataset_id"]: item for item in config["tasks"]}
    if len(items) != 4 or {item.task.dataset_id for item in items} != DATASETS:
        raise ValueError("materializer did not produce the four selected sources")
    matrix_bytes = 0
    for item in items:
        task, reg = item.task, item.scorer_registration
        spec = expected[task.dataset_id]
        if (
            task.task_id != spec["task_id"]
            or task.family != "EVT"
            or reg.task_family != "EVT"
            or reg.visibility != "dev"
            or task.split_id != spec["split_id"]
            or task.independent_family != spec["independent_family"]
            or task.provenance.license_id != spec["license_id"]
            or task.provenance.usage_profile != "noncommercial_research"
            or task._train.shape != (spec["train_rows"], spec["columns"])
            or task._evaluation.shape != (spec["evaluation_rows"], spec["columns"])
            or max(spec["train_rows"], spec["evaluation_rows"]) > 150000
            or reg.sample_count != spec["evaluation_rows"]
        ):
            raise ValueError("materialized task changed source/license/family/split/row scope")
        usable = [
            label
            for label, masked in zip(reg.labels, reg.masked_samples, strict=True)
            if not masked
        ]
        if not usable or not any(usable) or all(usable) or len(usable) < reg.sliding_window:
            raise ValueError("fixed EVT evaluation is ineligible; boundaries will not be moved")
        matrix_bytes += task._train.nbytes + task._evaluation.nbytes
    if matrix_bytes > LIMITS["matrix_bytes"]:
        raise ValueError("four-source matrix exceeds its 40MiB scope")


def private_write(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError("private output symlink rejected")
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError("existing output has conflicting immutable bytes")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)


def connect_installer(path: Path, *, role: str, database: str) -> Engine:
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
        raise ValueError("installer credential must be a private bounded regular file")
    url = make_url(path.read_text().strip())
    if url.get_backend_name() != "postgresql" or url.username != role or url.database != database:
        raise ValueError("installer credential role/database differs from approved target")
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=30000"},
    )
    try:
        with engine.connect() as connection:
            actual = connection.execute(text("SELECT session_user,current_database()")).one()
            if tuple(actual) != (role, database):
                raise ValueError("actual installer authority differs")
    except BaseException:
        engine.dispose()
        raise
    return engine


def publish_entry(registry_path: Path, runtime: Path, entry: SuiteEntry) -> str:
    if registry_path.is_symlink() or registry_path.stat().st_mode & 0o077:
        raise ValueError("target registry must be an existing private regular file")
    lock = os.open(
        registry_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry = load_suite_registry(registry_path, runtime)
        entries = dict(registry.entries)
        if entry.suite_id in entries and entries[entry.suite_id] != entry:
            raise ValueError("existing registry entry conflicts with public baseline identity")
        entries[entry.suite_id] = entry
        document = SuiteRegistryFile(schema="lab-suite-registry.v1", suites=tuple(entries.values()))
        payload = canonical_bytes(document.model_dump(mode="json", by_alias=True))
        fd, tmp = tempfile.mkstemp(prefix=".public-registry-", dir=registry_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, registry_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        verified = load_suite_registry(registry_path, runtime)
        if verified.get(entry.suite_id) != entry:
            raise ValueError("registry readback differs")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(lock)


def prepare(
    *,
    root: Path,
    config: dict[str, Any],
    destination: Path,
    registration: Engine | None = None,
    planner: Engine | None = None,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    runtime = (root / "data/runtime").resolve(strict=True)
    target = destination.resolve()
    if runtime not in target.parents or destination.is_symlink():
        raise ValueError("destination must remain under private runtime")
    if shutil.disk_usage(runtime).free < LIMITS["minimum_free_disk_bytes"] + LIMITS["output_bytes"]:
        raise ValueError("preparation would breach the fixed 20GiB reserve")
    verify_inputs(root, config)
    items = (
        *materialize_tsb_development(repository_root=root, evaluation_rows=None),
        *materialize_smd_development(
            repository_root=root, entity_limit=1, train_rows=12000, evaluation_rows=12000
        ),
    )
    verify_tasks(items, config)
    weighted, weights = finalize_public_suite_weights(items)
    if weights.family_cap != 0.25 or len(weights.family_shares) != 4:
        raise ValueError("four-source weighting changed")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.stat().st_mode & 0o077:
        raise ValueError("output directory must be private")
    staging = destination / ".suite-preparation.json"
    if staging.exists():
        raise ValueError("unfinished suite preparation needs operator review")
    count, digest = write_suite_manifest(
        tuple(item.task for item in weighted), staging, suite_id=config["suite_id"], suite_version=3
    )
    if count > LIMITS["manifest_bytes"]:
        raise ValueError("prepared manifest exceeds fixed64MiB scope")
    suite_path = destination / "suite.json"
    private_write(suite_path, staging.read_bytes())
    staging.unlink()
    scenario = canonical_bytes({"schema": "review-fake-provider-input.v1", "proposals": []})
    scenario_path = destination / "baseline-only-scenario.json"
    private_write(scenario_path, scenario)
    entry = SuiteEntry(
        suite_id=config["suite_id"],
        track="anomaly",
        program_version=config["program_version"],
        suite_manifest_path=str(suite_path.relative_to(runtime)),
        suite_manifest_sha256=digest,
        provider="fake-json",
        scenario_path=str(scenario_path.relative_to(runtime)),
        scenario_sha256=hashlib.sha256(scenario).hexdigest(),
        proposal_limit=1,
        allowed_purposes=("baseline",),
    )
    SuiteRegistry((entry,), runtime).verify_entry(entry)
    from lab.api.contracts import BaselineBudget, StartBaselineRequest

    baseline_request = StartBaselineRequest(
        idempotency_key="public050-baseline-" + digest[:24],
        suite=entry.suite_id,
        program_version=entry.program_version,
        budget=BaselineBudget(experiments=0, wall_seconds=7200, model_tokens=0),
    )
    private_write(
        destination / "baseline-request.json",
        canonical_bytes(baseline_request.model_dump(mode="json")),
    )
    private_write(
        destination / "registry-entry.json", canonical_bytes(entry.model_dump(mode="json"))
    )
    record = {
        "schema": "public-baseline050-preparation.v1",
        "suite_id": entry.suite_id,
        "suite_version": 3,
        "manifest_sha256": digest,
        "manifest_bytes": count,
        "task_count": 4,
        "planned_score_count": 36,
        "measured_score_count": 0,
        "provider_constructed": False,
        "models_enabled": False,
        "allowed_purposes": ["baseline"],
        "usage_profile": "noncommercial_research",
        "source_plan_sha256": hashlib.sha256(canonical_bytes(config)).hexdigest(),
        "registry_entry_sha256": SuiteRegistry.entry_sha256(entry),
        "profiles": [
            {
                "dataset_id": item.task.dataset_id,
                "profile_sha256": item.task.profile_sha256,
                "split_id": item.task.split_id,
                "family": item.task.independent_family,
                "license": item.task.provenance.license_id,
            }
            for item in weighted
        ],
        "matrix_bytes": sum(
            item.task._train.nbytes + item.task._evaluation.nbytes for item in weighted
        ),
        "installation": "not_run",
        "planner_readback": "not_run",
        "registry_publication": "not_run",
        "scope": (
            "Three fixed TSB members plus SMD machine-1-1 12k official-split slice; "
            "not full-dataset acceptance"
        ),
    }
    if registration is not None:
        if planner is None or registry_path is None:
            raise ValueError("installation requires Planner readback and registry")
        for item in weighted:
            install_scorer_task(registration, item.scorer_registration)
        document, loaded, observed = load_suite_manifest(suite_path, planner)
        if observed != digest or document.suite_version != 3 or len(loaded) != 4:
            raise ValueError("independent Planner readback differs")
        record.update(
            installation="passed",
            planner_readback="passed",
            registry_publication="passed",
            registry_sha256=publish_entry(registry_path, runtime, entry),
        )
    private_write(
        destination / ("installed.json" if registration is not None else "prepared.json"),
        canonical_bytes(record),
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=REPOSITORY)
    parser.add_argument(
        "--config", type=Path, default=REPOSITORY / "ops/public-four-source-050.json"
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--registration-dsn-file", type=Path)
    parser.add_argument("--planner-dsn-file", type=Path)
    parser.add_argument("--registry-file", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    config = read_config(args.config)
    require_bounded_scope()
    registration = planner = None
    try:
        if args.install:
            if not all((args.registration_dsn_file, args.planner_dsn_file, args.registry_file)):
                raise ValueError(
                    "offline installation requires explicit trusted role files and registry"
                )
            registration = connect_installer(
                args.registration_dsn_file,
                role="swapp_lab_migrator",
                database=config["expected_database"],
            )
            planner = connect_installer(
                args.planner_dsn_file,
                role="swapp_lab_planner",
                database=config["expected_database"],
            )
        result = prepare(
            root=args.source_root.resolve(strict=True),
            config=config,
            destination=args.destination,
            registration=registration,
            planner=planner,
            registry_path=args.registry_file,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        if registration is not None:
            registration.dispose()
        if planner is not None:
            planner.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
