"""Bounded validity receipts around the original native AOS artifact gates.

``build_receipt`` takes snapshots BEFORE and AFTER an explicitly supplied native
verification operation. An old native readback without these snapshots cannot
be adopted. The root host separately retains the canonical receipt's SHA-256.

``NativeArtifactReceiptProvider`` is the configured factory's artifact callback.
It checks current identities/file sets, small source/interpreter bytes and exact
native dependency VERSIONS, plus an explicit current-rights callback. Artifact
identity continuity is cached receipt validity, not constant byte surveillance.
Original native workers must still reverify artifact bytes before activation
inside existing scheduler ownership. No dependency package byte attestation,
model loading, GPU operation, service launch or database writing occurs here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import selectors
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path

SCHEMA = "aos-native-artifact-receipt.v1"
IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
    "st_mode",
    "st_uid",
    "st_nlink",
)
MAX_RECEIPT_BYTES = 512 * 1024
MAX_INTERPRETER_BYTES = 32 * 1024**2
MAX_FILES = 1024
MAX_VALIDITY_SECONDS = 900
_DEPENDENCY_PREAMBLE = r"""
import importlib.metadata as metadata,json
from email.parser import HeaderParser
from textwrap import dedent
header_parser=HeaderParser()
def _dependency_headers(distribution):
    if (type(distribution) is not metadata.PathDistribution
        or type(distribution).metadata is not metadata.Distribution.metadata
        or type(distribution).version is not metadata.Distribution.version):
        return distribution.metadata
    text=(distribution.read_text('METADATA') or distribution.read_text('PKG-INFO')
          or distribution.read_text(''))
    if not isinstance(text,str):return distribution.metadata
    # PathDistribution.read_text normalizes newlines. Retain the original header
    # parser/first-value behavior and Message's folded-header repair; the package
    # description body cannot contribute Name or Version. Read fresh every time.
    fields=header_parser.parsestr(text.partition('\n\n')[0])
    return {k:dedent(' '*8+v) if v and '\n' in v else v
            for k in ('Name','Version') for v in (fields[k],)}
def dependency(distribution):
    fields=_dependency_headers(distribution)
    version=(fields['Version'] if type(distribution).version is metadata.Distribution.version
             else distribution.version)
    return fields['Name'].lower().replace('_','-'),version
"""
_DEPENDENCY_CODE = _DEPENDENCY_PREAMBLE + (
    "print(json.dumps(dict(dependency(d) for d in metadata.distributions()),"
    "sort_keys=True,separators=(',',':')))"
)
_DEPENDENCY_WORKER_CODE = (
    _DEPENDENCY_PREAMBLE
    + r"""
import importlib,os,select,sys,time
from pathlib import Path
deadline=float(sys.argv[1])
def stamp(path):
    p=Path(path)
    if not os.path.lexists(p):return None
    fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_mode','st_uid','st_nlink')
    link=p.lstat();target=p.resolve(strict=True);info=target.stat()
    return (tuple(getattr(link,k) for k in fields),str(target),
            tuple(getattr(info,k) for k in fields))
def configuration():
    roots=[];files={}
    for value in sys.path:
        p=Path(value)
        if not p.exists():roots.append((value,None));continue
        resolved=p.resolve(strict=True);info=resolved.stat()
        roots.append((value,str(resolved),info.st_dev,info.st_ino,info.st_mode,info.st_uid))
        if p.is_dir():
            for name in sorted(os.listdir(p)):
                if (name.endswith('.pth')
                    or name.partition('.')[0] in ('sitecustomize','usercustomize')):
                    files[str(p/name)]=stamp(p/name)
        else:files[str(p)]=stamp(p)
    files[str(Path(sys.prefix)/'pyvenv.cfg')]=stamp(Path(sys.prefix)/'pyvenv.cfg')
    files[sys.executable]=stamp(sys.executable)
    return (tuple(sys.path),sys.prefix,sys.base_prefix,roots,files)
