"""Owner-bound, read-only CPU study description; it grants no launch or GPU rights."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lab.api.mode_experiments import ModeSnapshotStore
from lab.api.registry import ApiPrincipal, SuiteEntry, SuiteRegistry
from lab.director.parameter_grid import MAX_GRID_BYTES, ParameterGridProvider
from lab.operating_modes.contracts import ModeConfig


class LabCpuCapability(BaseModel):
    """Separate from the existing local-model capability and its authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["scientist.lab-cpu-capability.v1"] = Field(alias="schema")
    owner_id: str
    origin: Literal["aos"]
    suite_id: str
    track: Literal["mode"]
    program_version: str
    provider: Literal["mode-grid"]
    source_kind: Literal["synthetic"]
    suite_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    suite_entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    aos_cpu_study_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_experiments: int = Field(ge=1, le=35)
    max_wall_seconds: int = Field(ge=1, le=14_400)
    model_tokens: Literal[0]
    allowed_purpose: Literal["research"]
    allocation_authority: Literal[False]
    gpu_release_verified: Literal[False]
    native_inference_authorized: Literal[False]
    launch_authorized: Literal[False]


class CpuSnapshotSummary(BaseModel):
    """Public feature metadata only; evaluation input counts precede Scorer filtering."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source_kind: Literal["synthetic"]
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str = Field(pattern=r"^synthetic\.[a-z_]+$")
    source_version: str = Field(pattern=r"^v1\.seed[0-9]{1,10}$")
    sensors: list[Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]] = Field(
        min_length=1, max_length=64
    )
    rows: int = Field(ge=3, le=4096)
    train_rows: int = Field(ge=2, le=4095)
    evaluation_input_rows: int = Field(
        ge=1,
        le=4094,
        description="Snapshot input rows before embargo/masks; not the scored row count.",
    )
    first_utc: str
    last_utc: str
    entity_authority: Literal[False]


class CpuConfigurationDescriptor(BaseModel):
    """An ordered registered configuration, never an observed score or recommendation."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    position: int = Field(ge=1, le=35)
    method: Literal["lsh", "optics", "som"]
    configuration: ModeConfig
    configuration_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class CpuRegisteredGrid(BaseModel):
    """A request with experiments=N uses the first N entries of this available prefix."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    selection: Literal["ordered_registered_prefix"]
    registered_configuration_count: int = Field(ge=1, le=35)
    available_prefix_count: int = Field(ge=1, le=35)
    request_semantics: Literal["first_n_of_available_prefix"]
    configurations: list[CpuConfigurationDescriptor] = Field(min_length=1, max_length=35)


class LabCpuStudy(BaseModel):
    """Source-only proposed description; it adds no registration or launch authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["scientist.lab-cpu-study.v1"] = Field(alias="schema")
    capability: LabCpuCapability
    snapshot: CpuSnapshotSummary
    grid: CpuRegisteredGrid


