"""Review candidate: read-only API capability, using existing principal/registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, cast

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from lab.api.registry import ApiPrincipal, SuiteRegistry


class LabCapability(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["scientist.lab-capability.v1"] = Field(alias="schema")
    owner_id: str
    origin: Literal["aos"]
    suite_id: str
    track: Literal["anomaly", "mode"]
    program_version: str
    provider: Literal["local-qwen"]
    suite_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    suite_entry_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_config_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    proposal_limit: int = Field(ge=1, le=35)
    proposal_contract: Literal["candidate-python.v1", "operating-mode-config.v1"]
    allowed_purpose: Literal["research"]
    gpu_release_verified: Literal[False]


def install_aos_capability_route(
    app: FastAPI, resolve_principal: Callable[[str | None], ApiPrincipal]
) -> None:
    """Install inside create_app with its ORIGINAL cached principal resolver."""

    @app.get("/v1/aos-capability/{suite_id}", response_model=LabCapability)
    def capability(
        suite_id: str, authorization: Annotated[str | None, Header()] = None
    ) -> LabCapability:
        principal = resolve_principal(authorization)
        if principal.origin != "aos":
            raise HTTPException(status_code=403, detail="AOS principal required")
        registry = cast(SuiteRegistry | None, app.state.suite_registry)
        if registry is None:
            raise HTTPException(status_code=503, detail="Trusted suite registry unavailable")
        try:
            entry = registry.get(suite_id)
            registry.verify_entry(entry)
        except (KeyError, ValueError, OSError):
            raise HTTPException(status_code=404, detail="Verified suite unavailable") from None
        if entry.provider != "local-qwen" or "research" not in entry.allowed_purposes:
            raise HTTPException(status_code=422, detail="Registered local model research required")
        if entry.provider_config_sha256 is None:
            raise HTTPException(status_code=503, detail="Registered provider pin unavailable")
        contract = getattr(entry, "proposal_contract", "candidate-python.v1")
        if contract not in {"candidate-python.v1", "operating-mode-config.v1"}:
            raise HTTPException(status_code=503, detail="Registered proposal contract incompatible")
        return LabCapability(
            schema="scientist.lab-capability.v1",
            owner_id=principal.owner_id,
            origin=principal.origin,
            suite_id=entry.suite_id,
            track=entry.track,
            program_version=entry.program_version,
            provider=entry.provider,
            suite_manifest_sha256=entry.suite_manifest_sha256,
            suite_entry_sha256=SuiteRegistry.entry_sha256(entry),
            provider_config_sha256=entry.provider_config_sha256,
            proposal_limit=entry.proposal_limit,
            proposal_contract=cast(
                Literal["candidate-python.v1", "operating-mode-config.v1"], contract
            ),
            allowed_purpose="research",
            gpu_release_verified=False,
        )
