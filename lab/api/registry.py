"""Private, hash-pinned API principals and suite execution registry."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

MAX_PROPOSAL_SCENARIO_BYTES = 4 * 1024**2


class ApiPrincipal(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    token: StrictStr = Field(min_length=32, max_length=512)
    origin: Literal["local", "aos"]
    owner_id: StrictStr = Field(min_length=1, max_length=128)


class PrincipalFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["lab-api-principals.v1"] = Field(alias="schema")
    principals: tuple[ApiPrincipal, ...] = Field(min_length=1, max_length=16)

    @field_validator("principals", mode="before")
    @classmethod
    def normalize_principals(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class SuiteEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    suite_id: StrictStr = Field(min_length=1, max_length=128)
    track: Literal["anomaly", "mode"]
    program_version: StrictStr = Field(min_length=1, max_length=64)
    suite_manifest_path: StrictStr = Field(min_length=1, max_length=1024)
    suite_manifest_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["fake-json", "local-qwen", "mode-grid"]
    scenario_path: StrictStr | None = Field(default=None, min_length=1, max_length=1024)
    scenario_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_config_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_limit: int = Field(ge=1, le=35)
    allowed_purposes: tuple[Literal["research", "baseline"], ...] = ("research", "baseline")

    @field_validator("allowed_purposes", mode="before")
    @classmethod
    def validate_allowed_purposes(cls, value: object) -> object:
        if not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 2:
            raise ValueError("suite must allow one or two explicit purposes")
        if any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
            raise ValueError("suite allowed purposes cannot repeat")
        return tuple(value)

    @model_validator(mode="after")
    def validate_provider_config(self) -> SuiteEntry:
        if self.provider == "fake-json":
            if (
                self.scenario_path is None
                or self.scenario_sha256 is None
                or self.provider_config_sha256 is not None
            ):
                raise ValueError("fake-json suite requires only its scenario receipt")
        elif self.provider == "mode-grid":
            if (
                self.scenario_path is None
                or self.scenario_sha256 is None
                or self.provider_config_sha256 != self.scenario_sha256
            ):
                raise ValueError("mode-grid requires one exact configuration/scenario digest")
        elif (
            self.scenario_path is not None
            or self.scenario_sha256 is not None
            or self.provider_config_sha256 is None
        ):
            raise ValueError("local-qwen suite requires a provider configuration digest")
        return self


class SuiteRegistryFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["lab-suite-registry.v1"] = Field(alias="schema")
    suites: tuple[SuiteEntry, ...] = Field(min_length=1, max_length=64)

    @field_validator("suites", mode="before")
    @classmethod
    def normalize_suites(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class SuiteRegistry:
    """Validated mapping; resolved input paths stay inside private runtime data."""

    def __init__(self, entries: tuple[SuiteEntry, ...], runtime_root: Path) -> None:
        if runtime_root.is_symlink():
            raise ValueError("runtime root cannot be a symlink")
        self.runtime_root = runtime_root.resolve(strict=True)
        mapped: dict[str, SuiteEntry] = {}
        for entry in entries:
            if entry.suite_id in mapped:
                raise ValueError("suite registry repeats a suite id")
            self.verify_file(
                entry.suite_manifest_path,
                entry.suite_manifest_sha256,
                maximum_bytes=MAX_SUITE_MANIFEST_BYTES,
            )
            if entry.provider in {"fake-json", "mode-grid"}:
                if entry.scenario_path is None or entry.scenario_sha256 is None:
                    raise ValueError("fake provider scenario receipt is incomplete")
                self.verify_file(
                    entry.scenario_path,
                    entry.scenario_sha256,
                    maximum_bytes=MAX_PROPOSAL_SCENARIO_BYTES,
                )
            mapped[entry.suite_id] = entry
        self.entries = mapped

    def get(self, suite_id: str) -> SuiteEntry:
        try:
            return self.entries[suite_id]
        except KeyError:
            raise KeyError("suite is not registered") from None

    def verify_entry(self, entry: SuiteEntry) -> tuple[Path, Path | None]:
        if self.entries.get(entry.suite_id) != entry:
            raise ValueError("suite entry does not belong to this loaded registry")
        manifest = self.verify_file(
            entry.suite_manifest_path,
            entry.suite_manifest_sha256,
            maximum_bytes=MAX_SUITE_MANIFEST_BYTES,
        )
        scenario = None
        if entry.provider in {"fake-json", "mode-grid"}:
            if entry.scenario_path is None or entry.scenario_sha256 is None:
                raise ValueError("fake provider scenario receipt is incomplete")
            scenario = self.verify_file(
                entry.scenario_path,
                entry.scenario_sha256,
                maximum_bytes=MAX_PROPOSAL_SCENARIO_BYTES,
            )
        return manifest, scenario

    @staticmethod
    def entry_sha256(entry: SuiteEntry) -> str:
        payload = entry.model_dump(mode="json")
        # Default both-purpose entries retain their historical admission digest.
        if set(entry.allowed_purposes) == {"research", "baseline"}:
            payload.pop("allowed_purposes")
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def resolve(self, relative_path: str, expected_sha256: str, *, maximum_bytes: int) -> Path:
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("registered paths must be clean runtime-relative paths")
        candidate = self.runtime_root.joinpath(path)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("registered input must be a regular non-symlink file")
        resolved = candidate.resolve(strict=True)
        if self.runtime_root not in resolved.parents:
            raise ValueError("registered input escaped the private runtime root")
        return self._verify_path(resolved, expected_sha256, maximum_bytes=maximum_bytes)

    def verify_file(
        self,
        relative_path: str,
        expected_sha256: str,
        *,
        maximum_bytes: int = MAX_SUITE_MANIFEST_BYTES,
    ) -> Path:
        return self.resolve(relative_path, expected_sha256, maximum_bytes=maximum_bytes)

    def _verify_path(self, path: Path, expected_sha256: str, *, maximum_bytes: int) -> Path:
        metadata = path.stat()
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IRGRP | stat.S_IROTH):
            raise ValueError("registered input must be private and not group/world writable")
        if metadata.st_size > maximum_bytes:
            raise ValueError("registered input exceeds the size limit")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("registered input hash differs from its trusted registry entry")
        return path


def _read_private_json(path: Path, *, maximum_bytes: int = 64 * 1024) -> object:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("private registry must be a regular, non-symlink file")
    metadata = candidate.stat()
    if metadata.st_mode & 0o077 or metadata.st_size > maximum_bytes:
        raise ValueError("private registry permissions or size are invalid")
    return json.loads(candidate.read_bytes())


def load_principals(path: Path) -> tuple[ApiPrincipal, ...]:
    document = PrincipalFile.model_validate(_read_private_json(path), strict=True)
    if len({item.token for item in document.principals}) != len(document.principals):
        raise ValueError("principal token is duplicated")
    if len({(item.origin, item.owner_id) for item in document.principals}) != len(
        document.principals
    ):
        raise ValueError("principal origin/owner identity is duplicated")
    return document.principals


def load_suite_registry(path: Path, runtime_root: Path) -> SuiteRegistry:
    document = SuiteRegistryFile.model_validate(_read_private_json(path), strict=True)
    return SuiteRegistry(document.suites, runtime_root)