def install_aos_cpu_capability_route(
    app: FastAPI, resolve_principal: Callable[[str | None], ApiPrincipal]
) -> None:
    """Reuse create_app's original cached principal resolver; perform no DB or effects."""

    def verified_capability(
        suite_id: str, authorization: str | None
    ) -> tuple[LabCpuCapability, SuiteEntry, SuiteRegistry]:
        principal = resolve_principal(authorization)
        if principal.origin != "aos":
            raise HTTPException(403, "AOS principal required")
        registry = cast(SuiteRegistry | None, app.state.suite_registry)
        if registry is None:
            raise HTTPException(503, "Trusted suite registry unavailable")
        try:
            entry = registry.get(suite_id)
            grant = entry.aos_cpu_study
            if (
                grant is None
                or entry.provider != "mode-grid"
                or entry.track != "mode"
                or entry.allowed_purposes != ("research",)
                or entry.public_dev_study is not None
                or entry.proposal_contract != "candidate-python.v1"
                or entry.provider_config_sha256 is None
                or entry.snapshot_sha256 is None
                or entry.provider_config_sha256 != grant.provider_config_sha256
                or entry.snapshot_sha256 != grant.snapshot_sha256
                or entry.proposal_limit > grant.max_experiments
            ):
                raise ValueError("CPU study binding unavailable")
            grant.authorize(principal.origin, principal.owner_id)
            registry.verify_entry(entry)
        except (KeyError, ValueError, OSError):
            # An absent/foreign grant and artifact drift reveal no paths or owner details.
            raise HTTPException(404, "Verified CPU study unavailable") from None
        result = LabCpuCapability(
            schema="scientist.lab-cpu-capability.v1",
            owner_id=principal.owner_id,
            origin="aos",
            suite_id=entry.suite_id,
            track="mode",
            program_version=entry.program_version,
            provider="mode-grid",
            source_kind="synthetic",
            suite_manifest_sha256=entry.suite_manifest_sha256,
            suite_entry_sha256=registry.entry_sha256(entry),
            provider_config_sha256=entry.provider_config_sha256,
            snapshot_sha256=entry.snapshot_sha256,
            aos_cpu_study_sha256=grant.sha256,
            max_experiments=min(grant.max_experiments, entry.proposal_limit),
            max_wall_seconds=grant.max_wall_seconds,
            model_tokens=0,
            allowed_purpose="research",
            allocation_authority=False,
            gpu_release_verified=False,
            native_inference_authorized=False,
            launch_authorized=False,
        )
        return result, entry, registry

    @app.get("/v1/aos-cpu-capability/{suite_id}", response_model=LabCpuCapability)
    def capability(
        suite_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> LabCpuCapability:
        return verified_capability(suite_id, authorization)[0]

    @app.get("/v1/aos-cpu-study/{suite_id}", response_model=LabCpuStudy)
    def study(suite_id: str, authorization: Annotated[str | None, Header()] = None) -> LabCpuStudy:
        result, entry, registry = verified_capability(suite_id, authorization)
        try:
            # These interfaces verify feature/config bytes without reading evaluator labels.
            store = ModeSnapshotStore(registry.runtime_root / "mode-snapshots")
            snapshot = store.load(result.snapshot_sha256)
            scenario = registry.resolve(
                cast(str, entry.scenario_path),
                cast(str, entry.scenario_sha256),
                maximum_bytes=MAX_GRID_BYTES,
            )
            provider = ParameterGridProvider.load(
                scenario,
                configuration_sha256=result.provider_config_sha256,
                registry_entry_sha256=result.suite_entry_sha256,
            )
            summary = CpuSnapshotSummary(
                source_kind="synthetic",
                snapshot_sha256=snapshot.sha256,
                source_id=snapshot.selection.source_id,
                source_version=snapshot.selection.source_version,
                sensors=list(snapshot.sensors),
                rows=len(snapshot.values),
                train_rows=snapshot.train_rows,
                evaluation_input_rows=len(snapshot.values) - snapshot.train_rows,
                first_utc=snapshot.timestamps_utc[0],
                last_utc=snapshot.timestamps_utc[-1],
                entity_authority=False,
            )
            configurations = []
            for position, descriptor in enumerate(provider.entries[: result.max_experiments], 1):
                config = ModeConfig.model_validate(descriptor["configuration"], strict=True)
                configurations.append(
                    CpuConfigurationDescriptor(
                        position=position,
                        method=config.method,
                        configuration=config,
                        configuration_sha256=descriptor["configuration_sha256"],
                        candidate_sha256=descriptor["candidate_sha256"],
                    )
                )
            if provider.snapshot_sha256 != result.snapshot_sha256:
                raise ValueError("CPU grid snapshot changed")
            # Recheck kind/installation/suite binding after constructing the bounded view.
            registry.verify_entry(entry)
        except (KeyError, ValueError, OSError):
            raise HTTPException(404, "Verified CPU study unavailable") from None
        return LabCpuStudy(
            schema="scientist.lab-cpu-study.v1",
            capability=result,
            snapshot=summary,
            grid=CpuRegisteredGrid(
                selection="ordered_registered_prefix",
                registered_configuration_count=len(provider.entries),
                available_prefix_count=len(configurations),
                request_semantics="first_n_of_available_prefix",
                configurations=configurations,
            ),
        )
