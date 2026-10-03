"""Install bounded synthetic local research and independent private holdout.

No model activation or run admission. Existing credentials and principal identities
are retained. This configures an existing synthetic.baseline-proof.v1 installation;
it does not create a cold-start database/profile/model installation. Synthetic
results do not establish public dataset acceptance. Service configuration is
prepared separately and never activated or restarted by this command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data/runtime"
sys.path.insert(0, str(ROOT))
STAGE = "arguments"
PREPARATION_STAGE = "arguments"
PREPARATION_OUTPUT_CREATED = False


def preparation_stage(value: str) -> None:
    global PREPARATION_STAGE
    PREPARATION_STAGE = value


def private_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("expected an absolute private file path")
    for parent in reversed(path.parents):
        info = parent.lstat()
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or info.st_mode & 0o022
            and not sticky_root
        ):
            raise ValueError("private file path has an unsafe parent")
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 1 <= info.st_size <= 16 * 1024
    ):
        raise ValueError("expected a private current-owned regular file")
    return path


def dump_private(path: Path, value: object) -> str:
    payload = (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode()
        + b"\n"
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(payload).hexdigest()


def prepare_development(args: argparse.Namespace) -> dict[str, object]:
    global PREPARATION_OUTPUT_CREATED
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from lab.api.contracts import StartRunRequest
    from lab.api.registry import SuiteEntry, SuiteRegistryFile, load_principals, load_suite_registry
    from lab.director.baselines import baseline_candidate_source
    from lab.director.fake_llm import AgentContext
    from lab.director.local_llm import LocalQwenProposalProvider, provider_config_sha256
    from lab.director.strategy import (
        initial_strategy_state,
        resolve_strategy_selection,
        select_strategy_move,
    )
    from lab.director.suite_manifest import load_suite_manifest
    from lab.llm.gpu_scheduler import SystemdPrincipalResolver
    from lab.llm.router import route_system

    os.umask(0o077)
    preparation_stage("validate_private_inputs")
    output = args.output_runtime
    if (
        not output.is_absolute()
        or ".." in output.parts
        or output.parent != RUNTIME
        or output.exists()
        or output.is_symlink()
        or RUNTIME.resolve(strict=True) != RUNTIME
    ):
        raise ValueError("output must be a new direct child of the Scientist runtime")
    url = make_url(private_path(args.planner_dsn_file).read_text().strip())
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 55432
        or url.database != args.expected_database
        or url.username != "swapp_lab_planner"
        or not url.password
        or url.query
    ):
        raise ValueError("Planner credentials differ from the expected main deployment")
    source_registry = load_suite_registry(args.source_registry_file, RUNTIME)
    try:
        original = source_registry.get("synthetic.baseline-proof.v1")
    except (KeyError, ValueError) as error:
        raise ValueError(
            "Existing synthetic.baseline-proof.v1 profile installation is required; "
            "this command is not an open-core cold-start installer"
        ) from error
    source_suite, _ = source_registry.verify_entry(original)
    principals = load_principals(private_path(args.source_principals_file))
    locals_ = [principal for principal in principals if principal.origin == "local"]
    if len(locals_) != 1:
        raise ValueError("main console must have exactly one identifiable local owner")
    engine = create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=5,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c default_transaction_read_only=on -c statement_timeout=5000",
        },
    )
    try:
        preparation_stage("validate_main_planner_identity")
        with engine.connect() as connection:
            identity = (
                connection.execute(
                    text(
                        "SELECT current_database() AS database,"
                        "current_user AS role,session_user AS session_role,"
                        "current_setting('transaction_read_only') AS readonly"
                    )
                )
                .mappings()
                .one()
            )
            if (
                identity["database"] != args.expected_database
                or identity["role"] != "swapp_lab_planner"
                or identity["session_role"] != "swapp_lab_planner"
                or identity["readonly"] != "on"
            ):
                raise ValueError("actual Planner database or read-only binding differs")
        preparation_stage("validate_original_suite_profiles")
        document, tasks, source_sha = load_suite_manifest(source_suite, engine)
        if len(tasks) != 4 or len({task.independent_family for task in tasks}) != 4:
            raise ValueError("expected four trusted independently seeded synthetic dev tasks")
        preparation_id = uuid4()
        suite_id = "synthetic.local-qwen-delivery." + preparation_id.hex
        suite_document = document.model_dump(mode="json", by_alias=True)
        suite_document["suite_id"] = suite_id
        output.mkdir(mode=0o700)
        PREPARATION_OUTPUT_CREATED = True
        preparation_stage("write_and_validate_copied_suite")
        suite_file = output / "suite.json"
        suite_sha = dump_private(suite_file, suite_document)
        copied_document, copied_tasks, verified_sha = load_suite_manifest(suite_file, engine)
        if (
            copied_document.suite_id != suite_id
            or verified_sha != suite_sha
            or len(copied_tasks) != len(tasks)
        ):
            raise ValueError("copied suite failed independent main Planner verification")
        config_sha = provider_config_sha256("research")
        preparation_stage("write_private_registry_principal_and_admission")
        entry = SuiteEntry(
            suite_id=suite_id,
            track="anomaly",
            program_version="bounded-local-delivery.v1",
            suite_manifest_path=str(suite_file.relative_to(RUNTIME)),
            suite_manifest_sha256=suite_sha,
            provider="local-qwen",
            provider_config_sha256=config_sha,
            proposal_limit=6,
            allowed_purposes=("research",),
        )
        registry_file = output / "registry.json"
        registry_sha = dump_private(
            registry_file,
            SuiteRegistryFile(schema="lab-suite-registry.v1", suites=(entry,)).model_dump(
                mode="json", by_alias=True
            ),
        )
        registry = load_suite_registry(registry_file, RUNTIME)
        admission = StartRunRequest(
            idempotency_key="delivery-research-" + preparation_id.hex,
            track="anomaly",
            suite=suite_id,
            program_version=entry.program_version,
            budget={"experiments": 6, "wall_seconds": 7200, "model_tokens": 180000},
        )
        admission_file = output / "admission.json"
        dump_private(admission_file, admission.model_dump(mode="json", exclude_none=True))

        # No resolver invocation, scheduler construction, model activation or model request.
        preparation_stage("construct_offline_preflight_provider")
        provider = LocalQwenProposalProvider(
            run_id=preparation_id,
            owner="lab",
            principal_resolver=SystemdPrincipalResolver(
                {
                    "lab": "swapp-lab-gpu-delivery-preflight.service",
                    "aos": "swapp-aos-gpu-delivery.service",
                }
            ),
            runtime_database=Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3",
            registry_entry_sha256=registry.entry_sha256(entry),
            profile_set="research",
        )
        source = baseline_candidate_source("robust_z").decode()
        cards = tuple(
            f"task={task.task_id}; family={task.family}; signals="
            + ",".join(task.context.signals)
            + f"; sampling_s={task.context.sampling_s}; task_weight={task.task_weight}"
            for task in copied_tasks
        )
        state = initial_strategy_state(0)
        movements = []
        for ordinal in range(1, 7):
            preparation_stage(f"preflight_strategy_context_{ordinal}")
            state, selection = select_strategy_move(state, ordinal)
            system = route_system(
                "experiment", selection.move_type, explore=False, discard_streak=0
            )
            context = AgentContext(
                phase="proposal",
                experiment_number=ordinal,
                system=system,
                move_type=selection.move_type,
                task_cards=cards,
                champion_source=source,
                recent_feedback=(),
            )
            preparation_stage(f"preflight_offline_tokenizer_{ordinal}")
            _, token_count = provider._select_messages(
                context, provider._profile_for_system(system), timeout_seconds=30
            )
            preparation_stage(f"preflight_json_ast_{ordinal}")
            provider._parse_proposal(
                json.dumps(
                    {
                        "hypothesis": "CPU contract preflight only",
                        "move_type": selection.move_type,
                        "candidate_source": source,
                        "predicted_delta": 0.0,
                    }
                ),
                expected_move=selection.move_type,
            )
            movements.append(
                {
                    "ordinal": ordinal,
                    "move_type": selection.move_type,
                    "system": system,
                    "initial_prompt_tokens": token_count,
                }
            )
            state = resolve_strategy_selection(state, ordinal=ordinal, outcome="DISCARD")
        preparation_stage("write_preparation_receipt")
        result = {
            "schema": "scientist-ui-visible-research-preparation.v1",
            "preparation_id": str(preparation_id),
            "runtime_directory": str(output),
            "suite_id": suite_id,
            "suite_file": str(suite_file),
            "suite_manifest_sha256": suite_sha,
            "source_suite_sha256": source_sha,
            "registry_file": str(registry_file),
            "registry_sha256": registry_sha,
            "provider_config_sha256": config_sha,
            "provider_registry_entry_sha256": registry.entry_sha256(entry),
            "principals_file": str(args.source_principals_file),
            "admission_file": str(admission_file),
            "api_port": 18591,
            "main_console_owner_preserved": True,
            "main_planner_read_only": True,
            "trusted_task_count": len(tasks),
            "baseline_measurements_expected": 36,
            "budget": admission.budget.model_dump(mode="json"),
            "planned_moves": movements,
            "sql_writes": False,
            "run_admitted": False,
            "gpu_allocated": False,
            "model_research_acceptance": False,
            "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        dump_private(output / "prepared.private.json", result)
        return result
    finally:
        engine.dispose()


def stage(value: str) -> None:
    global STAGE
    STAGE = value


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def engine_for(path: Path, *, role: str, database: str, readonly: bool):
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(private_path(path).read_text().strip())
    if (
        url.drivername != "postgresql+psycopg"
        or url.host != "127.0.0.1"
        or url.port != 55432
        or url.database != database
        or url.username != role
        or not url.password
        or url.query
    ):
        raise ValueError("deployment credential binding differs")
    options = "-c statement_timeout=10000"
    if readonly:
        options += " -c default_transaction_read_only=on"
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=1,
        max_overflow=0,
        connect_args={"connect_timeout": 5, "options": options},
    )
    with engine.connect() as connection:
        identity = connection.execute(
            text("SELECT current_database(),current_user,session_user")
        ).one()
        if tuple(identity) != (database, role, role):
            engine.dispose()
            raise ValueError("actual database role differs")
    return engine


def prepare(args: argparse.Namespace) -> dict[str, object]:
    import numpy as np
    import pandas as pd
    from sqlalchemy import text

    from harness.contracts import FitContext
    from lab.director.public_suite import (
        ScorerTaskRegistration,
        install_scorer_task,
        task_semantics_sha256,
    )
    from lab.scorer.holdout import HoldoutTaskSpec, register_holdout_suite

    os.umask(0o077)
    # Existing helper proves exact development profiles and all six offline
    # tokenizer/JSON/AST contexts before any holdout database write.
    stage("development_profiles_and_six_offline_contexts")
    prepared = prepare_development(args)
    stage("generate_distinct_private_holdout")
    suite = json.loads(Path(prepared["suite_file"]).read_text())
    tasks = []
    registrations = []
    private_records = []
    dev_matrix_hashes = {
        digest(task[field]) for task in suite["tasks"] for field in ("train", "evaluation")
    }
    for index, dev in enumerate(suite["tasks"]):
        seed = args.holdout_seed + index * 7919
        rng = np.random.default_rng(seed)
        train_a = rng.normal(size=128)
        eval_a = rng.normal(size=128)
        slope = 1.5 + index * 0.5
        train = np.column_stack((train_a, slope * train_a + rng.normal(0, 0.03, 128)))
        evaluation = np.column_stack((eval_a, slope * eval_a + rng.normal(0, 0.03, 128)))
        labels = np.zeros(128, dtype=bool)
        labels[28:40] = True
        labels[81:96] = True
        evaluation[labels, 1] += 1.0
        columns = tuple(dev["columns"])
        if columns != ("sensor_a", "sensor_b"):
            raise ValueError("synthetic held-out generator requires verified two-sensor suite")
        train_values, eval_values = train.tolist(), evaluation.tolist()
        if digest(train_values) in dev_matrix_hashes or digest(eval_values) in dev_matrix_hashes:
            raise ValueError("held-out features overlap development matrices")
        dataset_id = "heldout-" + prepared["preparation_id"].replace("-", "") + f"-{index}"
        split_id = "independent-synthetic-heldout-v1"
        session_id = "seed-" + str(seed)
        record = {
            "schema": "private-synthetic-heldout-task.v1",
            "dataset_id": dataset_id,
            "split_id": split_id,
            "session_id": session_id,
            "seed": seed,
            "columns": list(columns),
            "train": train_values,
            "evaluation": eval_values,
            "labels": labels.tolist(),
            "visibility": "holdout",
            "source": "generated-independent-rng",
            "normalization": "identity-vus-endpoints-0-and-1-not-measured-calibration",
        }
        profile_sha = digest(record)
        times = tuple(index * 60 for index in range(128))
        masks = (False,) * 128
        semantics_sha = task_semantics_sha256(
            task_family="EVT",
            sampling_s=60,
            evaluation_times=times,
            masked_samples=masks,
            failure_windows=(),
        )
        registrations.append(
            ScorerTaskRegistration(
                dataset_id=dataset_id,
                split_id=split_id,
                session_id=session_id,
                sample_count=128,
                sliding_window=4,
                embargo_seconds=0,
                profile_sha256=profile_sha,
                task_family="EVT",
                sampling_s=60,
                labels=tuple(bool(value) for value in labels),
                evaluation_times=times,
                masked_samples=masks,
                failure_windows=(),
                semantics_sha256=semantics_sha,
                visibility="holdout",
            )
        )
        tasks.append(
            HoldoutTaskSpec(
                task_id=f"private-independent-synthetic-{index}",
                dataset_id=dataset_id,
                split_id=split_id,
                session_id=session_id,
                profile_sha256=profile_sha,
                family="EVT",
                task_weight=0.25,
                base_score=0.0,
                reference_score=1.0,
                sliding_window=4,
                context=FitContext(
                    seed=0, signals=columns, regime_signals=(), sampling_s=60, time_budget_s=30.0
                ),
                train=pd.DataFrame(train, columns=columns),
                evaluation=pd.DataFrame(evaluation, columns=columns),
            )
        )
        private_records.append(record)
    bundle = {
        "schema": "private-synthetic-heldout-bundle.v1",
        "suite_id": prepared["suite_id"],
        "suite_version": suite["suite_version"],
        "tasks": private_records,
        "development_manifest_sha256": prepared["suite_manifest_sha256"],
        "epsilon": 0.0,
        "public_dataset_acceptance": False,
    }
    private_sha = digest(bundle)
    dump_private(args.output_runtime / "holdout-bundle.private.json", bundle)
    result = {
        **prepared,
        "holdout_manifest_sha256": private_sha,
        "holdout_task_count": len(tasks),
        "holdout_installed": False,
        "admission_preconditions_verified": False,
        "holdout_source": "distinct-independent-generated-synthetic",
        "normalization": "identity-vus-endpoints-not-measured-baseline",
        "public_research_acceptance": False,
    }
    if args.install_holdout:
        stage("validate_migrator_identity")
        migrator = engine_for(
            args.migrator_dsn_file,
            role="swapp_lab_migrator",
            database=args.expected_database,
            readonly=False,
        )
        try:
            for registration in registrations:
                stage("install_holdout_profile_labels_semantics")
                install_scorer_task(migrator, registration)
        finally:
            migrator.dispose()
        stage("validate_scorer_registry_identity")
        registry_scorer = engine_for(
            args.scorer_dsn_file,
            role="swapp_lab_scorer",
            database=args.expected_database,
            readonly=False,
        )
        try:
            stage("register_private_holdout_suite")
            installed = register_holdout_suite(
                registry_scorer,
                suite_id=prepared["suite_id"],
                suite_version=suite["suite_version"],
                manifest_sha256=private_sha,
                development_manifest_sha256=prepared["suite_manifest_sha256"],
                epsilon=0.0,
                tasks=tuple(tasks),
            )
        finally:
            registry_scorer.dispose()
        stage("read_only_exact_scorer_holdout_readback")
        scorer = engine_for(
            args.scorer_dsn_file,
            role="swapp_lab_scorer",
            database=args.expected_database,
            readonly=True,
        )
        try:
            with scorer.connect() as connection:
                header = (
                    connection.execute(
                        text(
                            "SELECT manifest_sha256,development_manifest_sha256,task_count,epsilon "
                            "FROM scorer.holdout_suite_versions WHERE suite_id=:suite "
                            "AND suite_version=:version"
                        ),
                        {"suite": prepared["suite_id"], "version": suite["suite_version"]},
                    )
                    .mappings()
                    .one()
                )
                rows = connection.execute(
                    text(
                        "SELECT dataset_id,split_id,session_id,profile_sha256 "
                        "FROM scorer.holdout_suite_tasks "
                        "WHERE suite_id=:suite AND suite_version=:version"
                    ),
                    {"suite": prepared["suite_id"], "version": suite["suite_version"]},
                ).all()
            expected_rows = {
                (task.dataset_id, task.split_id, task.session_id, task.profile_sha256)
                for task in tasks
            }
            if (
                header["manifest_sha256"] != private_sha
                or header["development_manifest_sha256"] != prepared["suite_manifest_sha256"]
                or header["task_count"] != len(tasks)
                or header["epsilon"] != 0.0
                or {tuple(row) for row in rows} != expected_rows
                or installed["tasks"] != len(tasks)
            ):
                raise ValueError("private Scorer installed holdout readback differs")
            result.update(
                holdout_installed=True,
                admission_preconditions_verified=True,
                sql_writes=True,
                holdout_registration=installed,
            )
        finally:
            scorer.dispose()
    stage("write_pre_admission_receipt")
    dump_private(args.output_runtime / "holdout-prepared.private.json", result)
    return result


def atomic_json(path: Path, document: object) -> None:
    payload = canonical(document) + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".research-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def append_registry(registry_path: Path, incoming_path: Path) -> None:
    """Publish validated additions; an exact retry never changes the file generation."""
    from lab.api.registry import MAX_REGISTRY_BYTES, SuiteRegistryFile, load_suite_registry

    lock = registry_path.with_suffix(".lock")
    if lock.is_symlink():
        raise ValueError("invalid registry lock")
    with lock.open("a") as registry_lock:
        fcntl.flock(registry_lock, fcntl.LOCK_EX)
        current = load_suite_registry(registry_path, RUNTIME)
        incoming = load_suite_registry(incoming_path, RUNTIME)
        entries = dict(current.entries)
        for key, entry in incoming.entries.items():
            if key in entries and entries[key] != entry:
                raise ValueError("append-only setup cannot overwrite an existing suite")
            entries[key] = entry
        if entries == current.entries:
            return
        merged = SuiteRegistryFile(schema="lab-suite-registry.v1", suites=tuple(entries.values()))
        document = merged.model_dump(mode="json", by_alias=True)
        if len(canonical(document)) + 1 > MAX_REGISTRY_BYTES:
            raise ValueError("merged registry exceeds the deployment limit")
        atomic_json(registry_path, document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-runtime", type=Path, required=True)
    parser.add_argument("--expected-database", default="swapp_lab")
    parser.add_argument("--planner-dsn-file", type=Path, default=RUNTIME / "postgres/planner.dsn")
    parser.add_argument("--migrator-dsn-file", type=Path, default=RUNTIME / "postgres/migrator.dsn")
    parser.add_argument("--scorer-dsn-file", type=Path, default=RUNTIME / "postgres/scorer.dsn")
    parser.add_argument(
        "--source-registry-file", type=Path, default=RUNTIME / "console-bootstrap-034/registry.json"
    )
    parser.add_argument(
        "--source-principals-file",
        type=Path,
        default=RUNTIME / "console-bootstrap-034/principals.json",
    )
    parser.add_argument("--holdout-seed", type=int, default=2026100101)
    parser.add_argument("--aos-gpu-unit", default="swapp-aos-gpu.service")
    parser.add_argument("--install-holdout", action="store_true", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        from ops.research_configuration import CONFIG, read_environment, validate_unit_name

        validate_unit_name(args.aos_gpu_unit)
        if args.source_registry_file != RUNTIME / "console-bootstrap-034/registry.json":
            raise ValueError("setup must target the existing main registry")
        if args.source_principals_file != RUNTIME / "console-bootstrap-034/principals.json":
            raise ValueError("setup must preserve the existing main principals")
        worker = read_environment(CONFIG / "lab-worker.env")
        if any(not key.endswith("_FILE") and not key.startswith("SWAPP_") for key in worker):
            raise ValueError("worker config must contain path bindings only")
        lock_path = RUNTIME / "start-lab.lock"
        if lock_path.is_symlink():
            raise ValueError("invalid installation lock")
        with lock_path.open("a") as installation_lock:
            fcntl.flock(installation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Append-only installation creates unique task/suite identities. Existing
            # running/unresolved ledger rows are unrelated and never inspected.
            # Activation is a separate, explicit operation after exact owned-service
            # generation/inflight cleanup proof, not a global ledger-state guess.
            prepared = prepare(args)
            registry_path = args.source_registry_file
            append_registry(registry_path, Path(prepared["registry_file"]))
            worker.update(
                SWAPP_LAB_GPU_UNIT="swapp-ai-scientist-director-drain.service",
                SWAPP_AOS_GPU_UNIT=args.aos_gpu_unit,
                SWAPP_GPU_RUNTIME_DB=str(Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"),
                LAB_SCORER_DSN_FILE=str(args.scorer_dsn_file),
            )
            descriptor, temporary = tempfile.mkstemp(prefix=".worker-", dir=CONFIG)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w") as stream:
                    stream.write("".join(f"{key}={value}\n" for key, value in worker.items()))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, CONFIG / "research-worker.pending.env")
            finally:
                Path(temporary).unlink(missing_ok=True)
            prepared.update(
                schema="self-service-research.v1",
                registry_file=str(registry_path),
                principals_file=str(args.source_principals_file),
                expected_database=args.expected_database,
                scorer_dsn_file=str(args.scorer_dsn_file),
                worker_environment=worker,
                principal_sha256=hashlib.sha256(
                    args.source_principals_file.read_bytes()
                ).hexdigest(),
            )
            # Store no credentials, only already-private configuration path bindings.
            atomic_json(CONFIG / "research.pending.json", prepared)
            print(
                json.dumps(
                    {
                        "suite_id": prepared["suite_id"],
                        "installed": True,
                        "activation_pending": True,
                        "owned_service_cleanup_proof_required": True,
                        "aos_acceptance": False,
                        "next": (
                            "Review pending configuration; prove exact owned-service generation "
                            "and inflight cleanup, then explicitly activate "
                            "and restart owned services"
                        ),
                    }
                )
            )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {
                    "installation": "failed",
                    "stage": STAGE,
                    "error_type": type(error).__name__,
                    "message_omitted": True,
                    "prerequisite": (
                        "Existing synthetic.baseline-proof.v1 profile is required; "
                        "this is an existing-profile installer, not cold-start setup"
                    )
                    if PREPARATION_STAGE == "validate_private_inputs"
                    else None,
                    "partial_install_possible": True,
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
