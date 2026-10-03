"""Pure receipt lifetime/drift tests; synthetic gates are not native model proof."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import threading
import venv
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import aos_native_artifact_receipts as receipts


def test_dependency_readback_preserves_full_map_and_duplicate_discovery_order(monkeypatch, capsys):
    class Distribution(importlib.metadata.Distribution):
        def __init__(self, name, version):
            self.fields = {"Name": name, "Version": version}
            self.reads = 0

        @property
        def metadata(self):
            self.reads += 1
            return self.fields

        def read_text(self, filename):
            return ""

        def locate_file(self, path):
            return Path(path)

    distributions = [
        Distribution("First_Name", "1"),
        Distribution("first-name", "2"),
        Distribution("Missing-Version", None),
    ]
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter(distributions))
    exec(receipts._DEPENDENCY_CODE, {})  # pylint: disable=exec-used
    assert json.loads(capsys.readouterr().out) == {"first-name": "2", "missing-version": None}
    assert [distribution.reads for distribution in distributions] == [1, 1, 1]


def test_dependency_readback_preserves_custom_version_provider(monkeypatch, capsys):
    class Distribution(importlib.metadata.Distribution):
        @property
        def metadata(self):
            return {"Name": "Custom_Provider", "Version": "metadata-version"}

        @property
        def version(self):
            return "provider-version"

        def read_text(self, filename):
            return ""

        def locate_file(self, path):
            return Path(path)

    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter([Distribution()]))
    exec(receipts._DEPENDENCY_CODE, {})  # pylint: disable=exec-used
    assert json.loads(capsys.readouterr().out) == {"custom-provider": "provider-version"}


@pytest.mark.parametrize(
    "layout,text",
    [
        ("METADATA", "Name: Mixed_Case.Package\nVersion: 1.0\n"),
        ("METADATA", "Name: Folded_\n        Package\nVersion: 1.\n        2\n"),
        ("METADATA", "Name: First_Name\nName: Ignored\nVersion: 1\nVersion: 2\n"),
        ("METADATA", "Name: Missing_Version\n"),
        ("METADATA", "Version: 1.0\n"),
        ("METADATA", "Name: Header_Only\nVersion: 1\n\nName: Not-A-Header\nVersion: 9\n"),
        ("PKG-INFO", "Name: Fallback_Package\nVersion: 2.0\n"),
        ("empty-METADATA", "Name: Empty_Metadata_Fallback\nVersion: 3.0\n"),
        ("old-egg-info", "Name: Old_Egg\nVersion: 4.0\n"),
    ],
)
def test_dependency_headers_match_original_path_distribution_semantics(tmp_path, layout, text):
    path = tmp_path / "fixture.dist-info"
    if layout == "old-egg-info":
        path = tmp_path / "fixture.egg-info"
        path.write_text(text)
    else:
        path.mkdir()
        filename = "PKG-INFO" if layout == "empty-METADATA" else layout
        (path / filename).write_text(text)
        if layout == "empty-METADATA":
            (path / "METADATA").write_text("")
    distribution = importlib.metadata.PathDistribution(path)
    fields = distribution.metadata
    namespace = {}
    exec(receipts._DEPENDENCY_PREAMBLE, namespace)  # pylint: disable=exec-used
    if fields["Name"] is None:
        with pytest.raises(AttributeError):
            namespace["dependency"](distribution)
    else:
        expected = fields["Name"].lower().replace("_", "-"), fields["Version"]
        assert namespace["dependency"](distribution) == expected


def test_dependency_headers_preserve_custom_path_distribution_provider(tmp_path):
    class CustomDistribution(importlib.metadata.PathDistribution):
        reads = 0

        @property
        def metadata(self):
            self.reads += 1
            return {"Name": "Custom_Path", "Version": "metadata-version"}

        @property
        def version(self):
            return "provider-version"

        def read_text(self, filename):
            raise AssertionError("custom provider must retain its original metadata path")

    distribution = CustomDistribution(tmp_path)
    namespace = {}
    exec(receipts._DEPENDENCY_PREAMBLE, namespace)  # pylint: disable=exec-used
    assert namespace["dependency"](distribution) == ("custom-path", "provider-version")
    assert distribution.reads == 1


def test_interpreter_receipt_supports_bounded_large_executable_without_widening_sources(tmp_path):
    target = tmp_path / "python"
    with target.open("wb") as stream:
        stream.truncate(8 * 1024**2 + 1)
    target.chmod(0o700)
    observed = receipts._interpreter(str(target))
    assert observed["identity"]["st_size"] == 8 * 1024**2 + 1
    with pytest.raises(receipts.NativeArtifactReceiptError, match="byte bound"):
        receipts._small_file(target)


def test_interpreter_receipt_rejects_executable_above_32_mib(tmp_path):
    target = tmp_path / "python"
    with target.open("wb") as stream:
        stream.truncate(32 * 1024**2 + 1)
    target.chmod(0o700)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="byte bound"):
        receipts._interpreter(str(target))


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def receipt_fixture(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(receipts, "_clock", lambda: now[0])
    monkeypatch.setattr(receipts, "_boot", lambda: "actual-test-boot")
    dependencies = {"exact-native-package": "1.0"}
    dependency_read = Mock(return_value=dependencies)

    def read_dependencies(interpreter, *, verify_inputs=None):
        if verify_inputs is not None:
            verify_inputs()
        return dependency_read.return_value

    dependency_read.side_effect = read_dependencies
    monkeypatch.setattr(receipts, "_dependencies", dependency_read)
    python = tmp_path / "model-python"
    python.write_bytes(b"synthetic executable model interpreter")
    python.chmod(0o700)
    app_python = tmp_path / "aos-python"
    app_python.symlink_to(python)
    source = tmp_path / "reviewed-source.py"
    source.write_bytes(b"original native verification source")
    source_inputs = {str(source): file_hash(source)}
    requirements, bindings = {}, {}
    for profile, kind in (
        ("aos.decider.turn.v1", "decider"),
        ("aos.bonsai.vision.v1", "bonsai-vision"),
    ):
        model, runtime = tmp_path / (kind + "-model"), tmp_path / (kind + "-runtime")
        model.mkdir()
        runtime.mkdir()
        weights = model / "weights"
        weights.write_bytes(b"original model bytes")
        code = runtime / "runtime"
        code.write_bytes(b"original runtime bytes")
        manifest = {"model_path": str(model), "model_files": {"weights": file_hash(weights)}}
        native_lib = tmp_path / "native-library"
        if kind == "decider":
            manifest.update(
                code_path=str(runtime),
                code_files={"runtime": file_hash(code)},
                dependencies=dependencies,
            )
            interpreter = python
        else:
            alias = runtime / "runtime-alias.so"
            alias.symlink_to(code.name)
            native_lib.write_bytes(b"original native library")
            manifest.update(
                runtime_path=str(runtime),
                runtime_files={"runtime": file_hash(code), "runtime-alias.so": file_hash(code)},
                native_libraries={str(native_lib): file_hash(native_lib)},
            )
            interpreter = app_python
        manifest_path = tmp_path / (kind + "-manifest.json")
        manifest_path.write_bytes(receipts.canonical(manifest))
        requirements[profile] = {
            "manifest": manifest,
            "manifest_path": str(manifest_path),
            "profile": {"kind": kind, "manifest_sha256": file_hash(manifest_path)},
            "artifact_roots": [str(model), str(runtime)],
            "interpreter": str(interpreter),
            "interpreter_target": str(python),
        }
        bindings[profile] = {"profile_id": profile, "independent_policy_sha256": "a" * 64}
    readback = dict(
        schema="aos-model-environment-readback.v1",
        interpreter=str(python),
        artifact_bytes_verified=True,
        dependency_versions_verified=True,
        source_and_manifests_unchanged=True,
        model_instantiated=False,
        gpu_acquired=False,
    )
    native = Mock(return_value=readback)
    receipt = receipts.build_receipt(
        bindings, requirements, source_inputs, native, validity_seconds=30
    )
    path = tmp_path / "private-receipt.json"
    path.write_bytes(receipts.canonical(receipt))
    path.chmod(0o600)
    rights = Mock(return_value=None)
    provider = receipts.NativeArtifactReceiptProvider(
        path, file_hash(path), verify_current_rights=rights
    )
    selected = {"aos.bonsai.vision.v1": bindings["aos.bonsai.vision.v1"]}
    requested = {profile: requirements[profile] for profile in selected}
    return SimpleNamespace(**locals())


def test_receipt_brackets_native_operation_and_supports_distinct_interpreters(receipt_fixture):
    ctx = receipt_fixture
    ctx.native.assert_called_once_with()
    assert ctx.receipt["before_sha256"] == ctx.receipt["after_sha256"]
    assert ctx.receipt["snapshot"]["verification_interpreter"] == str(ctx.python)
    assert set(ctx.receipt["snapshot"]["interpreters"]) == {str(ctx.python), str(ctx.app_python)}
    ctx.provider(ctx.selected, ctx.requested)
    ctx.rights.assert_called_once_with(ctx.selected, ctx.requested)
    assert all(call.args == (str(ctx.python),) for call in ctx.dependency_read.call_args_list)


def test_per_call_does_not_rehash_weight_runtime_or_native_library_contents(
    receipt_fixture, monkeypatch
):
    ctx = receipt_fixture
    small_read = Mock(wraps=receipts._small_file)
    monkeypatch.setattr(receipts, "_small_file", small_read)
    ctx.provider(ctx.selected, ctx.requested)
    read_paths = {str(call.args[0]) for call in small_read.call_args_list}
    assert str(ctx.tmp_path / "native-library") not in read_paths
    for required in ctx.requirements.values():
        assert str(receipts.Path(required["artifact_roots"][0]) / "weights") not in read_paths
        assert str(receipts.Path(required["artifact_roots"][1]) / "runtime") not in read_paths


def test_snapshot_source_aggregate_bound_survives_scoped_reuse(receipt_fixture):
    ctx = receipt_fixture
    sources = {}
    checksum = hashlib.sha256(bytes(8 * 1024**2)).hexdigest()
    for index in range(4):
        path = ctx.tmp_path / f"bounded-source-{index}"
        with path.open("wb") as stream:
            stream.truncate(8 * 1024**2)
        sources[str(path)] = checksum
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        value = receipts.snapshot(ctx.requirements, sources)
        assert sum(item["identity"]["st_size"] for item in value["source_inputs"].values()) == (
            32 * 1024**2
        )
        overflow = ctx.tmp_path / "one-byte-over-aggregate"
        overflow.write_bytes(b"x")
        sources[str(overflow)] = file_hash(overflow)
        with pytest.raises(receipts.NativeArtifactReceiptError, match="bound"):
            receipts.snapshot(ctx.requirements, sources)


@pytest.mark.parametrize("change", ["weight", "extra-file", "library", "source", "interpreter"])
def test_identity_sets_source_or_executable_drift_denies(receipt_fixture, change):
    ctx = receipt_fixture
    paths = {
        "weight": ctx.tmp_path / "decider-model/weights",
        "library": ctx.tmp_path / "native-library",
        "source": ctx.source,
        "interpreter": ctx.python,
        "extra-file": ctx.tmp_path / "bonsai-vision-model/foreign",
    }
    paths[change].write_bytes(b"changed bytes")
    with pytest.raises(receipts.NativeArtifactReceiptError):
        ctx.provider(ctx.selected, ctx.requested)
    ctx.rights.assert_not_called()


def test_changed_native_dependency_version_environment_denies(receipt_fixture):
    ctx = receipt_fixture
    ctx.dependency_read.return_value = {"exact-native-package": "2.0"}
    with pytest.raises(receipts.NativeArtifactReceiptError, match="dependency"):
        ctx.provider(ctx.selected, ctx.requested)


@pytest.mark.parametrize("time", [99.0, 130.0, 131.0])
def test_stale_or_not_yet_verified_receipts_deny(receipt_fixture, time):
    ctx = receipt_fixture
    ctx.now[0] = time
    with pytest.raises(receipts.NativeArtifactReceiptError, match="stale"):
        ctx.provider(ctx.selected, ctx.requested)


def test_foreign_boot_or_binding_or_requirement_denies(receipt_fixture, monkeypatch):
    ctx = receipt_fixture
    changed = deepcopy(ctx.selected)
    changed["aos.bonsai.vision.v1"]["independent_policy_sha256"] = "b" * 64
    with pytest.raises(receipts.NativeArtifactReceiptError, match="reviewed"):
        ctx.provider(changed, ctx.requested)
    monkeypatch.setattr(receipts, "_boot", lambda: "foreign-boot")
    with pytest.raises(receipts.NativeArtifactReceiptError, match="boot"):
        ctx.provider(ctx.selected, ctx.requested)


def test_original_native_operation_cannot_change_artifact_identity(receipt_fixture):
    ctx = receipt_fixture

    def changing_gate():
        (ctx.tmp_path / "decider-model/weights").write_bytes(b"changed during native verification")
        return ctx.readback

    with pytest.raises(receipts.NativeArtifactReceiptError, match="across actual verification"):
        receipts.build_receipt(ctx.bindings, ctx.requirements, ctx.source_inputs, changing_gate)


def test_old_readback_without_native_snapshots_cannot_be_adopted(receipt_fixture):
    ctx = receipt_fixture
    ctx.path.write_bytes(receipts.canonical(ctx.readback))
    provider = receipts.NativeArtifactReceiptProvider(
        ctx.path, file_hash(ctx.path), verify_current_rights=ctx.rights
    )
    with pytest.raises(receipts.NativeArtifactReceiptError):
        provider(ctx.selected, ctx.requested)


def test_independent_receipt_hash_is_required(receipt_fixture):
    ctx = receipt_fixture
    provider = receipts.NativeArtifactReceiptProvider(
        ctx.path, "f" * 64, verify_current_rights=ctx.rights
    )
    with pytest.raises(receipts.NativeArtifactReceiptError, match="digest"):
        provider(ctx.selected, ctx.requested)


def test_missing_or_boolean_current_rights_is_denied(receipt_fixture):
    ctx = receipt_fixture
    with pytest.raises(TypeError):
        receipts.NativeArtifactReceiptProvider(ctx.path, file_hash(ctx.path))
    ctx.rights.return_value = True
    with pytest.raises(receipts.NativeArtifactReceiptError, match="complete or raise"):
        ctx.provider(ctx.selected, ctx.requested)


def test_current_rights_revocation_and_drift_during_callback_are_denied(receipt_fixture):
    ctx = receipt_fixture
    ctx.rights.side_effect = receipts.NativeArtifactReceiptError("current rights revoked")
    with pytest.raises(receipts.NativeArtifactReceiptError, match="revoked"):
        ctx.provider(ctx.selected, ctx.requested)

    def drift(*_arguments):
        (ctx.tmp_path / "native-library").write_bytes(b"changed during rights callback")

    ctx.rights.side_effect = drift
    with pytest.raises(receipts.NativeArtifactReceiptError):
        ctx.provider(ctx.selected, ctx.requested)


def test_declared_runtime_alias_is_bound_to_link_and_target_identity(receipt_fixture):
    ctx = receipt_fixture
    alias = ctx.tmp_path / "bonsai-vision-runtime/runtime-alias.so"
    entry = ctx.receipt["snapshot"]["profile_artifacts"]["aos.bonsai.vision.v1"]["trees"][1][
        "files"
    ][str(alias)]
    assert entry["target_path"] == str(alias.parent / "runtime")
    assert entry["identity"] != entry["target_identity"]
    alias.unlink()
    alias.symlink_to(ctx.source)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="alias"):
        ctx.provider(ctx.selected, ctx.requested)


def test_native_library_alias_cannot_replace_original_library(receipt_fixture):
    ctx = receipt_fixture
    library = ctx.tmp_path / "native-library"
    library.unlink()
    library.symlink_to(ctx.source)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="symbolic link"):
        ctx.provider(ctx.selected, ctx.requested)


def test_verified_file_reads_are_reused_only_inside_original_scope(tmp_path, monkeypatch):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"exact independently pinned source bytes")
    expected = file_hash(path)
    identity = path.stat()
    original_read = os.read
    reads = []

    def tracked_read(descriptor, length):
        data = original_read(descriptor, length)
        info = os.fstat(descriptor)
        if data and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino):
            reads.append(len(data))
        return data

    monkeypatch.setattr(receipts.os, "read", tracked_read)
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        first = receipts._small_file(path, expected)
        assert receipts._small_file(path, expected) == first
    assert reads == [len(first[0])]
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        assert receipts._small_file(path, expected) == first
    assert reads == [len(first[0]), len(first[0])]


def test_nested_artifact_scope_cannot_renew_original_deadline(tmp_path, monkeypatch):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"verified before the original deadline")
    expected = file_hash(path)
    now = [100.0]
    monkeypatch.setattr(receipts.time, "monotonic", lambda: now[0])
    with pytest.raises(receipts.NativeArtifactReceiptError, match="deadline"):
        with receipts.artifact_verification_scope(103.0):
            receipts._small_file(path, expected)
            now[0] = 102.0
            with receipts.artifact_verification_scope(105.0):
                now[0] = 103.0
                receipts._small_file(path, expected)


def test_nested_artifact_scope_keeps_original_cancellation_callback(tmp_path, monkeypatch):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"must not be read after original cancellation")
    expected = file_hash(path)
    cancelled = [False]

    def current():
        if cancelled[0]:
            raise receipts.NativeArtifactReceiptError("original owner cancelled")

    read = Mock(side_effect=AssertionError("cancelled nested scope performed a read"))
    monkeypatch.setattr(receipts.os, "read", read)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="original owner cancelled"):
        with receipts.artifact_verification_scope(
            receipts.time.monotonic() + 3, check_current=current
        ):
            with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
                cancelled[0] = True
                receipts._small_file(path, expected)
    read.assert_not_called()


@pytest.mark.parametrize("body_fails", [False, True])
def test_artifact_scope_attempts_all_owned_cleanup_and_preserves_original_error(body_fails):
    closed = []
    original = RuntimeError("original verification failed")

    def first():
        closed.append("first")
        raise OSError("owned child cleanup failed")

    def second():
        closed.append("second")

    expected = RuntimeError if body_fails else receipts.NativeArtifactReceiptError
    with pytest.raises(expected) as raised:
        with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
            operation = receipts._operation()
            operation.workers.update(
                first=SimpleNamespace(close=first), second=SimpleNamespace(close=second)
            )
            if body_fails:
                raise original
    assert closed == ["first", "second"]
    assert receipts._operation() is None
    if body_fails:
        assert raised.value is original
    else:
        assert "cleanup failed" in str(raised.value)


def test_artifact_scope_cannot_finish_successfully_after_cleanup_crosses_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(receipts.time, "monotonic", lambda: now[0])

    def close():
        now[0] = 103.0

    with pytest.raises(receipts.NativeArtifactReceiptError, match="deadline"):
        with receipts.artifact_verification_scope(103.0):
            receipts._operation().workers["owned"] = SimpleNamespace(close=close)
    assert receipts._operation() is None


def test_expired_artifact_scope_denies_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"must not be read")
    expected = file_hash(path)
    monkeypatch.setattr(receipts.time, "monotonic", lambda: 100.0)
    read = Mock(side_effect=AssertionError("expired scope performed a file read"))
    monkeypatch.setattr(receipts.os, "read", read)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="deadline"):
        with receipts.artifact_verification_scope(100.0):
            receipts._small_file(path, expected)
    read.assert_not_called()


@pytest.mark.parametrize(
    "change", ["source", "source-replaced", "interpreter-replaced", "extra-artifact", "library"]
)
def test_warmed_artifact_scope_still_denies_current_identity_drift(receipt_fixture, change):
    ctx = receipt_fixture
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        ctx.provider(ctx.selected, ctx.requested)
        ctx.rights.assert_called_once()
        if change in {"source-replaced", "interpreter-replaced"}:
            path = ctx.source if change == "source-replaced" else ctx.python
            replacement = path.with_name(path.name + ".replacement")
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(path.stat().st_mode & 0o777)
            os.replace(replacement, path)
        else:
            path = {
                "source": ctx.source,
                "extra-artifact": ctx.tmp_path / "bonsai-vision-model/unreviewed-file",
                "library": ctx.tmp_path / "native-library",
            }[change]
            path.write_bytes(b"changed after the first complete proof")
        with pytest.raises(receipts.NativeArtifactReceiptError):
            ctx.provider(ctx.selected, ctx.requested)
    # A cached successful attestation must not bypass the changed inputs.
    ctx.rights.assert_called_once()


def test_warmed_artifact_scope_rechecks_current_rights_on_every_call(receipt_fixture):
    ctx = receipt_fixture
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        ctx.provider(ctx.selected, ctx.requested)
        ctx.provider(ctx.selected, ctx.requested)
        assert ctx.rights.call_count == 2
        ctx.rights.side_effect = receipts.NativeArtifactReceiptError("current rights revoked")
        with pytest.raises(receipts.NativeArtifactReceiptError, match="revoked"):
            ctx.provider(ctx.selected, ctx.requested)
        assert ctx.rights.call_count == 3


def test_copied_artifact_scope_cannot_outlive_its_original_owner(tmp_path):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"original scope only")
    expected = file_hash(path)
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        receipts._small_file(path, expected)
        copied = copy_context()
    with pytest.raises(receipts.NativeArtifactReceiptError):
        copied.run(receipts._small_file, path, expected)


def test_copied_artifact_scope_cannot_cross_owner_thread(tmp_path):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"original owner thread only")
    expected = file_hash(path)
    failures = []

    def borrowed_read(context):
        try:
            context.run(receipts._small_file, path, expected)
        except BaseException as error:
            failures.append(error)

    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        receipts._small_file(path, expected)
        thread = threading.Thread(target=borrowed_read, args=(copy_context(),))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], receipts.NativeArtifactReceiptError)


def test_inherited_artifact_scope_cannot_cross_async_task(tmp_path):
    path = tmp_path / "pinned-input"
    path.write_bytes(b"original asyncio task only")
    expected = file_hash(path)

    async def borrowed_read():
        receipts._small_file(path, expected)

    async def run():
        with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
            receipts._small_file(path, expected)
            with pytest.raises(receipts.NativeArtifactReceiptError):
                await asyncio.create_task(borrowed_read())

    asyncio.run(run())


@pytest.fixture
def metadata_environment(tmp_path):
    """An isolated temporary interpreter with synthetic metadata, without pip."""
    root = tmp_path / "metadata-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    site = next((root / "lib").glob("python*/site-packages"))

    def distribution(name, version):
        directory = site / (name.replace("-", "_") + "-fixture.dist-info")
        directory.mkdir()
        metadata = directory / "METADATA"
        metadata.write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        return metadata

    first = distribution("Probe-Package", "1.0")
    return SimpleNamespace(
        root=root, site=site, python=root / "bin/python", first=first, distribution=distribution
    )


def track_metadata_workers(monkeypatch):
    original = receipts.subprocess.Popen
    workers = []

    def spawn(args, *positional, **options):
        process = original(args, *positional, **options)
        if receipts._DEPENDENCY_WORKER_CODE in args:
            workers.append(process)
        return process

    monkeypatch.setattr(receipts.subprocess, "Popen", spawn)
    return workers


def test_scoped_metadata_worker_rereads_versions_and_distribution_membership(
    metadata_environment, monkeypatch
):
    ctx = metadata_environment
    workers = track_metadata_workers(monkeypatch)
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        assert receipts._dependencies(str(ctx.python)) == {"probe-package": "1.0"}
        ctx.first.write_text("Metadata-Version: 2.1\nName: Probe-Package\nVersion: 2.0\n")
        assert receipts._dependencies(str(ctx.python)) == {"probe-package": "2.0"}
        ctx.distribution("New-Distribution", "3.0")
        assert receipts._dependencies(str(ctx.python)) == {
            "probe-package": "2.0",
            "new-distribution": "3.0",
        }
        ctx.first.unlink()
        ctx.first.parent.rmdir()
        assert receipts._dependencies(str(ctx.python)) == {"new-distribution": "3.0"}
        assert len(workers) == 1 and workers[0].poll() is None
    assert workers[0].poll() is not None


@pytest.mark.parametrize("outcome", ["success", "callback_error", "callback_expiry"])
def test_metadata_request_brackets_input_verification_and_denies_uncertain_retry(
    metadata_environment, monkeypatch, outcome
):
    ctx = metadata_environment
    now = [receipts.time.monotonic()]
    start = now[0]
    monkeypatch.setattr(receipts.time, "monotonic", lambda: now[0])
    workers = track_metadata_workers(monkeypatch)
    events = []
    original_write, original_read = os.write, os.read

    def write(descriptor, value):
        result = original_write(descriptor, value)
        if value == b"versions\n":
            events.append("request")
        return result

    def read(descriptor, length):
        result = original_read(descriptor, length)
        if workers and not workers[0].stdout.closed and descriptor == workers[0].stdout.fileno():
            events.append("response")
        return result

    monkeypatch.setattr(receipts.os, "write", write)
    monkeypatch.setattr(receipts.os, "read", read)
    source_error = ValueError("source verifier failed")

    def verify_inputs():
        assert events == ["request"]
        events.append("callback")
        if outcome == "callback_error":
            raise source_error
        if outcome == "callback_expiry":
            now[0] = start + 2.1  # The request's two seconds expire before the scope's three.

    with receipts.artifact_verification_scope(start + 3) as operation:
        if outcome == "success":
            assert receipts._dependencies(str(ctx.python), verify_inputs=verify_inputs) == {
                "probe-package": "1.0"
            }
            assert events == ["request", "callback", "response"]
            assert operation.workers[str(ctx.python)].pending is False
        else:
            expected = (
                ValueError if outcome == "callback_error" else receipts.NativeArtifactReceiptError
            )
            with pytest.raises(expected) as raised:
                receipts._dependencies(str(ctx.python), verify_inputs=verify_inputs)
            if outcome == "callback_error":
                assert raised.value is source_error
            else:
                assert "deadline" in str(raised.value)
            assert events == ["request", "callback"]
            assert operation.workers[str(ctx.python)].pending is True
            retry = Mock(side_effect=AssertionError("Uncertain request retried its verifier"))
            with pytest.raises(receipts.NativeArtifactReceiptError):
                receipts._dependencies(str(ctx.python), verify_inputs=retry)
            retry.assert_not_called()
            assert events == ["request", "callback"]
        assert len(workers) == 1
    assert workers[0].poll() is not None


@pytest.mark.parametrize(
    "change", ["pyvenv.cfg", "new-pth", "new-customization-package", "new-customization-bytecode"]
)
def test_scoped_metadata_worker_denies_changed_startup_configuration(
    metadata_environment, monkeypatch, change
):
    ctx = metadata_environment
    workers = track_metadata_workers(monkeypatch)
    with receipts.artifact_verification_scope(receipts.time.monotonic() + 3):
        assert receipts._dependencies(str(ctx.python)) == {"probe-package": "1.0"}
        if change == "pyvenv.cfg":
            path = ctx.root / "pyvenv.cfg"
            path.write_text(path.read_text() + "\n# changed after worker startup\n")
        elif change == "new-pth":
            (ctx.site / "new-search-path.pth").write_text("# startup configuration changed\n")
        elif change == "new-customization-package":
            package = ctx.site / "sitecustomize"
            package.mkdir()
            (package / "__init__.py").write_text("# new interpreter startup customization\n")
        else:
            (ctx.site / "sitecustomize.pyc").write_bytes(
                b"new startup file must be rejected before loading"
            )
        with pytest.raises(receipts.NativeArtifactReceiptError):
            receipts._dependencies(str(ctx.python))
    assert len(workers) == 1 and workers[0].poll() is not None


def test_scoped_metadata_worker_is_reaped_on_original_deadline(metadata_environment, monkeypatch):
    ctx = metadata_environment
    now = [receipts.time.monotonic()]
    deadline = now[0] + 3
    monkeypatch.setattr(receipts.time, "monotonic", lambda: now[0])
    workers = track_metadata_workers(monkeypatch)
    with pytest.raises(receipts.NativeArtifactReceiptError, match="deadline"):
        with receipts.artifact_verification_scope(deadline):
            assert receipts._dependencies(str(ctx.python)) == {"probe-package": "1.0"}
            assert len(workers) == 1 and workers[0].poll() is None
            now[0] = deadline
            receipts._dependencies(str(ctx.python))
    assert workers[0].poll() is not None
