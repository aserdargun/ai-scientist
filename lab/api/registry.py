"""Private, hash-pinned API principals and suite execution registry."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)

from lab.suite_limits import MAX_SUITE_MANIFEST_BYTES

if TYPE_CHECKING:
    from lab.operating_modes.public_snapshot import PublicTaskSnapshot


MAX_PROPOSAL_SCENARIO_BYTES = 4 * 1024**2
MAX_REGISTRY_BYTES = 64 * 1024


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


class PublicDevStudy(BaseModel):
    """One immutable CPU development grant, inside the existing registry authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    owner_id: StrictStr = Field(min_length=1, max_length=128)
    origin: Literal["local"]
    snapshot_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    binding_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    source_suite_manifest_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    original_task_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    profile_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    max_experiments: int = Field(ge=1, le=2)
    max_wall_seconds: int = Field(ge=1, le=600)
    model_tokens: int = Field(ge=0, le=0)

    def verify_snapshot(self, snapshot: PublicTaskSnapshot) -> None:
        if (
            self.snapshot_sha256 != snapshot.sha256
            or self.binding_sha256 != snapshot.binding.sha256
            or self.source_suite_manifest_sha256 != snapshot.binding.source_suite_manifest_sha256
            or self.original_task_sha256 != snapshot.binding.original_task_sha256
            or self.profile_sha256 != snapshot.binding.original_task.profile_sha256
        ):
            raise ValueError("public registry grant differs from the source binding")

    def verify_budget(self, experiments: int, wall_seconds: int, model_tokens: int) -> None:
        if (
            any(type(value) is not int for value in (experiments, wall_seconds, model_tokens))
            or not 1 <= experiments <= self.max_experiments
            or not 1 <= wall_seconds <= self.max_wall_seconds
            or model_tokens != 0
        ):
            raise ValueError("public development study exceeds its CPU policy")


class SuiteEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    suite_id: StrictStr = Field(min_length=1, max_length=128)
    track: Literal["anomaly", "mode"]
    program_version: StrictStr = Field(min_length=1, max_length=64)
    suite_manifest_path: StrictStr = Field(min_length=1, max_length=1024)
    suite_manifest_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["fake-json", "local-qwen", "mode-grid", "mode-stream"]
    scenario_path: StrictStr | None = Field(default=None, min_length=1, max_length=1024)
    scenario_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_config_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    proposal_limit: int = Field(ge=1, le=35)
    proposal_contract: Literal["candidate-python.v1", "operating-mode-config.v1"] = (
        "candidate-python.v1"
    )
    snapshot_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    public_dev_study: PublicDevStudy | None = None
    allowed_purposes: tuple[Literal["research", "baseline", "mode-stream"], ...] = (
        "research",
        "baseline",
    )

    @model_serializer(mode="wrap")
    def preserve_legacy_bytes(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value = cast(dict[str, Any], handler(self))
        if self.public_dev_study is None:
            value.pop("public_dev_study", None)
        return value

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
        if self.provider == "mode-stream":
            if (
                self.track != "mode"
                or self.program_version != "mode-stream.v1"
                or self.allowed_purposes != ("mode-stream",)
                or self.proposal_limit != 1
                or self.scenario_path is not None
                or self.scenario_sha256 is not None
                or self.provider_config_sha256 is None
                or self.snapshot_sha256 is not None
                or self.proposal_contract != "candidate-python.v1"
            ):
                raise ValueError("mode-stream requires a finite diagnostic-only plan")
            return self
        if "mode-stream" in self.allowed_purposes:
            raise ValueError("mode-stream purpose requires its explicit provider")
        if self.proposal_contract == "operating-mode-config.v1":
            if (
                self.provider != "local-qwen"
                or self.track != "mode"
                or self.snapshot_sha256 is None
            ):
                raise ValueError(
                    "mode configuration proposals require local-qwen/mode snapshot pins"
                )
        elif self.snapshot_sha256 is not None and self.public_dev_study is None:
            raise ValueError("snapshot pins require the operating mode proposal contract")
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
        if self.public_dev_study is not None:
            if (
                self.provider != "mode-grid"
                or self.track != "mode"
                or self.proposal_contract != "candidate-python.v1"
                or self.snapshot_sha256 != self.public_dev_study.snapshot_sha256
                or self.proposal_limit > self.public_dev_study.max_experiments
                or self.allowed_purposes != ("research",)
            ):
                raise ValueError("public development requires its bounded CPU grid registry entry")
        return self


class SuiteRegistryFile(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["lab-suite-registry.v1"] = Field(alias="schema")
    suites: tuple[SuiteEntry, ...] = Field(max_length=64)

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
        for entry in entries:
            self.verify_snapshot(entry)

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
        self.verify_snapshot(entry)
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
        if entry.public_dev_study is None:
            payload.pop("public_dev_study", None)
        if entry.proposal_contract == "candidate-python.v1" and entry.snapshot_sha256 is None:
            payload.pop("proposal_contract")
            payload.pop("snapshot_sha256")
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

    def verify_snapshot(self, entry: SuiteEntry) -> Path | None:
        """Bind the one-task exception to the exact Scorer-installed source snapshot."""
        if entry.public_dev_study is not None:
            from lab.api.mode_experiments import ModeSnapshotStore
            from lab.api.mode_sources import authorize_snapshot
            from lab.director.suite_manifest import SuiteManifest

            store = ModeSnapshotStore(self.runtime_root / "mode-snapshots")
            snapshot = store.public_snapshot(entry.public_dev_study.snapshot_sha256)
            if snapshot is None or store.installed(snapshot.sha256) is None:
                raise ValueError("public development snapshot is not installed")
            entry.public_dev_study.verify_snapshot(snapshot)
            authorize_snapshot(store, snapshot.sha256, entry.public_dev_study.owner_id)
            path = self.verify_file(entry.suite_manifest_path, entry.suite_manifest_sha256)
            document = SuiteManifest.model_validate_json(path.read_bytes(), strict=True)
            if (
                document.suite_id != entry.suite_id
                or document.weight_policy != "single_snapshot_study.v1"
                or document.family_cap != 1.0
                or len(document.tasks) != 1
            ):
                raise ValueError("public development suite binding differs")
            snapshot.binding.verify_study_task(document.tasks[0])
            return path
        if entry.snapshot_sha256 is None:
            return None
        from lab.api.mode_experiments import ModeSnapshotStore
        from lab.director.local_llm import provider_profile_set_for_sha256
        from lab.director.suite_manifest import SuiteManifest

        root = self.runtime_root / "mode-snapshots"
        if not root.is_dir():
            raise ValueError("registered mode snapshot store is unavailable")
        store = ModeSnapshotStore(root)
        store.load(entry.snapshot_sha256)
        if store.installed(entry.snapshot_sha256) is None:
            raise ValueError("registered mode snapshot has no Scorer installation receipt")
        path = self.verify_file(entry.suite_manifest_path, entry.suite_manifest_sha256)
        document = SuiteManifest.model_validate_json(path.read_bytes(), strict=True)
        if (
            document.suite_id != entry.suite_id
            or document.weight_policy != "single_snapshot_study.v1"
            or len(document.tasks) != 1
            or document.tasks[0].provenance.source_manifest_sha256 != entry.snapshot_sha256
        ):
            raise ValueError("mode suite does not match its pinned installed snapshot")
        original = SuiteManifest.model_validate_json(
            (store.directory(entry.snapshot_sha256) / "suite.json").read_bytes(), strict=True
        )
        if document.model_copy(update={"suite_id": original.suite_id}) != original:
            raise ValueError("mode suite differs from its Scorer-installed task and weights")
        if entry.provider_config_sha256 is None:
            raise ValueError("mode provider configuration digest is missing")
        provider_profile_set_for_sha256(entry.provider_config_sha256, entry.proposal_contract)
        return path

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


def _read_private_json(path: Path, *, maximum_bytes: int = MAX_REGISTRY_BYTES) -> object:
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
