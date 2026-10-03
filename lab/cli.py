"""Small local CLI for health checks and one-shot Director dispatch."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess  # nosec B404 -- only fixed systemd-run and bounded child argv are used
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from lab.operating_modes.public_snapshot import PublicTaskBinding

from sqlalchemy import Engine, case, create_engine, or_, select, text

from harness.fingerprint import compute_harness_hash
from lab.api.app import create_app
from lab.api.registry import SuiteEntry, SuiteRegistry, load_principals, load_suite_registry
from lab.db.schema import runs, score_jobs
from lab.director.baseline_runner import run_baseline_suite
from lab.director.baselines import calibration_sha256
from lab.director.budget import MAX_PROPOSALS, MAX_RUN_MODEL_TOKENS, MAX_RUN_WALL_SECONDS, RunBudget
from lab.director.fake_llm import AgentContext, ProposalProvider, ProposalTurn
from lab.director.journal import DirectorRunLease
from lab.director.local_llm import (
    LocalQwenProposalProvider,
    provider_config_sha256,
    provider_profile_set_for_sha256,
)
from lab.director.loop import DirectorLoop, DirectorLoopResult, load_fake_provider
from lab.director.ownership import (
    ExecutionContract,
    ExecutionOwner,
    OwnerProcessIdentity,
    active_execution_owner,
    assert_execution_owner_closure_transaction,
    assert_execution_owner_transaction,
    bind_execution_owner,
    claim_initial_execution,
    owned_execution,
    reset_execution_owner,
)
from lab.director.recovery import (
    apply_stop_and_finalize,
    capture_current_owner,
    inspect_recovery,
    is_current_dispatch_owner,
)
from lab.director.suite_manifest import SuiteManifest, load_suite_manifest
from lab.director.task_plan import cancel_queued_score_job, seal_run_task_plan
from lab.llm.gpu_scheduler import SystemdPrincipalResolver
from lab.llm.native_runtime import SystemdUnitManager
from lab.replay import replay_run
from lab.reporting import build_run_report
from lab.sandbox.docker_runner import DEFAULT_SANDBOX_IMAGE, LocalDockerRunner
from lab.scorer.credential_path import configured_scorer_dsn_file
from lab.scorer.supervisor import (
    MAX_WORKER_RUNTIME_SECONDS,
    SYSTEMD_RUN,
    run_scorer_empty_baseline_stop_process,
    run_scorer_finalize_process,
    run_scorer_process,
    run_scorer_recovery_process,
)
from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLANNER_DSN = PROJECT_ROOT / "data/runtime/postgres/planner.dsn"
DEFAULT_DIRECTOR_DSN = PROJECT_ROOT / "data/runtime/postgres/director.dsn"
GPU_RUNTIME_ROOT = PROJECT_ROOT / "data/runtime/gpu"
DIRECTOR_DISPATCH_OVERHEAD_SECONDS = 600
DIRECTOR_DISPATCH_ENVIRONMENT = (
    "LAB_DIRECTOR_DSN_FILE",
    "LAB_PLANNER_DSN_FILE",
    "LAB_SCORER_DSN_FILE",
    "LAB_SUITE_REGISTRY_FILE",
    "LAB_CPU_ONLY",
    "SWAPP_AOS_GPU_UNIT",
    "SWAPP_LAB_GPU_UNIT",
    "SWAPP_GPU_RUNTIME_DB",
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
)


def _director_model_observer(engine: Engine, owner: ExecutionOwner) -> Callable[[], None]:
    """Observe the captured owner with a bounded read-only private SQL worker."""
    from lab.director.model_cancellation import DirectorModelObserver

    return DirectorModelObserver(engine, owner)


def _local_qwen_provider(
    run_id: UUID,
    registry_entry_sha256: str,
    *,
    director_engine: Engine,
    profile_set: Literal["smoke", "research"] = "smoke",
    proposal_contract: Literal[
        "candidate-python.v1", "operating-mode-config.v1"
    ] = "candidate-python.v1",
) -> LocalQwenProposalProvider:
    """Build the fixed local provider from service-owned identity and runtime paths."""
    if re.fullmatch(r"[0-9a-f]{64}", registry_entry_sha256) is None:
        raise ValueError("trusted provider registry receipt is required")
    aos_unit = os.environ.get("SWAPP_AOS_GPU_UNIT", "")
    lab_unit = os.environ.get("SWAPP_LAB_GPU_UNIT", "")
    database_value = os.environ.get("SWAPP_GPU_RUNTIME_DB", "")
    if not aos_unit or not lab_unit or not database_value:
        raise RuntimeError("local model service requires its fixed GPU principal mapping")
    database = Path(database_value)
    database = _validated_gpu_runtime_database(database)
    owner = active_execution_owner()
    if owner is None or owner.run_id != run_id:
        raise RuntimeError("local provider requires its captured Director execution owner")
    expected_configuration = (
        provider_config_sha256(profile_set)
        if proposal_contract == "candidate-python.v1"
        else provider_config_sha256(profile_set, proposal_contract)
    )
    provider = LocalQwenProposalProvider(
        run_id=run_id,
        owner="lab",
        principal_resolver=SystemdPrincipalResolver({"aos": aos_unit, "lab": lab_unit}),
        runtime_database=database,
        registry_entry_sha256=registry_entry_sha256,
        profile_set=profile_set,
        proposal_contract=proposal_contract,
        cancellation_observer=_director_model_observer(director_engine, owner),
    )
    if provider.configuration_sha256 != expected_configuration:
        raise RuntimeError("local provider configuration changed during construction")
    return provider


def _validated_gpu_runtime_database(database: Path) -> Path:
    """Accept a private test DB or the one fixed broker DB, never a caller path."""
    if not database.is_absolute() or database.is_symlink():
        raise ValueError("GPU runtime database must be absolute and cannot be a symlink")
    project_root = GPU_RUNTIME_ROOT
    shared_database = Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"

    if database == shared_database:
        home = Path.home()
        directory_paths = _absolute_directory_chain(shared_database.parent)
        for directory in directory_paths:
            try:
                info = directory.lstat()
            except OSError as exc:
                raise ValueError("shared GPU runtime directory is unavailable") from exc
            is_home_or_descendant = directory == home or home in directory.parents
            root_sticky_directory = bool(info.st_mode & 0o1000) and info.st_uid == 0
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not root_sticky_directory)
            ):
                raise ValueError("shared GPU runtime path has an unsafe directory component")
            if root_sticky_directory:
                continue
            if is_home_or_descendant and info.st_uid != os.getuid():
                raise ValueError("shared GPU home path is not owned by the service owner")
            if directory in {home / ".local/state", home / ".local/state/swapp-gpu"} and (
                info.st_mode & 0o077
            ):
                raise ValueError("shared GPU state directory must be private to the service owner")
        parent = shared_database.parent
    else:
        if database.name in {"", ".", ".."} or database.parent != project_root:
            raise ValueError(
                "GPU runtime database must be a direct child of its private runtime root"
            )
        _validate_private_directory_chain(project_root, strict_root=project_root)
        parent = project_root
    try:
        database_info = database.lstat()
    except FileNotFoundError:
        database_info = None
    except OSError as exc:
        raise ValueError("GPU runtime database cannot be inspected") from exc
    if database_info is not None and (
        database.is_symlink()
        or not database.is_file()
        or database_info.st_uid != os.getuid()
        or database_info.st_mode & 0o077
    ):
        raise ValueError("GPU runtime database must be a private regular file")
    return parent / database.name


def _absolute_directory_chain(path: Path) -> tuple[Path, ...]:
    """Return every original lexical directory component without resolving it."""
    if not path.is_absolute():
        raise ValueError("GPU runtime directory must be absolute")
    components = [Path("/")]
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        components.append(current)
    return tuple(components)


def _validate_private_directory_chain(path: Path, *, strict_root: Path) -> None:
    for directory in _absolute_directory_chain(path):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise ValueError("GPU runtime directory is unavailable") from exc
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or info.st_uid not in {0, os.getuid()}
            or (info.st_mode & 0o022 and not (info.st_mode & 0o1000 and info.st_uid == 0))
        ):
            raise ValueError("GPU runtime path has an unsafe directory component")
        if directory == strict_root and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("GPU runtime directory must be private to the service owner")


def _planner_engine() -> Engine:
    path = Path(os.environ.get("LAB_PLANNER_DSN_FILE", DEFAULT_PLANNER_DSN))
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("private Planner DSN file is unavailable or not mode 0600")
    dsn = path.read_text(encoding="utf-8").strip()
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError("Planner DSN must use the psycopg PostgreSQL driver")
    return create_engine(dsn, pool_size=1, max_overflow=0, pool_pre_ping=True)


def _director_engine() -> Engine:
    path = Path(os.environ.get("LAB_DIRECTOR_DSN_FILE", DEFAULT_DIRECTOR_DSN))
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise RuntimeError("private Director DSN file is unavailable or not mode 0600")
    dsn = path.read_text(encoding="utf-8").strip()
    if not dsn.startswith("postgresql+psycopg://"):
        raise RuntimeError("Director DSN must use the psycopg PostgreSQL driver")
    # DirectorRunLease holds one dedicated advisory-lock connection throughout
    # the run. The Director also needs a second short-lived connection for its
    # read/write transactions; pool_size=1 would self-deadlock every CLI run.
    return create_engine(dsn, pool_size=2, max_overflow=0, pool_pre_ping=True)


def _private_runtime_directory(path: Path) -> Path:
    runtime_root = (PROJECT_ROOT / "data/runtime").resolve()
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise ValueError("runtime artifact path cannot be a symlink")
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = candidate.resolve()
    if runtime_root not in resolved.parents:
        raise ValueError("Director artifacts must remain under the private project runtime")
    os.chmod(resolved, 0o700)
    return resolved


def _verified_artifact_root(path: Path) -> Path:
    """Resolve an existing run-scoped artifact root without changing its contents."""
    runtime_root = (PROJECT_ROOT / "data/runtime").resolve()
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("run artifact root must be an existing regular directory")
    resolved = candidate.resolve(strict=True)
    stat = resolved.stat()
    if runtime_root not in resolved.parents or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("run artifact root must be private under project runtime")
    return resolved


class _ClosureOnlyProvider:
    """Inert provider for deadline-expired receipt reconciliation."""

    def propose(self, context: AgentContext) -> ProposalTurn:
        raise RuntimeError("expired resume cannot propose or call a model")


def _resume_terminal_loop(loop: DirectorLoop, calibration: Any) -> DirectorLoopResult:
    """Reconcile existing receipt suffixes, then require a complete historical terminal chain."""
    from lab.director.holdout_replay import recover_observed_terminal_holdouts
    from lab.director.task_plan import read_terminal_holdout_checkpoints

    loop._verify_execution_identity(calibration)
    recover_observed_terminal_holdouts(
        loop.director_engine, loop.lease, artifact_root=loop.artifact_root
    )
    # This read fails before _load_or_initialize could initialize a fresh state.
    loop._latest_state_key()
    state, checkpoint = loop._load_or_initialize(calibration)
    _, terminal_state, marker = read_terminal_holdout_checkpoints(
        loop.director_engine, run_id=loop.run_id, lease=loop.lease, artifact_root=loop.artifact_root
    )
    if terminal_state != state.model_dump(mode="json", by_alias=True) or marker.get(
        "state_sha256"
    ) != checkpoint.get("payload_sha256"):
        raise RuntimeError("expired resume terminal chain conflicts with current durable state")
    if state.holdout_last_status not in {"passed", "reverted", "quota_exhausted", "failed"}:
        raise RuntimeError("expired resume has no terminal holdout application")
    status: Literal["proposal_limit_reached", "budget_exhausted"] = (
        "proposal_limit_reached"
        if state.completed_proposals == state.proposal_limit
        else "budget_exhausted"
    )
    return loop._result(state, checkpoint, status)


def _request_proposal_contract(
    request: Mapping[str, object],
) -> Literal["candidate-python.v1", "operating-mode-config.v1"]:
    contract = request.get("proposal_contract", "candidate-python.v1")
    if not isinstance(contract, str) or contract not in {
        "candidate-python.v1",
        "operating-mode-config.v1",
    }:
        raise ValueError("immutable proposal contract is unsupported")
    return (
        "operating-mode-config.v1"
        if contract == "operating-mode-config.v1"
        else "candidate-python.v1"
    )


def _registered_public_grid_binding(
    request: dict[str, Any], suite_file: Path, *, owner_id: str, origin: str, snapshot_sha256: str
) -> PublicTaskBinding:
    """Recheck immutable public CPU authority before any candidate or baseline dispatch."""
    from lab.api.mode_experiments import ModeSnapshotStore

    configured = os.environ.get("LAB_SUITE_REGISTRY_FILE", "")
    if not configured:
        raise ValueError("public development registry unavailable")
    registry = load_suite_registry(Path(configured), PROJECT_ROOT / "data/runtime")
    entry = registry.get(request["suite"])
    policy = entry.public_dev_study
    public = ModeSnapshotStore(PROJECT_ROOT / "data/runtime/mode-snapshots").public_snapshot(
        snapshot_sha256
    )
    if (
        policy is None
        or public is None
        or policy.owner_id != owner_id
        or policy.origin != origin
        or request.get("public_dev_study") != policy.model_dump(mode="json")
        or request.get("provider") != entry.provider
        or request.get("track") != entry.track
        or request.get("study_kind") != "single_snapshot_study"
        or request.get("provider_config_sha256") != entry.provider_config_sha256
        or request.get("provider_registry_entry_sha256") != registry.entry_sha256(entry)
        or request.get("suite_manifest_sha256") != entry.suite_manifest_sha256
        or request.get("snapshot_sha256") != policy.snapshot_sha256
        or registry.verify_entry(entry)[0] != suite_file.resolve(strict=True)
    ):
        raise ValueError("public development dispatch differs from immutable registry authority")
    policy.verify_snapshot(public)
    budget = request["budget"]
    policy.verify_budget(budget["experiments"], budget["wall_seconds"], budget["model_tokens"])
    return public.binding


def _registered_mode_agent_snapshot(request: dict[str, object], suite_file: Path) -> str:
    """Grant single-snapshot authority only to the hash-bound local mode registry entry."""
    configured = os.environ.get("LAB_SUITE_REGISTRY_FILE", "")
    if not configured or request.get("study_kind") != "single_snapshot_study":
        raise ValueError("mode agent study requires its registered immutable admission")
    registry = load_suite_registry(Path(configured), PROJECT_ROOT / "data/runtime")
    suite_id = request.get("suite")
    if not isinstance(suite_id, str):
        raise ValueError("mode agent suite identity is missing")
    entry = registry.get(suite_id)
    path, _ = registry.verify_entry(entry)
    if (
        entry.provider != "local-qwen"
        or entry.track != "mode"
        or entry.proposal_contract != "operating-mode-config.v1"
        or entry.snapshot_sha256 is None
        or request.get("provider") != entry.provider
        or request.get("track") != entry.track
        or request.get("proposal_contract") != entry.proposal_contract
        or request.get("snapshot_sha256") != entry.snapshot_sha256
        or request.get("provider_config_sha256") != entry.provider_config_sha256
        or request.get("provider_registry_entry_sha256") != registry.entry_sha256(entry)
        or request.get("suite_manifest_sha256") != entry.suite_manifest_sha256
        or path != suite_file.resolve(strict=True)
    ):
        raise ValueError("mode agent snapshot differs from trusted registry admission")
    return entry.snapshot_sha256


def _finalize_mode_study(
    planner: Engine,
    *,
    owner: ExecutionOwner,
    request: dict[str, object],
    manifest: SuiteManifest,
    snapshot_sha256: str | None,
    holdout_enabled: bool,
    loop_status: str,
    budget: RunBudget,
) -> dict[str, object] | None:
    """Seal a finished explicit mode study under its original live execution budget."""
    if (
        holdout_enabled
        or not (
            request.get("provider") == "mode-grid"
            or (
                request.get("provider") == "local-qwen"
                and request.get("proposal_contract") == "operating-mode-config.v1"
                and request.get("snapshot_sha256") == snapshot_sha256
                and request.get("study_kind") == "single_snapshot_study"
            )
        )
        or request.get("track") != "mode"
        or manifest.weight_policy != "single_snapshot_study.v1"
        or snapshot_sha256 is None
        or loop_status not in {"proposal_limit_reached", "budget_exhausted"}
    ):
        return None
    expired: dict[str, object] = {
        "exit_code": None,
        "state": "finalization_window_expired",
        "research_status": "manual_review_required",
        "unit": None,
    }
    if budget.remaining_wall_seconds < 1:
        return expired
    seal_run_task_plan(planner, run_id=owner.run_id)
    remaining = min(60, int(budget.remaining_wall_seconds))
    if remaining < 1:
        return expired
    result = run_scorer_finalize_process(
        owner.run_id,
        admitted_generation=owner.generation,
        execution_sha256=owner.execution_sha256,
        remaining_seconds=remaining,
    )
    return {
        "exit_code": result.exit_code,
        "state": (result.result or {}).get("state", "worker_failed"),
        "research_status": (result.result or {}).get("research_status", "pending"),
        "unit": result.unit,
    }


def _run_director(args: argparse.Namespace) -> dict[str, object]:
    director: Engine | None = None
    planner: Engine | None = None
    try:
        if not 1 <= args.seed_wall_seconds <= 600:
            raise ValueError("seed wall limit must be in 1..600 seconds")
        owner = active_execution_owner()
        if owner is None or owner.run_id != args.run_id:
            raise RuntimeError("raw Director execution requires a captured generation owner")
        from lab.director.resume import ResumeReceipt

        resume_receipt = getattr(args, "resume_receipt", None)
        if resume_receipt is not None and (
            not isinstance(resume_receipt, ResumeReceipt) or resume_receipt.owner != owner
        ):
            raise ValueError("resume receipt differs from captured owner")
        closure_only = resume_receipt is not None and (
            resume_receipt.closure_only or datetime.now(UTC) >= resume_receipt.deadline_at
        )
        director = _director_engine()
        with director.begin() as connection:
            connection.execute(text("SELECT lab.lock_run_plan(:run_id)"), {"run_id": args.run_id})
            if closure_only:
                assert_execution_owner_closure_transaction(connection, owner, run_id=args.run_id)
            else:
                assert_execution_owner_transaction(connection, owner, run_id=args.run_id)
            run = (
                connection.execute(
                    text(
                        "SELECT state, request_json, owner_id, origin FROM lab.runs "
                        "WHERE run_id=:run_id"
                    ),
                    {"run_id": args.run_id},
                )
                .mappings()
                .one_or_none()
            )
        if run is None or run["state"] != "running":
            raise ValueError("Director run must exist in running state")
        runtime = _private_runtime_directory(args.artifact_root)
        planner = _planner_engine()
        request = run["request_json"]
        from lab.director.field_context import load_frozen_field_context
        from lab.director.history_context import load_frozen_prior_findings

        prior_findings = load_frozen_prior_findings(request)
        field_context = load_frozen_field_context(request)
        study_snapshot = None
        public_binding = None
        if request.get("provider") == "mode-grid" and request.get("track") == "mode":
            from lab.api.mode_experiments import ModeSnapshotStore
            from lab.director.parameter_grid import ParameterGridProvider

            if args.provider != "mode-grid" or args.scenario_file is None:
                raise ValueError("mode study requires the registered grid provider")
            study_provider = ParameterGridProvider.load(
                args.scenario_file,
                configuration_sha256=request.get("provider_config_sha256"),
                registry_entry_sha256=request.get("provider_registry_entry_sha256"),
            )
            study_snapshot = study_provider.snapshot_sha256
            if (
                ModeSnapshotStore(PROJECT_ROOT / "data/runtime/mode-snapshots").installed(
                    study_snapshot
                )
                is None
            ):
                raise ValueError("mode study snapshot is not installed")
            store = ModeSnapshotStore(PROJECT_ROOT / "data/runtime/mode-snapshots")
            public = store.public_snapshot(study_snapshot)
            if public is not None:
                public_binding = _registered_public_grid_binding(
                    request,
                    args.suite_file,
                    owner_id=run["owner_id"],
                    origin=run["origin"],
                    snapshot_sha256=study_snapshot,
                )
        elif request.get("proposal_contract") == "operating-mode-config.v1":
            study_snapshot = _registered_mode_agent_snapshot(request, args.suite_file)
            if args.provider != "local-qwen":
                raise ValueError("mode agent study requires the registered local provider")
        if field_context is not None:
            load_frozen_field_context(request, expected_snapshot_sha256=study_snapshot)
            if study_snapshot is None:
                raise ValueError("field context dispatch has no verified mode snapshot")
        manifest, tasks, manifest_sha = load_suite_manifest(
            args.suite_file,
            planner,
            study_snapshot_sha256=study_snapshot,
            public_task_binding=public_binding,
        )
        if not isinstance(request, dict) or not isinstance(request.get("budget"), dict):
            raise ValueError("immutable Director request has no valid budget")
        run_budget = request["budget"]
        requested_proposals = run_budget.get("experiments")
        requested_wall = run_budget.get("wall_seconds")
        requested_tokens = run_budget.get("model_tokens")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (requested_proposals, requested_wall, requested_tokens)
        ):
            raise ValueError("immutable Director budget fields must be strict integers")
        if not 1 <= requested_proposals <= MAX_PROPOSALS:
            raise ValueError("requested proposal budget exceeds the host limit")
        if not 1 <= requested_wall <= MAX_RUN_WALL_SECONDS:
            raise ValueError("requested wall budget exceeds the host limit")
        if not 0 <= requested_tokens <= MAX_RUN_MODEL_TOKENS:
            raise ValueError("requested model-token budget exceeds the host limit")
        proposal_limit = requested_proposals
        if args.proposal_limit is not None and args.proposal_limit != proposal_limit:
            raise ValueError("CLI proposal limit differs from the immutable run request")
        if (
            not isinstance(request, dict)
            or request.get("suite") != args.suite_id
            or request.get("suite") != manifest.suite_id
            or request.get("suite_manifest_sha256") != manifest_sha
            or request.get("proposal_limit") != proposal_limit
        ):
            raise ValueError("suite or proposal budget differs from the immutable run request")
        provider: ProposalProvider
        if closure_only:
            provider = _ClosureOnlyProvider()
        elif args.provider == "fake-json":
            if args.scenario_file is None:
                raise ValueError("fake-json provider requires its private scenario file")
            scenario_path = args.scenario_file.expanduser()
            fake_provider = load_fake_provider(scenario_path)
            scenario_sha = __import__("hashlib").sha256(scenario_path.read_bytes()).hexdigest()
            if request.get("scenario_sha256") != scenario_sha:
                raise ValueError("fake proposal scenario differs from the immutable run request")
            if len(fake_provider) < proposal_limit:
                raise ValueError("fake proposal scenario is shorter than the requested run")
            provider = fake_provider
        elif args.provider == "mode-grid":
            from lab.director.parameter_grid import ParameterGridProvider

            config_sha = request.get("provider_config_sha256")
            entry_sha = request.get("provider_registry_entry_sha256")
            if (
                args.scenario_file is None
                or not isinstance(config_sha, str)
                or not isinstance(entry_sha, str)
                or request.get("provider") != "mode-grid"
                or request.get("scenario_sha256") != config_sha
                or requested_tokens != 0
            ):
                raise ValueError("immutable mode-grid provider identity is incomplete")
            provider = ParameterGridProvider.load(
                args.scenario_file, configuration_sha256=config_sha, registry_entry_sha256=entry_sha
            )
            if len(provider) != proposal_limit:
                raise ValueError("grid size differs from immutable proposal budget")
        elif args.provider == "local-qwen":
            entry_sha = getattr(args, "provider_registry_entry_sha256", None)
            if not isinstance(entry_sha, str):
                entry_sha = request.get("provider_registry_entry_sha256")
            config_sha = request.get("provider_config_sha256")
            if not isinstance(config_sha, str):
                raise ValueError("immutable local provider configuration digest is missing")
            proposal_contract = _request_proposal_contract(request)
            profile_set = provider_profile_set_for_sha256(config_sha, proposal_contract)
            if (
                not isinstance(entry_sha, str)
                or request.get("scenario_sha256") is not None
                or config_sha != provider_config_sha256(profile_set, proposal_contract)
                or request.get("provider_registry_entry_sha256") != entry_sha
            ):
                raise ValueError("local provider configuration differs from immutable request")
            injected_provider = getattr(args, "provider_instance", None)
            provider = injected_provider or _local_qwen_provider(
                args.run_id,
                entry_sha,
                director_engine=director,
                profile_set=profile_set,
                proposal_contract=proposal_contract,
            )
            if (
                getattr(provider, "provider_id", None) != "local-qwen.v1"
                or getattr(provider, "configuration_sha256", None)
                != request.get("provider_config_sha256")
                or getattr(provider, "registry_entry_sha256", None) != entry_sha
                or getattr(provider, "proposal_contract", "candidate-python.v1")
                != proposal_contract
            ):
                raise ValueError("local provider instance does not match the registered identity")
        else:
            raise ValueError("provider is not enabled by the trusted run registry")
        harness_sha = compute_harness_hash(PROJECT_ROOT).sha256
        image_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
        budget = RunBudget(
            proposal_limit=proposal_limit,
            wall_limit=requested_wall,
            token_limit=requested_tokens,
        )
        # The artifact root is a strict content-addressed store: only hash
        # shards and its lock/quota metadata may live there. Keep Docker work
        # directories in a sibling private runtime tree.
        work_root = _private_runtime_directory(runtime.parent / "sandbox" / str(args.run_id))
        runner = LocalDockerRunner(image=DEFAULT_SANDBOX_IMAGE, work_root=work_root)
        with DirectorRunLease(director, args.run_id) as lease:
            if resume_receipt is not None:
                from lab.director.artifacts import read_registered_calibration
                from lab.director.baseline_runner import _restore_latest_baseline_budget

                # Missing calibration is an explicit infrastructure failure, never a new bootstrap.
                calibration = read_registered_calibration(
                    director, run_id=args.run_id, artifact_root=runtime
                )
                _restore_latest_baseline_budget(director, lease, args.run_id, budget, runtime)
                budget.observe_elapsed(resume_receipt.elapsed_wall_seconds)
            else:
                calibration = run_baseline_suite(
                    director,
                    planner,
                    run_id=args.run_id,
                    tasks=tasks,
                    lease=lease,
                    runner=runner,
                    suite_id=manifest.suite_id,
                    suite_version=manifest.suite_version,
                    harness_sha256=harness_sha,
                    image_sha256=image_digest,
                    budget=budget,
                    artifact_root=runtime,
                    seed_wall_seconds=args.seed_wall_seconds,
                )
            from lab.director.holdout import holdout_suite_is_registered

            holdout_enabled = holdout_suite_is_registered(director, run_id=args.run_id)
            loop = DirectorLoop(
                director,
                planner,
                lease=lease,
                runner=runner,
                run_id=args.run_id,
                tasks=tasks,
                suite_manifest_sha256=manifest_sha,
                provider=provider,
                budget=budget,
                artifact_root=runtime,
                proposal_limit=proposal_limit,
                seed_wall_seconds=args.seed_wall_seconds,
                holdout_enabled=holdout_enabled,
                prior_findings=prior_findings,
                field_context=field_context,
            )
            loop_result = _resume_terminal_loop(loop, calibration) if closure_only else loop.run()
            holdout_status = "not_registered_manual_review"
            if holdout_enabled and loop_result.status in {
                "proposal_limit_reached",
                "budget_exhausted",
            }:
                if not closure_only:
                    loop_result = loop.check_run_end_holdout(loop_result)
                state_key = loop._state_key_for_digest(loop_result.checkpoint_sha256)
                state_receipt = lease.read_checkpoint(key=state_key, artifact_root=runtime)
                state_payload = state_receipt.get("payload") if state_receipt else None
                holdout_status = (
                    state_payload.get("holdout_last_status", "manual_review")
                    if isinstance(state_payload, dict)
                    else "manual_review"
                )
            elif holdout_enabled:
                holdout_status = "not_terminal_manual_review"
            finalizer_result: dict[str, object] | None = None
            if (
                holdout_enabled
                and loop_result.status in {"proposal_limit_reached", "budget_exhausted"}
                and holdout_status in {"passed", "reverted", "quota_exhausted", "failed"}
            ):
                from lab.director.task_plan import (
                    read_terminal_holdout_checkpoints,
                    seal_run_task_plan,
                    terminal_finalization_seconds,
                )

                terminal_checkpoints = read_terminal_holdout_checkpoints(
                    director, run_id=args.run_id, lease=lease, artifact_root=runtime
                )
                seal_run_task_plan(
                    planner, run_id=args.run_id, terminal_checkpoints=terminal_checkpoints
                )
                finalizer_seconds = terminal_finalization_seconds(planner, run_id=args.run_id)
                if finalizer_seconds >= 1:
                    finalizer = run_scorer_finalize_process(
                        args.run_id,
                        admitted_generation=owner.generation,
                        execution_sha256=owner.execution_sha256,
                        remaining_seconds=finalizer_seconds,
                        artifact_root=runtime,
                    )
                    finalizer_result = {
                        "exit_code": finalizer.exit_code,
                        "state": (finalizer.result or {}).get("state", "worker_failed"),
                        "research_status": (finalizer.result or {}).get(
                            "research_status", "pending"
                        ),
                        "unit": finalizer.unit,
                    }
                else:
                    finalizer_result = {
                        "exit_code": None,
                        "state": "finalization_window_expired",
                        "research_status": "manual_review_required",
                        "unit": None,
                    }
            elif not holdout_enabled and not closure_only:
                finalizer_result = _finalize_mode_study(
                    planner,
                    owner=owner,
                    request=request,
                    manifest=manifest,
                    snapshot_sha256=study_snapshot,
                    holdout_enabled=holdout_enabled,
                    loop_status=loop_result.status,
                    budget=budget,
                )
        return {
            "run_id": str(args.run_id),
            "suite_manifest_sha256": manifest_sha,
            "calibration_sha256": calibration_sha256(calibration),
            "proposal_limit": proposal_limit,
            "loop": loop_result.model_dump(mode="json"),
            "holdout_status": holdout_status,
            "finalizer": finalizer_result,
        }
    finally:
        if planner is not None:
            planner.dispose()
        if director is not None:
            director.dispose()


def _dispatch_one(engine: Engine, *, remaining_seconds: int) -> dict[str, str]:
    """Select one durable score job and run its separately supervised process."""
    now = datetime.now(UTC)
    ready = or_(
        score_jobs.c.state == "queued",
        (score_jobs.c.state == "running") & (score_jobs.c.lease_until < now),
    )
    with engine.connect() as connection:
        job_id = connection.execute(
            select(score_jobs.c.job_id)
            .join(runs, runs.c.run_id == score_jobs.c.run_id)
            .where(ready, runs.c.state == "running")
            .order_by(
                case((score_jobs.c.state == "queued", 0), else_=1),
                score_jobs.c.created_at,
                score_jobs.c.job_id,
            )
            .limit(1)
        ).scalar_one_or_none()
    if job_id is not None:
        result = run_scorer_process(UUID(str(job_id)), remaining_seconds=remaining_seconds)
        status = (
            result.result.get("state", "worker_failed")
            if result.result is not None
            else "worker_failed"
        )
        if status != "capacity_busy":
            return {
                "job_id": str(job_id),
                "state": status,
                "process_exit_code": str(result.exit_code),
                "systemd_unit": result.unit,
                "research_status": (result.result or {}).get("research_status", "pending"),
            }
    with engine.connect() as connection:
        stopped_job = connection.execute(
            select(
                score_jobs.c.job_id,
                score_jobs.c.run_id,
                score_jobs.c.state,
                score_jobs.c.claim_invocation_id,
                score_jobs.c.admitted_generation,
                score_jobs.c.execution_sha256,
            )
            .join(runs, runs.c.run_id == score_jobs.c.run_id)
            .where(
                runs.c.state == "stop_requested",
                score_jobs.c.state.in_(("queued", "running")),
            )
            .order_by(score_jobs.c.created_at, score_jobs.c.job_id)
            .limit(1)
        ).one_or_none()
    if stopped_job is None:
        with engine.connect() as connection:
            unstarted_baseline_stop = connection.execute(
                text("SELECT lab.next_unstarted_baseline_stop()")
            ).scalar_one_or_none()
        if unstarted_baseline_stop is not None:
            baseline_run_id = UUID(str(unstarted_baseline_stop))
            finalization = run_scorer_empty_baseline_stop_process(
                baseline_run_id, remaining_seconds=remaining_seconds
            )
            return {
                "run_id": str(baseline_run_id),
                "state": (
                    "stopped"
                    if _verified_stopped_baseline_report(
                        baseline_run_id,
                        (finalization.result or {}).get("report_sha256"),
                        finalization.exit_code,
                        finalization.result,
                    )
                    else "pending"
                ),
                "process_exit_code": str(finalization.exit_code),
                "systemd_unit": finalization.unit,
                "research_status": (finalization.result or {}).get("research_status", "pending"),
            }
        return {"state": "capacity_busy"} if job_id is not None else {"state": "idle"}
    if stopped_job.state == "queued":
        cancel_queued_score_job(engine, job_id=stopped_job.job_id)
        finalization = run_scorer_finalize_process(
            stopped_job.run_id,
            admitted_generation=stopped_job.admitted_generation,
            execution_sha256=stopped_job.execution_sha256,
            remaining_seconds=remaining_seconds,
        )
        return {
            "job_id": str(stopped_job.job_id),
            "state": "cancelled_queued",
            "process_exit_code": str(finalization.exit_code),
            "systemd_unit": finalization.unit,
            "research_status": (finalization.result or {}).get("research_status", "pending"),
        }
    if stopped_job.claim_invocation_id is None:
        raise RuntimeError("stop-requested running job has no recorded claim invocation")
    recovery = run_scorer_recovery_process(
        UUID(str(stopped_job.job_id)),
        expected_claim_invocation_id=stopped_job.claim_invocation_id,
        remaining_seconds=remaining_seconds,
    )
    status = (
        recovery.result.get("state", "worker_failed")
        if recovery.result is not None
        else "worker_failed"
    )
    return {
        "job_id": str(stopped_job.job_id),
        "state": status,
        "process_exit_code": str(recovery.exit_code),
        "systemd_unit": recovery.unit,
        "research_status": (recovery.result or {}).get("report_state", "pending"),
    }


def _verified_stopped_baseline_report(
    run_id: UUID,
    expected_sha256: str | None,
    exit_code: int,
    result: dict[str, str] | None,
) -> bool:
    """Report success only after reading the canonical terminal Scorer receipt."""
    if (
        exit_code != 0
        or result is None
        or result.get("state") != "finalized"
        or result.get("research_status") != "stopped"
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None
    ):
        return False
    director = _director_engine()
    try:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT r.state,r.report_sha256,p.report_sha256 AS stored_sha256,"
                        "p.report_json FROM lab.runs r JOIN lab.reports p USING(run_id) "
                        "WHERE r.run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
    finally:
        director.dispose()
    if row is None or not isinstance(row["report_json"], dict):
        return False
    report = row["report_json"]
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    if (
        row["state"] != "stopped"
        or row["report_sha256"] != digest
        or row["stored_sha256"] != digest
        or expected_sha256 != digest
        or report.get("schema") != "lab.baseline-report.v1"
        or report.get("run_id") != str(run_id)
        or report.get("status") != "stopped"
        or report.get("calibration_complete") is not False
    ):
        return False
    from lab.scorer.baseline_report import validate_baseline_report

    try:
        validate_baseline_report(report)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _ValidatedDispatch:
    request: dict[str, Any]
    entry: SuiteEntry
    suite_path: Path
    scenario_path: Path | None
    budget: dict[str, Any]
    proposal_limit: int
    purpose: str
    profile_set: Literal["smoke", "research"] | None
    entry_sha256: str
    contract: ExecutionContract


def _validate_dispatch_target(
    run_id: UUID, row: Mapping[str, Any], registry: SuiteRegistry
) -> _ValidatedDispatch:
    """One immutable registry/request/pin check for initial dispatch and takeover."""
    request = row["request_json"]
    if not isinstance(request, dict) or not isinstance(request.get("budget"), dict):
        raise ValueError("queued run has an invalid immutable request")
    entry = registry.get(str(request.get("suite", "")))
    suite_path, scenario_path = registry.verify_entry(entry)
    budget_json = request["budget"]
    proposal_limit = budget_json.get("experiments")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in (
            proposal_limit,
            budget_json.get("wall_seconds"),
            budget_json.get("model_tokens"),
        )
    ):
        raise ValueError("queued run budget is not strictly typed")
    if (
        request.get("track") != entry.track
        or request.get("program_version") != entry.program_version
        or request.get("provider") != entry.provider
        or request.get("proposal_limit") != proposal_limit
        or request.get("suite_manifest_sha256") != entry.suite_manifest_sha256
        or request.get("scenario_sha256") != entry.scenario_sha256
        or request.get("provider_config_sha256") != entry.provider_config_sha256
        or request.get("provider_registry_entry_sha256") != registry.entry_sha256(entry)
        or _request_proposal_contract(request) != entry.proposal_contract
        or request.get("snapshot_sha256") != entry.snapshot_sha256
        or proposal_limit > entry.proposal_limit
    ):
        raise ValueError("queued request no longer matches the trusted suite registry")
    entry_sha = registry.entry_sha256(entry)
    purpose = request.get("purpose", "research")
    if purpose not in {"research", "baseline", "mode-stream"}:
        raise ValueError("queued run has an unsupported purpose")
    if purpose == "mode-stream":
        if entry.provider != "mode-stream" or entry.allowed_purposes != ("mode-stream",):
            raise ValueError("stream request requires its dedicated registered purpose")
        if proposal_limit != 0 or budget_json.get("model_tokens") != 0:
            raise ValueError("diagnostic streams cannot reserve research proposals or tokens")
    if purpose == "baseline" and (proposal_limit != 0 or budget_json.get("model_tokens") != 0):
        raise ValueError("baseline request must reserve zero proposals and model tokens")
    if purpose == "research" and not 1 <= proposal_limit <= entry.proposal_limit:
        raise ValueError("research request proposal budget is outside the registered limit")
    profile_set: Literal["smoke", "research"] | None = None
    if purpose == "research" and entry.provider in {"fake-json", "mode-grid"}:
        if scenario_path is None:
            raise ValueError("trusted fake provider scenario is unavailable")
    elif purpose == "research":
        if entry.provider_config_sha256 is None:
            raise ValueError("registered local provider configuration digest is missing")
        profile_set = provider_profile_set_for_sha256(
            entry.provider_config_sha256, entry.proposal_contract
        )
        current_config_sha = provider_config_sha256(profile_set, entry.proposal_contract)
        if current_config_sha != entry.provider_config_sha256:
            raise ValueError("registered local provider profile differs from this worker")
    # Suite version belongs to the hash-pinned manifest, not the registry entry.
    with suite_path.open("rb") as manifest_stream:
        manifest_payload = manifest_stream.read(MAX_SUITE_MANIFEST_BYTES + 1)
    if len(manifest_payload) > MAX_SUITE_MANIFEST_BYTES or (
        hashlib.sha256(manifest_payload).hexdigest() != entry.suite_manifest_sha256
    ):
        raise ValueError("suite manifest changed before Director execution claim")
    suite_version: int
    if purpose == "mode-stream":
        from lab.director.mode_stream import ModeStreamPlan, plan_from_request
        from lab.operating_modes.stream import load_input

        stream_plan = ModeStreamPlan.model_validate_json(manifest_payload, strict=True)
        if stream_plan != plan_from_request(request):
            raise ValueError("stream plan differs from the admitted request")
        stream_plan.verify_input(
            load_input(registry.runtime_root / "mode-stream-inputs", stream_plan.input_sha256)
        )
        suite_identity, suite_version = stream_plan.suite_id, stream_plan.suite_version
    else:
        suite_document = SuiteManifest.model_validate_json(manifest_payload, strict=True)
        suite_identity, suite_version = suite_document.suite_id, suite_document.suite_version
    if suite_identity != entry.suite_id:
        raise ValueError("verified suite manifest identity differs from the registry")
    from lab.director.field_context import load_frozen_field_context
    from lab.director.history_context import load_frozen_prior_findings

    load_frozen_prior_findings(request)
    load_frozen_field_context(request)
    payload = json.dumps(
        {key: value for key, value in request.items() if key != "idempotency_key"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != row["payload_sha256"]:
        raise ValueError("queued request payload digest failed verification")
    harness_sha = compute_harness_hash(PROJECT_ROOT).sha256
    image_digest = DEFAULT_SANDBOX_IMAGE.rsplit("@sha256:", 1)[-1].removeprefix("sha256:")
    if purpose == "mode-stream" and (
        request.get("harness_sha256") != harness_sha or request.get("image_sha256") != image_digest
    ):
        raise ValueError("stream worker differs from the originally admitted harness/image")
    contract = ExecutionContract.build(
        run_id=run_id,
        request=request,
        request_sha256=str(row["payload_sha256"]),
        suite_id=entry.suite_id,
        suite_version=suite_version,
        suite_manifest_sha256=entry.suite_manifest_sha256,
        registry_entry_sha256=entry_sha,
        harness_sha256=harness_sha,
        image_sha256=image_digest,
    )
    return _ValidatedDispatch(
        request,
        entry,
        suite_path,
        scenario_path,
        budget_json,
        proposal_limit,
        purpose,
        profile_set,
        entry_sha,
        contract,
    )


def _dispatch_director_run(run_id: UUID) -> dict[str, object]:
    """Claim one queued API run and execute its hash-pinned registered inputs."""
    registry_path = Path(os.environ.get("LAB_SUITE_REGISTRY_FILE", ""))
    if not str(registry_path):
        raise RuntimeError("LAB_SUITE_REGISTRY_FILE is required for Director dispatch")
    registry = load_suite_registry(registry_path, PROJECT_ROOT / "data/runtime")
    director = _director_engine()
    try:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT state, request_json, payload_sha256, origin, owner_id "
                        "FROM lab.runs WHERE run_id=:run_id"
                    ),
                    {"run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError("queued Director run does not exist")
        if row["state"] == "stop_requested":
            return {"run_id": str(run_id), "state": "stop_requested", "dispatch": "not_started"}
        if row["state"] != "queued":
            raise ValueError("Director dispatcher accepts only a queued run")
        target = _validate_dispatch_target(run_id, dict(row), registry)
        entry = target.entry
        suite_path, scenario_path = target.suite_path, target.scenario_path
        budget_json, proposal_limit = target.budget, target.proposal_limit
        purpose, profile_set = target.purpose, target.profile_set
        entry_sha, contract = target.entry_sha256, target.contract
        observed = capture_current_owner(str(row["payload_sha256"]), run_id)
        process = OwnerProcessIdentity(
            payload_sha256=observed.payload_sha256,
            worker_pid=observed.worker_pid,
            worker_start_ticks=observed.worker_start_ticks,
            worker_boot_id=observed.worker_boot_id,
            worker_unit=observed.worker_unit,
            worker_invocation_id=observed.worker_invocation_id,
            worker_cgroup=observed.worker_cgroup,
        )
        owner = claim_initial_execution(director, contract=contract, process=process)
        owner_token = bind_execution_owner(owner)
        try:
            provider_instance: ProposalProvider | None = None
            if purpose == "research" and entry.provider == "fake-json":
                if scenario_path is None:
                    raise ValueError("trusted fake provider scenario is unavailable")
                provider_instance = load_fake_provider(scenario_path)
                if len(provider_instance) < proposal_limit:
                    raise ValueError("registered fake scenario is shorter than the run budget")
            elif purpose == "research" and entry.provider == "mode-grid":
                from lab.director.parameter_grid import ParameterGridProvider

                if scenario_path is None or entry.provider_config_sha256 is None:
                    raise ValueError("registered mode-grid configuration is unavailable")
                provider_instance = ParameterGridProvider.load(
                    scenario_path,
                    configuration_sha256=entry.provider_config_sha256,
                    registry_entry_sha256=entry_sha,
                )
            elif purpose == "research":
                if profile_set is None:
                    raise RuntimeError("trusted local provider profile was not verified")
                provider_instance = _local_qwen_provider(
                    run_id,
                    entry_sha,
                    director_engine=director,
                    profile_set=profile_set,
                    proposal_contract=entry.proposal_contract,
                )
            runtime = _private_runtime_directory(
                PROJECT_ROOT / "data/runtime/director-artifacts" / str(run_id)
            )
            if purpose == "mode-stream":
                from lab.director.mode_stream import plan_from_request, run_mode_stream

                planner = _planner_engine()
                try:
                    with DirectorRunLease(director, run_id) as lease:
                        result = run_mode_stream(
                            director,
                            planner,
                            run_id=run_id,
                            plan=plan_from_request(target.request),
                            input_root=registry.runtime_root / "mode-stream-inputs",
                            artifact_root=runtime,
                            lease=lease,
                        )
                finally:
                    planner.dispose()
            elif purpose == "baseline":
                planner = _planner_engine()
                try:
                    with DirectorRunLease(director, run_id) as lease:
                        from lab.director.baseline_operation import run_baseline_operation

                        result = run_baseline_operation(
                            director,
                            planner,
                            run_id=run_id,
                            suite_file=suite_path,
                            artifact_root=runtime,
                            expected_suite_manifest_sha256=entry.suite_manifest_sha256,
                            wall_seconds=budget_json["wall_seconds"],
                            lease=lease,
                        )
                finally:
                    planner.dispose()
            else:
                args = argparse.Namespace(
                    run_id=run_id,
                    suite_id=entry.suite_id,
                    suite_file=suite_path,
                    provider=entry.provider,
                    scenario_file=scenario_path,
                    provider_instance=provider_instance,
                    provider_registry_entry_sha256=entry_sha,
                    proposal_limit=proposal_limit,
                    seed_wall_seconds=min(MAX_WORKER_RUNTIME_SECONDS, budget_json["wall_seconds"]),
                    artifact_root=runtime,
                )
                result = _run_director(args)
            return {
                **_completed_dispatch_result(director, run_id, owner, result),
                "admitted_owner": _admitted_owner_result(owner),
            }
        except Exception as error:
            return {
                **_record_claimed_dispatch_failure(director, run_id, error, owner=owner),
                "admitted_owner": _admitted_owner_result(owner),
            }
        finally:
            reset_execution_owner(owner_token)
    finally:
        director.dispose()


def _admitted_owner_result(owner: ExecutionOwner) -> dict[str, object]:
    return {
        "generation": owner.generation,
        "invocation_id": owner.invocation_id,
        "execution_sha256": owner.execution_sha256,
    }


def _automatic_stopped_baseline_recovery(
    run_id: UUID, expected_owner: dict[str, object] | None
) -> dict[str, object]:
    """Attempt existing stop recovery once, with the original generation/deadline.

    This runs outside the exited run's global callback. Normal recovery retains
    its physical owner/child proofs, stopped-ledger checks and cleanup deadline.
    """
    from lab.director.recovery import _recovery_id, _request_sha256

    pending: dict[str, object] = {
        "run_id": str(run_id),
        "state": "stop_requested",
        "dispatch": "recovery_pending",
    }
    director, planner = _director_engine(), _planner_engine()
    try:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT r.state,r.request_json,r.payload_sha256,g.generation,"
                        "g.worker_invocation_id,g.execution_sha256,g.worker_pid,g.worker_start_ticks,"
                        "g.worker_boot_id,g.worker_unit,g.worker_cgroup,"
                        "e.deadline_at,sc.created_at+interval '120 seconds' "
                        "AS stop_cleanup_deadline, "
                        "(SELECT min(created_at)+interval '120 seconds' FROM lab.run_events "
                        "WHERE run_id=r.run_id AND event_type='run.stop_requested') "
                        "AS requested_stop_deadline, "
                        "EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=r.run_id "
                        "AND kind='proposal') AS proposal_stop "
                        "FROM lab.runs r JOIN lab.director_execution_control c "
                        "ON c.run_id=r.run_id JOIN lab.director_owner_generations g "
                        "ON g.run_id=c.run_id AND g.generation=c.current_generation "
                        "JOIN lab.director_execution_contracts e ON e.run_id=r.run_id "
                        "LEFT JOIN lab.director_stop_closures sc ON sc.run_id=r.run_id "
                        "WHERE r.run_id=:run AND c.mode='active'"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["state"] != "stop_requested" or expected_owner is None:
            return {**pending, "recovery_reason": "stop_generation_unavailable"}
        owner = ExecutionOwner(
            run_id, row["generation"], row["worker_invocation_id"], row["execution_sha256"]
        )
        if _admitted_owner_result(owner) != expected_owner:
            return {**pending, "recovery_reason": "stop_generation_changed"}
        deadline = row["deadline_at"]
        if not isinstance(deadline, datetime) or deadline.tzinfo is None:
            return {**pending, "recovery_reason": "original_deadline_unavailable"}
        remaining = min(120, int((deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()))
        requested_stop_deadline = row["requested_stop_deadline"]
        if (
            not isinstance(requested_stop_deadline, datetime)
            or requested_stop_deadline.tzinfo is None
        ):
            return {**pending, "recovery_reason": "first_stop_deadline_unavailable"}
        cleanup_deadline = min(deadline, requested_stop_deadline)
        remaining = min(
            remaining,
            int((requested_stop_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
        )
        stop_deadline = row["stop_cleanup_deadline"]
        if stop_deadline is not None:
            if not isinstance(stop_deadline, datetime) or stop_deadline.tzinfo is None:
                return {**pending, "recovery_reason": "stop_cleanup_deadline_unavailable"}
            cleanup_deadline = min(cleanup_deadline, stop_deadline)
            remaining = min(
                remaining, int((stop_deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            )
        if remaining < 1:
            return {**pending, "recovery_reason": "original_deadline_expired"}
        stream_stop = row["request_json"].get("purpose") == "mode-stream"
        if stream_stop:
            from lab.director.recovery import _owner_is_proven_dead, _stored_owner
            from lab.director.resume import _drain_director_sandbox

            prior = _stored_owner(row)
            if prior is None or not _owner_is_proven_dead(prior, run_id):
                return {**pending, "recovery_reason": "stream_owner_not_proven_dead"}
            _drain_director_sandbox(
                run_id,
                dict(row),
                PROJECT_ROOT / "data/runtime/director-artifacts" / str(run_id),
                time.monotonic() + max(0.0, (cleanup_deadline - datetime.now(UTC)).total_seconds()),
            )
        result = _with_director_global_slot(
            run_id,
            lambda: apply_stop_and_finalize(
                director,
                planner,
                run_id=run_id,
                recovery_id=_recovery_id(run_id, _request_sha256(run_id, "stop_and_finalize")),
                remaining_seconds=remaining,
                reconcile_interrupted_baseline=not stream_stop
                and not bool(row.get("proposal_stop", False)),
                reconcile_interrupted_proposal=bool(row.get("proposal_stop", False)),
                expected_owner=owner,
                cleanup_deadline=cleanup_deadline,
                artifact_root=_private_runtime_directory(
                    PROJECT_ROOT / "data/runtime/director-artifacts" / str(run_id)
                ),
            ),
        )
        if (
            result.get("recovery_state") == "completed"
            and result.get("action") == "finalized_stopped"
        ):
            return {
                "run_id": str(run_id),
                "state": "stopped",
                "dispatch": "terminal",
                "report_sha256": result["report_sha256"],
                "automatic_stop_recovery": "completed",
            }
        return {**pending, "recovery_reason": result.get("reason", result.get("state", "pending"))}
    except Exception as exc:
        failure: dict[str, object] = {
            **pending,
            "recovery_reason": "stop_recovery_unavailable",
            "recovery_error_type": type(exc).__name__,
        }
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            failure["recovery_sqlstate"] = sqlstate
        return failure
    finally:
        planner.dispose()
        director.dispose()


def _recover_stopped_baselines_at_drain_start(engine: Engine) -> None:
    """One bounded snapshot after a drain generation replaces its exited owner."""
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT r.run_id,g.generation,g.worker_invocation_id,g.execution_sha256 "
                    "FROM lab.runs r JOIN lab.director_execution_control c ON c.run_id=r.run_id "
                    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                    "AND g.generation=c.current_generation "
                    "JOIN lab.director_execution_contracts e ON e.run_id=r.run_id "
                    "LEFT JOIN lab.director_stop_closures sc ON sc.run_id=r.run_id "
                    "WHERE r.state='stop_requested' AND e.deadline_at>clock_timestamp() "
                    "AND (sc.created_at IS NULL OR sc.created_at+interval '120 seconds'"
                    ">clock_timestamp()) "
                    "AND (SELECT min(created_at)+interval '120 seconds' FROM lab.run_events "
                    "WHERE run_id=r.run_id AND event_type='run.stop_requested')>clock_timestamp() "
                    "AND c.mode='active' AND (r.request_json->>'purpose'='mode-stream' "
                    "OR EXISTS (SELECT 1 FROM lab.experiments x "
                    "WHERE x.run_id=r.run_id AND x.kind='baseline' "
                    "AND x.status IN ('proposed','primary_running'))) "
                    "ORDER BY r.created_at,r.run_id LIMIT 16"
                )
            )
            .mappings()
            .all()
        )
    for row in rows:
        owner = {
            "generation": row["generation"],
            "invocation_id": row["worker_invocation_id"],
            "execution_sha256": row["execution_sha256"],
        }
        result = _automatic_stopped_baseline_recovery(UUID(str(row["run_id"])), owner)
        print(json.dumps(result, sort_keys=True), flush=True)


def _completed_dispatch_result(
    director: Engine, run_id: UUID, owner: ExecutionOwner, result: dict[str, object]
) -> dict[str, object]:
    """Reuse the normal owned failure/terminal fences after initial or resumed work."""
    with director.connect() as connection:
        terminal = (
            connection.execute(
                text("SELECT state, report_sha256 FROM lab.runs WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
    loop_summary = result.get("loop")
    loop_state = (
        loop_summary.get("status", "execution_returned")
        if isinstance(loop_summary, dict)
        else "execution_returned"
    )
    if terminal["state"] == "running":
        failure_reason = (
            f"loop:{loop_state}"
            if loop_state != "proposal_limit_reached"
            else "finalizer:did_not_terminalize"
        )
        return {
            **result,
            **_record_claimed_dispatch_failure(
                director,
                run_id,
                RuntimeError("Director worker returned without terminalizing the run"),
                owner=owner,
                failure_reason=failure_reason,
            ),
            "loop_status": loop_state,
        }
    return {
        **result,
        "state": terminal["state"],
        "report_sha256": terminal["report_sha256"],
        "dispatch": ("stop_requested" if terminal["state"] == "stop_requested" else "terminal"),
    }


def _record_claimed_dispatch_failure(
    director: Engine,
    run_id: UUID,
    error: Exception,
    *,
    owner: ExecutionOwner,
    failure_reason: str = "execution_exception",
) -> dict[str, object]:
    """Fail clean work or preserve an already requested stop of this exact owner."""

    def owned_stop_requested() -> bool:
        # The API stop RPC already changed running to stop_requested. The normal
        # failure-closure RPC intentionally only accepts running, so it cannot
        # re-close that row. This observation performs no mutation and admits
        # only the same active execution tuple as the failure-closure fence.
        with director.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT 1 FROM lab.runs r "
                    "JOIN lab.director_execution_control c ON c.run_id=r.run_id "
                    "JOIN lab.director_owner_generations g ON g.run_id=c.run_id "
                    "AND g.generation=c.current_generation "
                    "JOIN lab.director_execution_contracts e ON e.run_id=r.run_id "
                    "WHERE r.run_id=:run_id AND r.state='stop_requested' "
                    "AND r.stop_requested AND c.mode='active' "
                    "AND c.current_generation=:generation "
                    "AND g.worker_invocation_id=:invocation_id "
                    "AND g.execution_sha256=:execution_sha256 "
                    "AND e.execution_sha256=:execution_sha256"
                ),
                {
                    "run_id": run_id,
                    "generation": owner.generation,
                    "invocation_id": owner.invocation_id,
                    "execution_sha256": owner.execution_sha256,
                },
            ).scalar_one_or_none()
        return value == 1

    def stop_result() -> dict[str, object]:
        return {
            "run_id": str(run_id),
            "state": "stop_requested",
            "dispatch": "recovery_required",
            "error_type": type(error).__name__,
        }

    def close_as(target_state: str) -> dict[str, object]:
        with director.begin() as connection:
            payload = connection.execute(
                text(
                    "SELECT lab.close_director_run_if_owned(:run_id,:generation,:invocation_id,"
                    ":execution_sha256,:target_state,:error_type,:failure_reason)"
                ),
                {
                    "run_id": run_id,
                    "generation": owner.generation,
                    "invocation_id": owner.invocation_id,
                    "execution_sha256": owner.execution_sha256,
                    "target_state": target_state,
                    "error_type": type(error).__name__,
                    "failure_reason": failure_reason,
                },
            ).scalar_one()
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or payload.get("state") != target_state:
            raise RuntimeError("Director failure closure returned an invalid receipt")
        return payload

    if owned_stop_requested():
        return stop_result()
    try:
        close_as("failed")
        return {
            "run_id": str(run_id),
            "state": "failed",
            "dispatch": "failed",
            "error_type": type(error).__name__,
        }
    except Exception:
        # The API may have won the stop race after the first observation.
        if owned_stop_requested():
            return stop_result()
        try:
            # Existing ledger terminal fences can require a new safe stop.
            close_as("stop_requested")
        except Exception:
            # Also cover a stop that wins after the second observation.
            if not owned_stop_requested():
                raise
        return stop_result()


def _with_director_global_slot(
    run_id: UUID, operation: Callable[[], dict[str, object]]
) -> dict[str, object]:
    """Use a private process-lifetime lock so Director dispatch is globally P=1."""
    lock_path = PROJECT_ROOT / "data/runtime/director-dispatch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock_path.is_symlink():
        raise RuntimeError("Director admission lock cannot be a symlink")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"run_id": str(run_id), "state": "capacity_busy", "dispatch": "deferred"}
        return operation()
    finally:
        os.close(descriptor)


def _dispatch_director_run_with_global_slot(run_id: UUID) -> dict[str, object]:
    return _with_director_global_slot(run_id, lambda: _dispatch_director_run(run_id))


def _dispatch_director_run_owned(run_id: UUID) -> dict[str, object]:
    """Run direct CLI dispatch inside a bounded run-bound systemd generation.

    The long-lived trusted queue drain is already an authorized owner and may
    execute inline. A direct ``dispatch-one`` invocation is re-executed in a
    fixed, run-bound child unit before it can claim the queued row.
    """
    if is_current_dispatch_owner(run_id):
        return _dispatch_director_run_with_global_slot(run_id)
    engine = _director_engine()
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT state,request_json FROM lab.runs WHERE run_id=:id"),
                    {"id": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError("queued Director run does not exist")
        if row["state"] != "queued":
            raise ValueError("direct dispatch accepts only a queued run")
        request = row["request_json"]
        budget = request.get("budget") if isinstance(request, dict) else None
        requested_wall = budget.get("wall_seconds") if isinstance(budget, dict) else None
        if (
            isinstance(requested_wall, bool)
            or not isinstance(requested_wall, int)
            or not 1 <= requested_wall <= MAX_RUN_WALL_SECONDS
        ):
            raise ValueError("immutable run request has no valid wall budget")
    finally:
        engine.dispose()

    unit = f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
    if (
        request.get("purpose", "research") == "research"
        and request.get("provider") == "local-qwen"
        and os.environ.get("SWAPP_LAB_GPU_UNIT") != unit
    ):
        raise ValueError(
            "direct local-qwen dispatch requires SWAPP_LAB_GPU_UNIT to match its "
            "run-bound unit; otherwise use the trusted Director drain service"
        )
    unit_runtime = requested_wall + DIRECTOR_DISPATCH_OVERHEAD_SECONDS
    return _launch_director_unit(run_id, unit_runtime=unit_runtime)


def _launch_director_unit(
    run_id: UUID, *, unit_runtime: int, restart_id: UUID | None = None
) -> dict[str, object]:
    """Launch only the two fixed Director commands under their canonical bounded units."""
    if not isinstance(run_id, UUID) or (
        restart_id is not None and not isinstance(restart_id, UUID)
    ):
        raise TypeError("Director unit identity must use UUID values")
    if (
        type(unit_runtime) is not int
        or not 1 <= unit_runtime <= MAX_RUN_WALL_SECONDS + DIRECTOR_DISPATCH_OVERHEAD_SECONDS
    ):
        raise ValueError("Director unit runtime exceeds its immutable bounded ceiling")
    unit = (
        f"swapp-ai-scientist-director-dispatch-{run_id.hex}.service"
        if restart_id is None
        else f"swapp-ai-scientist-director-resume-{run_id.hex}-{restart_id.hex}.service"
    )
    command = [
        SYSTEMD_RUN,
        "--user",
        "--wait",
        "--collect",
        "--quiet",
        "--pipe",
        f"--unit={unit}",
        "--slice=swapp-gpu.slice",
        "--property=MemoryMax=2G",
        "--property=MemorySwapMax=0",
        "--property=CPUQuota=100%",
        "--property=TasksMax=128",
        "--property=KillMode=control-group",
        f"--property=RuntimeMaxSec={unit_runtime}",
        f"--working-directory={PROJECT_ROOT}",
    ]
    for name in DIRECTOR_DISPATCH_ENVIRONMENT:
        value = os.environ.get(name)
        if value is None:
            continue
        if name == "LAB_SCORER_DSN_FILE":
            value = str(configured_scorer_dsn_file())
        if "\x00" in value or "\n" in value or len(value) > 4096:
            raise ValueError(f"dispatcher environment value is malformed: {name}")
        command.append(f"--setenv={name}={value}")
    command.extend(
        (
            sys.executable,
            "-m",
            "lab.cli",
            "director",
            "resume" if restart_id is not None else "dispatch-one",
            "--run-id",
            str(run_id),
        )
    )
    if restart_id is not None:
        command.extend(("--restart-id", str(restart_id)))
    # Validate all caller inputs before any service operation. Provision the
    # aggregate limits before systemd-run can implicitly create this slice with
    # unlimited defaults; an existing incompatible slice must fail unchanged.
    SystemdUnitManager().ensure_slice()
    try:
        completed = subprocess.run(  # nosec B603 -- fixed binary/module/argv and bounded unit
            command,
            capture_output=True,
            text=True,
            timeout=unit_runtime + 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bounded Director dispatch wait expired") from exc
    output = completed.stdout.strip().splitlines()
    if not output:
        raise RuntimeError(f"owned Director unit exited {completed.returncode} without a result")
    try:
        result = json.loads(output[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("owned Director unit returned a malformed result") from exc
    if not isinstance(result, dict) or result.get("run_id") != str(run_id):
        raise RuntimeError("owned Director unit result identity differs from the request")
    if restart_id is not None and result.get("restart_id") != str(restart_id):
        raise RuntimeError("owned resume unit result differs from its restart attempt")
    result["owner_unit"] = unit
    result["owner_exit_code"] = completed.returncode
    if result.get("state") == "stop_requested":
        result.update(_automatic_stopped_baseline_recovery(run_id, result.get("admitted_owner")))
    return result


def _is_current_resume_unit(run_id: UUID, restart_id: UUID) -> bool:
    """Launch hint from actual cgroup only; capture_current_owner verifies manager identity."""
    from lab.director.recovery import RecoveryPending, _current_cgroup

    expected = f"swapp-ai-scientist-director-resume-{run_id.hex}-{restart_id.hex}.service"
    try:
        return _current_cgroup().rsplit("/", maxsplit=1)[-1] == expected
    except RecoveryPending:
        return False


def _resume_director_run_owned(run_id: UUID, restart_id: UUID) -> dict[str, object]:
    """Launch an exact restart attempt; wall allowance derives from the original SQL deadline."""
    if not isinstance(run_id, UUID) or not isinstance(restart_id, UUID):
        raise TypeError("resume requires fixed run and restart UUIDs")
    if _is_current_resume_unit(run_id, restart_id):
        result = _with_director_global_slot(
            run_id, lambda: _resume_director_run(run_id, restart_id)
        )
        return {**result, "restart_id": str(restart_id)}
    director = _director_engine()
    try:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT r.state,r.stop_requested,r.request_json,"
                        "greatest(0,extract(epoch FROM x.deadline_at-clock_timestamp())) "
                        "AS remaining_seconds "
                        "FROM lab.runs r JOIN lab.director_execution_contracts x USING(run_id) "
                        "WHERE r.run_id=:run"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["state"] != "running" or row["stop_requested"]:
            raise ValueError("resume requires an existing unstopped running execution")
        request = row["request_json"]
        if not isinstance(request, dict) or request.get("purpose", "research") not in {
            "research",
            "mode-stream",
        }:
            raise ValueError("supported resume requires a research or diagnostic stream run")
        remaining = float(row["remaining_seconds"])
        if not math.isfinite(remaining) or not 0 <= remaining <= MAX_RUN_WALL_SECONDS:
            raise ValueError("original execution deadline is malformed")
        unit = f"swapp-ai-scientist-director-resume-{run_id.hex}-{restart_id.hex}.service"
        if (
            request.get("provider") == "local-qwen"
            and remaining > 0
            and os.environ.get("SWAPP_LAB_GPU_UNIT") != unit
        ):
            raise ValueError(
                "local-qwen resume requires the exact resume unit GPU principal mapping"
            )
        return _launch_director_unit(
            run_id,
            restart_id=restart_id,
            unit_runtime=math.ceil(remaining) + DIRECTOR_DISPATCH_OVERHEAD_SECONDS,
        )
    finally:
        director.dispose()


def _resume_director_run(run_id: UUID, restart_id: UUID) -> dict[str, object]:
    """Validate the same original inputs, drain/CAS, then execute under captured ownership."""
    from lab.director.recovery import RecoveryPending
    from lab.director.resume import resume_execution

    registry_value = os.environ.get("LAB_SUITE_REGISTRY_FILE", "")
    if not registry_value:
        raise RuntimeError("LAB_SUITE_REGISTRY_FILE is required for Director resume")
    registry = load_suite_registry(Path(registry_value), PROJECT_ROOT / "data/runtime")
    director, planner = _director_engine(), _planner_engine()
    try:
        with director.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT state,request_json,payload_sha256,origin,owner_id "
                        "FROM lab.runs WHERE run_id=:run"
                    ),
                    {"run": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None or row["state"] != "running":
            raise ValueError("resume requires an existing running execution")
        target = _validate_dispatch_target(run_id, dict(row), registry)
        if target.purpose not in {"research", "mode-stream"}:
            raise ValueError("supported resume requires a research or diagnostic stream execution")
        observed = capture_current_owner(str(row["payload_sha256"]), run_id)
        process = OwnerProcessIdentity(
            observed.payload_sha256,
            observed.worker_pid,
            observed.worker_start_ticks,
            observed.worker_boot_id,
            observed.worker_unit,
            observed.worker_invocation_id,
            observed.worker_cgroup,
        )
        runtime = _private_runtime_directory(
            PROJECT_ROOT / "data/runtime/director-artifacts" / str(run_id)
        )
        try:
            receipt = resume_execution(
                director,
                planner,
                contract=target.contract,
                process=process,
                restart_id=restart_id,
                artifact_root=runtime,
            )
        except RecoveryPending:
            return {
                "run_id": str(run_id),
                "restart_id": str(restart_id),
                "state": "recovery_pending",
                "dispatch": "deferred",
            }
        with owned_execution(receipt.owner):
            try:
                if target.purpose == "mode-stream":
                    from lab.director.mode_stream import plan_from_request, run_mode_stream

                    if receipt.closure_only:
                        raise TimeoutError("original stream deadline expired; replay cannot resume")
                    with DirectorRunLease(director, run_id) as lease:
                        result = run_mode_stream(
                            director,
                            planner,
                            run_id=run_id,
                            plan=plan_from_request(target.request),
                            input_root=registry.runtime_root / "mode-stream-inputs",
                            artifact_root=runtime,
                            lease=lease,
                        )
                    return {
                        **_completed_dispatch_result(director, run_id, receipt.owner, result),
                        "restart_id": str(restart_id),
                        "admitted_owner": _admitted_owner_result(receipt.owner),
                        "generation": receipt.owner.generation,
                        "original_deadline_at": receipt.deadline_at.isoformat(),
                    }
                args = argparse.Namespace(
                    run_id=run_id,
                    suite_id=target.entry.suite_id,
                    suite_file=target.suite_path,
                    provider=target.entry.provider,
                    scenario_file=target.scenario_path,
                    provider_instance=None,
                    provider_registry_entry_sha256=target.entry_sha256,
                    proposal_limit=target.proposal_limit,
                    seed_wall_seconds=min(
                        MAX_WORKER_RUNTIME_SECONDS, target.budget["wall_seconds"]
                    ),
                    artifact_root=runtime,
                    resume_receipt=receipt,
                )
                result = _run_director(args)
                terminal = _completed_dispatch_result(director, run_id, receipt.owner, result)
                return {
                    **terminal,
                    "restart_id": str(restart_id),
                    "admitted_owner": _admitted_owner_result(receipt.owner),
                    "generation": receipt.owner.generation,
                    "closure_only": receipt.closure_only,
                    "original_deadline_at": receipt.deadline_at.isoformat(),
                }
            except Exception as error:
                return {
                    **_record_claimed_dispatch_failure(
                        director,
                        run_id,
                        error,
                        owner=receipt.owner,
                        failure_reason="resume_reconciliation_failed",
                    ),
                    "restart_id": str(restart_id),
                    "admitted_owner": _admitted_owner_result(receipt.owner),
                }
    finally:
        planner.dispose()
        director.dispose()


def _next_queued_run(engine: Engine) -> UUID | None:
    with engine.connect() as connection:
        value = connection.execute(
            select(runs.c.run_id)
            .where(runs.c.state == "queued")
            .order_by(runs.c.created_at, runs.c.run_id)
            .limit(1)
        ).scalar_one_or_none()
    return UUID(str(value)) if value is not None else None


def _drain_director_queue(*, poll_seconds: int) -> int:
    """Poll the durable queue in a detached service, never in AOS foreground work."""
    if not 1 <= poll_seconds <= 60:
        raise ValueError("Director queue poll interval must be in 1..60 seconds")
    engine = _director_engine()
    try:
        _recover_stopped_baselines_at_drain_start(engine)
        while True:
            run_id = _next_queued_run(engine)
            if run_id is None:
                time.sleep(poll_seconds)
                continue
            try:
                with engine.connect() as connection:
                    queued_request = connection.execute(
                        select(runs.c.request_json).where(runs.c.run_id == run_id)
                    ).scalar_one()
                if (
                    isinstance(queued_request, dict)
                    and queued_request.get("purpose") == "mode-stream"
                ):
                    # A stream's stopped sandbox is reconciled after this exact
                    # bounded owner exits; the queue process keeps no stream lease.
                    wall = queued_request.get("budget", {}).get("wall_seconds")
                    if type(wall) is not int or not 1 <= wall <= MAX_RUN_WALL_SECONDS:
                        raise ValueError("stream queue entry has an invalid original wall budget")
                    result = _launch_director_unit(
                        run_id, unit_runtime=wall + DIRECTOR_DISPATCH_OVERHEAD_SECONDS
                    )
                else:
                    if os.environ.get("LAB_CPU_ONLY") == "true" and (
                        not isinstance(queued_request, dict)
                        or queued_request.get("provider") != "mode-grid"
                    ):
                        raise ValueError("CPU-only profile cannot dispatch this queued provider")
                    result = _dispatch_director_run_owned(run_id)
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "state": "dispatch_error",
                            "error": type(exc).__name__,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                time.sleep(poll_seconds)
            else:
                print(json.dumps(result, sort_keys=True), flush=True)
                if result.get("state") == "stop_requested":
                    # The shipped unit Restart=on-failure replaces this OWN
                    # generation. Its successor can prove retired shared owner
                    # death; this living process cannot recover itself.
                    return 75
                if result.get("state") == "capacity_busy":
                    time.sleep(poll_seconds)
                else:
                    time.sleep(0.2)
    except KeyboardInterrupt:
        return 0
    finally:
        engine.dispose()


def _serve_api(*, host: str, port: int) -> int:
    """Serve the authenticated local API on loopback only."""
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("Lab API must bind to a loopback address")
    principal_value = os.environ.get("LAB_API_PRINCIPALS_FILE")
    registry_value = os.environ.get("LAB_SUITE_REGISTRY_FILE")
    if not principal_value or not registry_value:
        raise RuntimeError("API service requires principals and suite registry configuration")
    principals = load_principals(Path(principal_value))
    registry = load_suite_registry(Path(registry_value), PROJECT_ROOT / "data/runtime")
    app = create_app(principals=principals, suite_registry=registry)
    import uvicorn

    uvicorn.run(app, host=host, port=port, access_log=False, log_config=None)
    return 0


def _director_recovery(args: argparse.Namespace) -> dict[str, object]:
    """Inspect or apply one durable, idempotent interrupted-run recovery."""
    director = _director_engine()
    planner = _planner_engine()
    try:
        if args.recovery_action == "inspect":
            return inspect_recovery(director, planner, args.run_id)
        return _with_director_global_slot(
            args.run_id,
            lambda: apply_stop_and_finalize(
                director,
                planner,
                run_id=args.run_id,
                recovery_id=args.recovery_id,
                remaining_seconds=args.remaining_seconds,
                reconcile_interrupted_baseline=not getattr(
                    args, "close_unattempted_proposal", False
                ),
                reconcile_interrupted_proposal=getattr(args, "close_unattempted_proposal", False),
                artifact_root=_private_runtime_directory(
                    PROJECT_ROOT / "data/runtime/director-artifacts" / str(args.run_id)
                ),
            ),
        )
    finally:
        director.dispose()
        planner.dispose()


def _baseline_http_json(
    *, api_url: str, token: str, path: str, method: str, body: dict[str, object] | None = None
) -> dict[str, object]:
    """Call only a loopback Lab API and bound response size/time."""
    parsed = urllib.parse.urlsplit(api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("baseline API URL must be an explicit loopback HTTP origin")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if not loopback or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("baseline API URL must target loopback without a path or query")
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if payload is not None else {}),
        },
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: object, fp: object, code: int, msg: str, headers: object, new_url: str
        ) -> None:
            return None

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    )
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("Lab API response exceeds the bounded response size")
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RuntimeError("Lab API redirects are not supported") from None
        raise RuntimeError(f"Lab API rejected baseline request with HTTP {exc.code}") from None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Lab API returned an invalid JSON object")
    return value


def _submit_baseline(args: argparse.Namespace) -> dict[str, object]:
    token_path = args.token_file.expanduser()
    if token_path.is_symlink() or not token_path.is_file() or token_path.stat().st_mode & 0o077:
        raise ValueError("API token file must be a private regular file")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token or len(token) > 4096:
        raise ValueError("API token file is empty or malformed")
    accepted = _baseline_http_json(
        api_url=args.api_url,
        token=token,
        path="/v1/baselines",
        method="POST",
        body={
            "idempotency_key": args.idempotency_key,
            "suite": args.suite,
            "program_version": args.program_version,
            "budget": {
                "experiments": 0,
                "wall_seconds": args.wall_seconds,
                "model_tokens": 0,
            },
        },
    )
    run_id = accepted.get("run_id")
    if not isinstance(run_id, str):
        raise ValueError("Lab API did not return a run identity")
    run_id = str(UUID(run_id))
    result: dict[str, object] = {
        "run_id": run_id,
        "state": accepted.get("state"),
        "reused": accepted.get("reused"),
    }
    if not args.wait:
        return result
    deadline = time.monotonic() + args.wall_seconds + DIRECTOR_DISPATCH_OVERHEAD_SECONDS
    while time.monotonic() < deadline:
        status_value = _baseline_http_json(
            api_url=args.api_url, token=token, path=f"/v1/runs/{run_id}", method="GET"
        )
        state = status_value.get("state")
        result.update({"state": state, "stop_requested": status_value.get("stop_requested")})
        if state in {"completed", "failed", "stopped"}:
            report_envelope = _baseline_http_json(
                api_url=args.api_url,
                token=token,
                path=f"/v1/runs/{run_id}/report",
                method="GET",
            )
            report = report_envelope.get("report")
            if not isinstance(report, dict):
                raise ValueError("Lab API did not return a verified report")
            canonical = json.dumps(
                report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode("utf-8")
            import hashlib

            digest = hashlib.sha256(canonical).hexdigest()
            if digest != report_envelope.get("report_sha256"):
                raise ValueError("baseline report envelope hash did not verify")
            if report.get("schema") != "lab.baseline-report.v1" or report.get("run_id") != run_id:
                raise ValueError("baseline report identity or schema differs from the admitted run")
            if report.get("status") != state:
                raise ValueError("baseline report status differs from its terminal run")
            from lab.scorer.baseline_report import validate_baseline_report

            validated = validate_baseline_report(report)
            if validated != report:
                raise ValueError("baseline report is not the strict typed contract")
            result.update({"report_sha256": digest, "report": report})
            return result
        time.sleep(2)
    raise TimeoutError(
        "baseline run did not reach a terminal state before its bounded wait expired"
    )


def main(argv: list[str] | None = None) -> int:
    """Run one local command and emit a bounded JSON status record."""
    parser = argparse.ArgumentParser(prog="lab")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="bootstrap measured baselines and run Director proposals")
    run.add_argument("--run-id", type=UUID, required=True)
    run.add_argument("--suite-id", required=True)
    run.add_argument("--suite-file", type=Path, required=True)
    run.add_argument("--provider", choices=("fake-json", "local-qwen", "mode-grid"), required=True)
    run.add_argument("--scenario-file", type=Path)
    run.add_argument("--proposal-limit", type=int)
    run.add_argument("--seed-wall-seconds", type=int, default=MAX_WORKER_RUNTIME_SECONDS)
    run.add_argument("--artifact-root", type=Path, required=True)
    baseline = commands.add_parser("baseline", help="admit a standalone measured calibration run")
    baseline.add_argument("--suite", required=True)
    baseline.add_argument("--program-version", required=True)
    baseline.add_argument("--wall-seconds", type=int, required=True)
    baseline.add_argument("--idempotency-key", required=True)
    baseline.add_argument("--api-url", default="http://127.0.0.1:8766")
    baseline.add_argument("--token-file", type=Path, required=True)
    baseline.add_argument("--wait", action="store_true")
    report_command = commands.add_parser(
        "report", help="build an HTML report from verified run artifacts"
    )
    report_command.add_argument("run_id", type=UUID)
    report_command.add_argument("--artifact-root", type=Path)
    replay_command = commands.add_parser(
        "replay", help="recompute all exact Referee decisions for a run"
    )
    replay_command.add_argument("run_id", type=UUID)
    replay_command.add_argument("--artifact-root", type=Path)
    scorer = commands.add_parser("scorer", help="dispatch a durable Scorer task")
    scorer_commands = scorer.add_subparsers(dest="scorer_command", required=True)
    drain = scorer_commands.add_parser("drain", help="dispatch at most one queued task")
    drain.add_argument(
        "--remaining-seconds",
        type=int,
        default=MAX_WORKER_RUNTIME_SECONDS,
        help="remaining run budget passed to the isolated Scorer worker",
    )
    finalize = scorer_commands.add_parser(
        "finalize", help="finalize a sealed, fully scored run in the Scorer process"
    )
    finalize.add_argument("--run-id", type=UUID, required=True)
    finalize.add_argument("--admitted-generation", type=int)
    finalize.add_argument("--execution-sha256")
    finalize.add_argument("--empty-baseline-stop", action="store_true")
    finalize.add_argument(
        "--remaining-seconds",
        type=int,
        default=MAX_WORKER_RUNTIME_SECONDS,
    )
    care_calibration = scorer_commands.add_parser(
        "care-calibration",
        help="resume one fixed Scorer-private Farm B baseline calibration grid",
    )
    care_calibration.add_argument("--calibration-id", type=UUID, required=True)
    care_calibration.add_argument("--remaining-seconds", type=int, default=14_400)
    plan = commands.add_parser("plan", help="manage a trusted run task plan")
    plan_commands = plan.add_subparsers(dest="plan_command", required=True)
    seal = plan_commands.add_parser("seal", help="close the complete Director task plan")
    seal.add_argument("--run-id", type=UUID, required=True)
    api = commands.add_parser("api", help="serve the local authenticated Lab API")
    api_commands = api.add_subparsers(dest="api_command", required=True)
    serve = api_commands.add_parser("serve", help="bind the API to loopback")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    director = commands.add_parser("director", help="dispatch a durable Director run")
    director_commands = director.add_subparsers(dest="director_command", required=True)
    dispatch = director_commands.add_parser(
        "dispatch-one",
        help=(
            "claim one queued run; direct local-qwen use requires a matching run-bound "
            "SWAPP_LAB_GPU_UNIT, otherwise use the trusted drain service"
        ),
    )
    dispatch.add_argument("--run-id", type=UUID, required=True)
    resume_command = director_commands.add_parser(
        "resume",
        help="resume one proven-dead research owner using its original immutable execution",
    )
    resume_command.add_argument("--run-id", type=UUID, required=True)
    resume_command.add_argument("--restart-id", type=UUID, required=True)
    queue_drain = director_commands.add_parser(
        "drain", help="run as a detached durable Director queue consumer"
    )
    queue_drain.add_argument("--poll-seconds", type=int, default=2)
    recovery = director_commands.add_parser(
        "recovery", help="inspect or recover one interrupted run"
    )
    recovery_commands = recovery.add_subparsers(dest="recovery_action", required=True)
    inspect = recovery_commands.add_parser("inspect", help="read owner, child, and ledger status")
    inspect.add_argument("--run-id", type=UUID, required=True)
    apply = recovery_commands.add_parser(
        "apply", help="stop a proven-dead owner and finalize only verified terminal work"
    )
    apply.add_argument("--run-id", type=UUID, required=True)
    apply.add_argument("--recovery-id", type=UUID, required=True)
    apply.add_argument(
        "--close-unattempted-proposal",
        action="store_true",
        help="close one calibrated, registered proposal with no candidate jobs",
    )
    apply.add_argument("--remaining-seconds", type=int, default=MAX_WORKER_RUNTIME_SECONDS)
    gpu = commands.add_parser("gpu", help="run a bounded shared-GPU maintenance operation")
    gpu_commands = gpu.add_subparsers(dest="gpu_command", required=True)
    lease = gpu_commands.add_parser("lease", help="switch the shared GPU lane to a fixed mode")
    lease.add_argument("mode", choices=("TRAIN",))
    lease.add_argument("--noop", action="store_true", required=True)
    doctor = commands.add_parser("doctor", help="run a fixed local diagnostic")
    doctor_commands = doctor.add_subparsers(dest="doctor_command", required=True)
    train_doctor = doctor_commands.add_parser("train", help="measure the bounded QLoRA profile")
    train_doctor.add_argument("--dry-run", action="store_true", required=True)
    training_worker = commands.add_parser("training-worker", help=argparse.SUPPRESS)
    training_worker.add_argument("--operation-id", required=True)
    training_worker.add_argument(
        "--operation", choices=("train_noop", "train_dry_run"), required=True
    )
    args = parser.parse_args(argv)
    if args.command == "baseline":
        try:
            if not 1 <= args.wall_seconds <= MAX_RUN_WALL_SECONDS:
                parser.error("--wall-seconds must be in 1..14400")
            print(json.dumps(_submit_baseline(args), sort_keys=True))
            return 0
        except Exception as exc:
            print(f"lab baseline failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "api" and args.api_command == "serve":
        try:
            return _serve_api(host=args.host, port=args.port)
        except Exception as exc:
            print(f"lab API service failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "director" and args.director_command == "dispatch-one":
        try:
            result = _dispatch_director_run_owned(args.run_id)
            print(json.dumps(result, sort_keys=True))
            if result.get("state") == "capacity_busy":
                return 75
            if result.get("state") in {"failed", "recovery_required", "dispatch_error"}:
                return 1
            if (
                result.get("state") == "stopped"
                and result.get("automatic_stop_recovery") == "completed"
            ):
                return 0
            if result.get("owner_exit_code") not in {None, 0}:
                return 1
            return 0
        except Exception as exc:
            print(f"lab Director dispatch failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "director" and args.director_command == "resume":
        try:
            result = _resume_director_run_owned(args.run_id, args.restart_id)
            print(json.dumps(result, sort_keys=True))
            if result.get("state") in {"capacity_busy", "recovery_pending"}:
                return 75
            if result.get("state") in {"failed", "stop_requested", "recovery_required"}:
                return 1
            return 0 if result.get("owner_exit_code") in {None, 0} else 1
        except Exception as exc:
            print(f"lab Director resume failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "director" and args.director_command == "drain":
        try:
            return _drain_director_queue(poll_seconds=args.poll_seconds)
        except Exception as exc:
            print(f"lab Director queue worker failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "director" and args.director_command == "recovery":
        try:
            if args.recovery_action == "apply" and not 1 <= args.remaining_seconds <= 600:
                parser.error("--remaining-seconds must be in 1..600")
            result = _director_recovery(args)
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("recovery_state") != "failed" else 1
        except Exception as exc:
            print(f"lab Director recovery failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "gpu" and args.gpu_command == "lease":
        if args.mode != "TRAIN" or args.noop is not True:
            parser.error("only `lab gpu lease TRAIN --noop` is supported")
        from lab.training.maintenance import gpu_lease_main

        return gpu_lease_main([args.mode, "--noop"])
    if args.command == "doctor" and args.doctor_command == "train":
        if args.dry_run is not True:
            parser.error("only `lab doctor train --dry-run` is supported")
        from lab.training.maintenance import doctor_train_main

        return doctor_train_main(["--dry-run"])
    if args.command == "training-worker":
        from lab.training.worker import worker_main

        return worker_main(["--operation-id", args.operation_id, "--operation", args.operation])
    if args.command in {"report", "replay"}:
        report_engine: Engine | None = None
        try:
            run_id = args.run_id
            artifact_root = _verified_artifact_root(
                args.artifact_root
                or (PROJECT_ROOT / "data/runtime/director-artifacts" / str(run_id))
            )
            report_engine = _director_engine()
            if args.command == "report":
                run_report = build_run_report(report_engine, run_id, artifact_root=artifact_root)
                report_dir = _private_runtime_directory(PROJECT_ROOT / "data/runtime/reports")
                output = report_dir / f"{run_id}.html"
                temporary = report_dir / f".{run_id}.{os.getpid()}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(temporary, flags, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as report_file:
                        report_file.write(run_report.html)
                        report_file.flush()
                        os.fsync(report_file.fileno())
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
                os.replace(temporary, output)
                print(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "report": str(output),
                            "sha256": run_report.sha256,
                            "summary": run_report.summary,
                        },
                        sort_keys=True,
                    )
                )
            else:
                replayed = replay_run(report_engine, run_id, artifact_root=artifact_root)
                print(
                    json.dumps(
                        {
                            "run_id": str(run_id),
                            "replayed_decisions": [
                                {
                                    "experiment_id": item.experiment_id,
                                    "stage": item.decision_stage,
                                    "verdict": item.verdict,
                                    "delta": item.delta,
                                    "ci_low": item.ci_low,
                                    "noise_sd": item.noise_sd,
                                    "manifest_sha256": item.manifest_sha256,
                                    "configuration_sha256": item.configuration_sha256,
                                }
                                for item in replayed
                            ],
                        },
                        sort_keys=True,
                    )
                )
            return 0
        except Exception as exc:
            print(f"lab {args.command} failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        finally:
            if report_engine is not None:
                report_engine.dispose()
    if args.command == "run":
        try:
            result = _dispatch_director_run_owned(args.run_id)
            print(json.dumps(result, sort_keys=True))
            return (
                75
                if result.get("state") == "capacity_busy"
                else 1
                if result.get("state") in {"failed", "recovery_required", "dispatch_error"}
                or result.get("owner_exit_code") not in {None, 0}
                else 0
            )
        except Exception as exc:
            print(f"lab Director run failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "scorer" and args.scorer_command == "drain":
        if not 1 <= args.remaining_seconds <= MAX_WORKER_RUNTIME_SECONDS:
            parser.error("--remaining-seconds must be in 1..600")
        engine: Engine | None = None
        try:
            engine = _planner_engine()
            dispatch_result = _dispatch_one(engine, remaining_seconds=args.remaining_seconds)
            print(json.dumps(dispatch_result, sort_keys=True))
            return 0 if dispatch_result.get("process_exit_code", "0") == "0" else 1
        except Exception as exc:
            print(f"lab scorer dispatch failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        finally:
            if engine is not None:
                engine.dispose()
    if args.command == "scorer" and args.scorer_command == "finalize":
        if not 1 <= args.remaining_seconds <= MAX_WORKER_RUNTIME_SECONDS:
            parser.error("--remaining-seconds must be in 1..600")
        if args.empty_baseline_stop:
            if args.admitted_generation is not None or args.execution_sha256 is not None:
                parser.error("empty baseline stop cannot include an execution owner")
        elif args.admitted_generation is None or args.execution_sha256 is None:
            parser.error("finalize requires both --admitted-generation and --execution-sha256")
        try:
            if args.empty_baseline_stop:
                from lab.scorer.supervisor import run_scorer_empty_baseline_stop_process

                finalize_result = run_scorer_empty_baseline_stop_process(
                    args.run_id, remaining_seconds=args.remaining_seconds
                )
            else:
                if not isinstance(args.admitted_generation, int) or not isinstance(
                    args.execution_sha256, str
                ):
                    parser.error("finalize requires a typed generation and execution digest")
                finalize_result = run_scorer_finalize_process(
                    args.run_id,
                    admitted_generation=args.admitted_generation,
                    execution_sha256=args.execution_sha256,
                    remaining_seconds=args.remaining_seconds,
                )
            print(
                json.dumps(
                    {
                        "run_id": str(args.run_id),
                        "state": finalize_result.result.get("state", "worker_failed")
                        if finalize_result.result is not None
                        else "worker_failed",
                        "process_exit_code": str(finalize_result.exit_code),
                        "systemd_unit": finalize_result.unit,
                        "research_status": (finalize_result.result or {}).get(
                            "research_status", "pending"
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0 if finalize_result.exit_code == 0 else 1
        except Exception as exc:
            print(f"lab scorer finalize failed ({type(exc).__name__})", file=sys.stderr)
            return 1
    if args.command == "scorer" and args.scorer_command == "care-calibration":
        if not 1 <= args.remaining_seconds <= 14_400:
            parser.error("--remaining-seconds must be in 1..14400")
        calibration_engine: Engine | None = None
        try:
            from lab.scorer.care_calibration import run_care_calibration
            from lab.scorer.worker import DEFAULT_DSN_FILE, _secret

            calibration_engine = create_engine(
                _secret(configured_scorer_dsn_file(DEFAULT_DSN_FILE)),
                pool_size=1,
                max_overflow=0,
                pool_timeout=5,
            )
            result = run_care_calibration(
                calibration_engine,
                args.calibration_id,
                remaining_seconds=args.remaining_seconds,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("state") == "complete" else 1
        except Exception as exc:
            print(f"lab scorer CARE calibration failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        finally:
            if calibration_engine is not None:
                calibration_engine.dispose()
    if args.command == "plan" and args.plan_command == "seal":
        plan_engine: Engine | None = None
        try:
            plan_engine = _planner_engine()
            sealed = seal_run_task_plan(plan_engine, run_id=args.run_id)
            print(
                json.dumps(
                    {
                        "run_id": str(args.run_id),
                        "task_plan_sha256": sealed.sha256,
                        "task_count": sealed.task_count,
                        "state": "sealed",
                    },
                    sort_keys=True,
                )
            )
            return 0
        except Exception as exc:
            print(f"lab plan seal failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        finally:
            if plan_engine is not None:
                plan_engine.dispose()
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