def modules():
    paths={getattr(module,'__file__',None) for module in tuple(sys.modules.values())}
    return {p:stamp(p) for p in paths if isinstance(p,str) and os.path.isfile(p)}
initial=configuration();loaded=None
while time.monotonic()<deadline:
    readable,_,_=select.select([sys.stdin],[],[],max(0,deadline-time.monotonic()))
    if not readable:break
    if sys.stdin.readline(16)!='versions\n':break
    if configuration()!=initial or (loaded is not None and modules()!=loaded):raise SystemExit(3)
    importlib.invalidate_caches()
    versions=dict(dependency(d) for d in metadata.distributions())
    if configuration()!=initial or time.monotonic()>=deadline:raise SystemExit(3)
    current=modules()
    if loaded is None:loaded=current
    elif current!=loaded:raise SystemExit(3)
    print(json.dumps(versions,sort_keys=True,separators=(',',':')),flush=True)
"""
)

_verification_memo = ContextVar("native_artifact_operation_memo", default=None)


def _owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


class _Operation:
    def __init__(self, deadline, check_current):
        self.deadline, self.check_current = deadline, check_current
        self.owner, self.closed = _owner(), False
        self.files, self.workers, self.bytes = {}, {}, 0

    def check(self):
        _require(
            not self.closed and self.owner == _owner(), "Artifact scope owner or lifetime changed"
        )
        _require(time.monotonic() < self.deadline, "Original artifact operation deadline expired")
        if self.check_current is not None:
            self.check_current()


def _operation():
    operation = _verification_memo.get()
    if operation is not None:
        operation.check()
    return operation


@contextmanager
def artifact_verification_scope(deadline, *, check_current=None):
    """Share verified bytes and a fresh-read process within one bounded operation."""
    _require(
        type(deadline) in (int, float) and math.isfinite(deadline),
        "Finite artifact deadline required",
    )
    previous = _operation()
    if previous is not None:
        deadline = min(deadline, previous.deadline)
        current = check_current

        def check_current():
            previous.check()
            if current is not None:
                current()

    operation = _Operation(deadline, check_current)
    operation.check()
    token = _verification_memo.set(operation)
    original_error = None
    try:
        yield operation
        operation.check()
    except BaseException as error:
        original_error = error
        raise
    finally:
        operation.closed = True
        failures = []
        try:
            for worker in operation.workers.values():
                try:
                    worker.close()
                except BaseException as error:
                    failures.append(error)
        finally:
            _verification_memo.reset(token)
        if failures:
            if original_error is not None:
                original_error.add_note("Owned dependency worker cleanup failed")
            else:
                raise NativeArtifactReceiptError(
                    "Owned dependency worker cleanup failed"
                ) from failures[0]
        if original_error is None:
            _require(
                time.monotonic() < deadline, "Original artifact deadline expired during cleanup"
            )
            if check_current is not None:
                check_current()


class NativeArtifactReceiptError(ValueError):
    """Receipt freshness, native artifact identity or current rights were denied."""


def _require(condition, message):
    if not condition:
        raise NativeArtifactReceiptError(message)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _clock():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _boot():
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _absolute(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "Explicit absolute paths are required")
    return path


def _hash(value):
    _require(
        type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None,
        "Independent SHA-256 is required",
    )
    return value


def _identity(path, *, directory=False):
    path = _absolute(path)
    _require(path.resolve(strict=True) == path, "Artifact/source path contains a symbolic link")
    info = path.lstat()
    _require(
        (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        and info.st_uid in (0, os.getuid())
        and not stat.S_IMODE(info.st_mode) & 0o022
        and (directory or info.st_nlink == 1),
        "Artifact/source type, owner or links differ",
    )
    return {name: getattr(info, name) for name in IDENTITY_FIELDS}


def _small_file(path, expected=None, *, limit=8 * 1024**2):
    operation = _operation()
    path = _absolute(path)
    before = _identity(path)
    _require(0 <= before["st_size"] <= limit, "Small verified input exceeds byte bound")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        _require(
            {name: getattr(info, name) for name in IDENTITY_FIELDS} == before,
            "Verified input was replaced before descriptor read",
        )
        cached = operation.files.get(str(path)) if operation is not None else None
        if cached is not None:
            content, proof = cached
            _require(proof["identity"] == before, "Verified operation input identity changed")
            after = os.fstat(descriptor)
            _require(
                {name: getattr(after, name) for name in IDENTITY_FIELDS} == before
                and _identity(path) == before,
                "Verified operation input changed during reuse",
            )
            _require(
                expected is None or proof["sha256"] == _hash(expected),
                "Verified small input digest differs",
            )
            operation.check()
            return content, deepcopy(proof)
        content = bytearray()
        while len(content) <= limit:
            if operation is not None:
                operation.check()
            block = os.read(descriptor, min(65536, limit + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        after = os.fstat(descriptor)
        _require(
            len(content) == before["st_size"]
            and {name: getattr(after, name) for name in IDENTITY_FIELDS} == before
            and _identity(path) == before,
            "Verified input changed during read",
        )
    finally:
        os.close(descriptor)
    checksum = hashlib.sha256(content).hexdigest()
    _require(expected is None or checksum == _hash(expected), "Verified small input digest differs")
    value, proof = bytes(content), {"identity": before, "sha256": checksum}
    if operation is not None:
        operation.check()
        if len(operation.files) < MAX_FILES and operation.bytes + len(value) <= 64 * 1024**2:
            operation.files[str(path)] = value, proof
            operation.bytes += len(value)
    return value, deepcopy(proof)


def _plain_bindings(bindings):
    _require(
        type(bindings) is dict and bindings, "Full independently reviewed bindings are required"
    )
    return {
        name: value.model_dump(mode="json") if hasattr(value, "model_dump") else deepcopy(value)
        for name, value in bindings.items()
    }


def _tree(root, expected):
    root = _absolute(root)
    _require(
        type(expected) is dict and 1 <= len(expected) <= MAX_FILES,
        "Exact native manifest artifact maps are required",
    )
    root_identity = _identity(root, directory=True)
    actual, files = set(), {}
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders:
            _identity(Path(directory) / name, directory=True)
        for name in names:
            path = Path(directory) / name
            linked = path.lstat()
            if stat.S_ISLNK(linked.st_mode):
                target = path.resolve(strict=True)
                _require(
                    target.is_relative_to(root)
                    and target.relative_to(root).as_posix() in expected
                    and linked.st_uid in (0, os.getuid())
                    and linked.st_nlink == 1,
                    "Native artifact alias escapes its declared pinned file set",
                )
                identity = {field: getattr(linked, field) for field in IDENTITY_FIELDS}
                target_identity = _identity(target)
            else:
                identity, target, target_identity = _identity(path), None, None
            # These are the original native verification tree exclusions.
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            _require(
                relative in expected and len(actual) <= MAX_FILES,
                "Native manifest artifact file set differs",
            )
            files[str(path)] = {"sha256": _hash(expected[relative]), "identity": identity}
            if target is not None:
                _require(
                    expected[relative] == expected[target.relative_to(root).as_posix()],
                    "Native artifact alias and declared target digests differ",
                )
                files[str(path)].update(target_path=str(target), target_identity=target_identity)
    _require(
        actual == set(expected) and _identity(root, directory=True) == root_identity,
        "Native manifest artifact tree changed or file set differs",
    )
    return {
        "root": str(root),
        "identity": root_identity,
        "file_set": sorted(actual),
        "files": files,
    }


def _interpreter(path):
    configured = _absolute(path)
    link_info = configured.lstat()
    _require(
        stat.S_ISREG(link_info.st_mode) or stat.S_ISLNK(link_info.st_mode),
        "Actual configured interpreter type differs",
    )
    target = configured.resolve(strict=True)
    _require(os.access(configured, os.X_OK), "Actual configured interpreter is not executable")
    # The installed uv CPython executable is about30MiB; source/config inputs
    # keep their8MiB bound. Exact executable identity and digest remain checked.
    _, current = _small_file(target, limit=MAX_INTERPRETER_BYTES)
    _require(current["identity"]["st_size"] > 0, "Actual interpreter target is empty")
    return {
        "configured_path": str(configured),
        "resolved_target": str(target),
        "configured_identity": {name: getattr(link_info, name) for name in IDENTITY_FIELDS},
        **current,
    }


class _DependencyWorker:
    """One owned interpreter; every request still discovers and reads current versions."""

    def __init__(self, interpreter, operation):
        operation.check()
        self.operation = operation
        self.process = subprocess.Popen(  # nosec B603 -- reviewed interpreter and fixed read-only code
            [interpreter, "-B", "-I", "-c", _DEPENDENCY_WORKER_CODE, str(operation.deadline)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={"PATH": "/usr/bin:/bin", "CUDA_VISIBLE_DEVICES": ""},
        )
        self.closed = False
        self.pending = False
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except BaseException:
            self.close()
            raise

    def read(self, *, verify_inputs=None):
        operation, process = self.operation, self.process
        operation.check()
        _require(
            not self.closed and not self.pending and process.poll() is None,
            "Owned dependency probe exited or has an unfinished request",
        )
        end = min(operation.deadline, time.monotonic() + 2)
        self.pending = True
        frame = b"versions\n"
        _require(
            os.write(process.stdin.fileno(), frame) == len(frame),
            "Dependency request write was incomplete",
        )
        # The worker reads fresh versions while this same snapshot observes its
        # sources. Sending and collecting share the original two-second budget.
        if verify_inputs is not None:
            verify_inputs()
        body = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not body.endswith(b"\n"):
                operation.check()
                remaining = end - time.monotonic()
                _require(remaining > 0, "Original dependency probe deadline expired")
                if not selector.select(timeout=min(0.02, remaining)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(8192, 65537 - len(body)))
                _require(
                    chunk and len(body) + len(chunk) <= 65536,
                    "Dependency probe exited or exceeded its response bound",
                )
                body.extend(chunk)
        operation.check()
        _require(
            body.count(b"\n") == 1 and time.monotonic() < end,
            "Dependency probe response or deadline differs",
        )
        value = json.loads(body)
        self.pending = False
        return value

    def close(self):
        if self.closed:
            return
        self.closed = True
        # Popen owns this unreaped child. Never signal a discovered PID or another unit.
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.communicate(timeout=0.25)
        finally:
            self.process.stdin.close()
            self.process.stdout.close()


def _dependencies(interpreter, *, verify_inputs=None):
    operation = _operation()
    if operation is not None:
        if interpreter not in operation.workers:
            operation.workers[interpreter] = _DependencyWorker(interpreter, operation)
        value = operation.workers[interpreter].read(verify_inputs=verify_inputs)
    else:
        if verify_inputs is not None:
            verify_inputs()
        value = _dependency_once(interpreter)
    _require(
        type(value) is dict
        and value
        and all(type(k) is str and type(v) is str for k, v in value.items()),
        "Native dependency version map is malformed",
    )
    return value


def _dependency_once(interpreter):
    result = subprocess.run(
        [interpreter, "-B", "-I", "-c", _DEPENDENCY_CODE],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
        env={"PATH": "/usr/bin:/bin", "CUDA_VISIBLE_DEVICES": ""},
    )
    _require(
        result.returncode == 0 and len(result.stdout.encode()) <= 65536,
        "Exact native dependency version readback failed",
    )
    return json.loads(result.stdout)


def snapshot(requirements, source_inputs):
    """Read artifact metadata, small reviewed inputs and native dependency versions."""
    _require(
        type(requirements) is dict
        and requirements
        and len(requirements) <= 3
        and type(source_inputs) is dict
        and 1 <= len(source_inputs) <= 256,
        "Explicit profile requirements and source inputs are required",
    )
    profiles, interpreters, dependency_expected = {}, {}, None
    verification_interpreter = None
    for profile, required in requirements.items():
        manifest, entry = required["manifest"], required["profile"]
        manifest_path = _absolute(required["manifest_path"])
        raw, manifest_file = _small_file(manifest_path, entry["manifest_sha256"], limit=256 * 1024)
        _require(json.loads(raw) == manifest, "Receipt manifest differs from explicit requirements")
        kind = entry["kind"]
        pairs = [
            ("model_path", "model_files"),
            ("code_path", "code_files") if kind == "decider" else ("runtime_path", "runtime_files"),
        ]
        trees = [_tree(manifest[root], manifest[files]) for root, files in pairs]
        _require(
            [tree["root"] for tree in trees] == required["artifact_roots"],
            "Receipt native roots differ from explicit configured requirements",
        )
        libraries = {}
        if kind != "decider":
            _require(
                type(manifest.get("native_libraries")) is dict and manifest["native_libraries"],
                "Original Bonsai native library map is required",
            )
            libraries = {
                str(_absolute(path)): {"sha256": _hash(checksum), "identity": _identity(path)}
                for path, checksum in manifest["native_libraries"].items()
            }
        else:
            dependency_expected = manifest["dependencies"]
        interpreter = _interpreter(required["interpreter"])
        _require(
            interpreter["resolved_target"] == required["interpreter_target"],
            "Actual interpreter target differs from configured requirements",
        )
        interpreters[interpreter["configured_path"]] = interpreter
        if kind == "decider":
            verification_interpreter = interpreter["configured_path"]
        profiles[profile] = {
            "manifest": manifest_file,
            "trees": trees,
            "native_libraries": libraries,
        }
    _require(
        verification_interpreter in interpreters
        and type(dependency_expected) is dict
        and dependency_expected,
        "Actual Decider verification interpreter and exact dependency map are required",
    )
    inputs = {}

    def verify_inputs():
        consumed = 0
        for path, checksum in source_inputs.items():
            # _small_file checks path/FD identity before reading and again after;
            # its size check also enforces the remaining aggregate budget.
            proof = _small_file(path, checksum, limit=min(8 * 1024**2, 32 * 1024**2 - consumed))[1]
            consumed += proof["identity"]["st_size"]
            inputs[str(_absolute(path))] = proof

    dependencies = _dependencies(verification_interpreter, verify_inputs=verify_inputs)
    _require(dependencies == dependency_expected, "Exact native dependency environment changed")
    _require(
        sum(value["identity"]["st_size"] for value in inputs.values()) <= 32 * 1024**2,
        "Reviewed source inputs exceed native configured-source aggregate bound",
    )
    return {
        "profile_artifacts": profiles,
        "interpreters": interpreters,
        "verification_interpreter": verification_interpreter,
        "dependency_versions": dependencies,
        "source_inputs": inputs,
    }


def build_receipt(
    reviewed_bindings, requirements, source_inputs, verify_native, *, validity_seconds=300
):
    """Bracket an actual original native verification operation; never adopt an old readback."""
    _require(callable(verify_native), "Actual native verification operation is required")
    _require(
        type(validity_seconds) is int and 1 <= validity_seconds <= MAX_VALIDITY_SECONDS,
        "Native receipt validity must be explicitly bounded",
    )
    bindings = _plain_bindings(reviewed_bindings)
    _require(
        set(bindings) == set(requirements), "Receipt profiles and reviewed full bindings differ"
    )
    boot, started = _boot(), _clock()
    before = snapshot(requirements, source_inputs)
    readback = verify_native()
    _require(
        type(readback) is dict
        and readback.get("schema") == "aos-model-environment-readback.v1"
        and readback.get("artifact_bytes_verified") is True
        and readback.get("dependency_versions_verified") is True
        and readback.get("source_and_manifests_unchanged") is True
        and readback.get("model_instantiated") is False
        and readback.get("gpu_acquired") is False,
        "Original native artifact/version verification did not complete",
    )
    after = snapshot(requirements, source_inputs)
    verified = _clock()
    _require(
        before == after and _boot() == boot and verified >= started,
        "Native identities, source or dependencies changed across actual verification",
    )
    _require(
        readback.get("interpreter") == before["verification_interpreter"],
        "Native verification used another interpreter",
    )
    return {
        "schema": SCHEMA,
        "boot_id": boot,
        "started_boottime": started,
        "verified_boottime": verified,
        "expires_boottime": verified + validity_seconds,
        "reviewed_bindings": bindings,
        "requirements": deepcopy(requirements),
        "requirements_sha256": digest(requirements),
        "before_sha256": digest(before),
        "after_sha256": digest(after),
        "snapshot": after,
        "native_readback_sha256": digest(readback),
        "native_readback": readback,
    }


class NativeArtifactReceiptProvider:
    """Configured factory callback using independently hash-bound, current native receipts."""

    def __init__(self, receipt_path, expected_receipt_sha256, *, verify_current_rights=None):
        if not callable(verify_current_rights):
            raise TypeError("Explicit current policy/runtime rights verifier is required")
        self._path, self._hash = _absolute(receipt_path), _hash(expected_receipt_sha256)
        self._rights = verify_current_rights

    def __call__(self, selected_bindings, requirements):
        try:
            started = time.monotonic()
            raw, _identity_receipt = _small_file(self._path, self._hash, limit=MAX_RECEIPT_BYTES)
            receipt = json.loads(raw)
            selected = _plain_bindings(selected_bindings)
            now = _clock()
            _require(
                receipt.get("schema") == SCHEMA
                and receipt.get("boot_id") == _boot()
                and all(
                    type(receipt.get(key)) in (int, float) and math.isfinite(receipt[key])
                    for key in ("started_boottime", "verified_boottime", "expires_boottime")
                )
                and 0
                <= receipt["started_boottime"]
                <= receipt["verified_boottime"]
                <= now
                < receipt["expires_boottime"]
                <= receipt["verified_boottime"] + MAX_VALIDITY_SECONDS,
                "Native artifact receipt is stale, from another boot or not yet verified",
            )
            _require(
                type(requirements) is dict
                and set(requirements) == set(selected)
                and set(selected) <= set(receipt["reviewed_bindings"])
                and all(
                    receipt["reviewed_bindings"][profile] == value
                    and receipt["requirements"][profile] == requirements[profile]
                    for profile, value in selected.items()
                )
                and receipt["requirements_sha256"] == digest(receipt["requirements"])
                and receipt["before_sha256"]
                == receipt["after_sha256"]
                == digest(receipt["snapshot"])
                and receipt["native_readback_sha256"] == digest(receipt["native_readback"]),
                "Native receipt does not bind the exact independently reviewed requirements",
            )
            source_inputs = {
                path: value["sha256"]
                for path, value in receipt["snapshot"]["source_inputs"].items()
            }
            # Full receipt requirements include Decider's native dependency map,
            # even when one current call selects only a Bonsai profile.
            _require(
                snapshot(receipt["requirements"], source_inputs) == receipt["snapshot"],
                "Native artifact identity, exact file set, source or dependency versions drifted",
            )
            _require(
                self._rights(deepcopy(selected_bindings), deepcopy(requirements)) is None,
                "Current policy/runtime rights verifier must complete or raise",
            )
            _require(
                snapshot(receipt["requirements"], source_inputs) == receipt["snapshot"]
                and _clock() < receipt["expires_boottime"]
                and _boot() == receipt["boot_id"]
                and time.monotonic() - started < 4,
                "Native receipt or rights verification lost current identities or bounded deadline",
            )
        except NativeArtifactReceiptError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            subprocess.SubprocessError,
        ) as error:
            raise NativeArtifactReceiptError(
                "Native artifact receipt current readback was denied"
            ) from error
