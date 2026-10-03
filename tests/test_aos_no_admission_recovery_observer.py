"""CPU-only witness CLI checks; fake providers and clocks grant no authority."""

import json
import stat
from types import SimpleNamespace

import pytest

from lab.llm.aos_gpu_control_store import control_deadline
from lab.llm.aos_no_admission_observation import canonical
from scripts import aos_no_admission_recovery_observer as cli
from tests.test_aos_no_admission_recovery import fixture as recovery_fixture


@pytest.fixture(name="witness")
def fixture_witness(monkeypatch, tmp_path):
    """A private fixture directory, static bytes and an independently fake clock."""
    tmp_path.chmod(0o700)
    context, old_raw, _expectations = recovery_fixture()
    context["freshness"].update(issued_boottime_us=100_000_000, expires_boottime_us=100_250_000)
    raw = canonical(context)
    clock = SimpleNamespace(monotonic=100.0, boottime=100.0)
    seen = {"inspect": 0, "context": 0, "reads": [], "deadlines": [], "sleeps": []}

    def inspect():
        seen["inspect"] += 1
        return {"fixture": True, "runtime_authorized": False}

    def current_context():
        seen["context"] += 1
        return raw

    def read_closure(proof):
        seen["reads"].append(proof)
        seen["deadlines"].append(control_deadline.get())
        return False

    def sleep(seconds):
        seen["sleeps"].append(seconds)
        clock.monotonic += seconds
        clock.boottime += seconds

    providers = SimpleNamespace(inspect=inspect, context=current_context, read_closure=read_closure)
    monkeypatch.setattr(cli, "_providers", lambda *_args: providers)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.monotonic,
            clock_gettime=lambda _kind: clock.boottime,
            CLOCK_BOOTTIME=7,
            sleep=sleep,
        ),
    )
    original = tmp_path / "retained-observation.private.json"
    original.write_bytes(old_raw)
    config = tmp_path / "fixture-config"
    output = tmp_path / "context.private.json"
    previous = control_deadline.set(None)
    try:
        yield SimpleNamespace(
            providers=providers,
            raw=raw,
            clock=clock,
            seen=seen,
            original=original,
            original_raw=old_raw,
            config=config,
            output=output,
        )
    finally:
        control_deadline.reset(previous)


def execute(witness, *, enabled=True, output=True):
    return cli.execute(
        witness.config,
        "1" * 64,
        witness=enabled,
        output=witness.output if output else None,
    )


def test_default_inspection_creates_no_context_or_authority(witness):
    result = execute(witness, enabled=False, output=False)
    assert result["mode"] == "inspect"
    assert result["runtime_authorized"] is False
    assert witness.seen["inspect"] == 1
    assert witness.seen["context"] == 0
    assert not witness.seen["reads"]
    assert not witness.output.exists()
    assert witness.original.read_bytes() == witness.original_raw
    assert control_deadline.get() is None


@pytest.mark.parametrize("enabled,output", [(False, True), (True, False)])
def test_invalid_output_mode_cannot_request_authority(witness, enabled, output):
    with pytest.raises(ValueError):
        execute(witness, enabled=enabled, output=output)
    assert witness.seen["context"] == 0
    assert not witness.output.exists()
    assert control_deadline.get() is None


def test_context_denial_precedes_file_creation_and_restores_prior_deadline(witness):
    def deny():
        assert control_deadline.get() == 101.0
        raise RuntimeError("private authority denied")

    witness.providers.context = deny
    token = control_deadline.set(101.0)
    try:
        with pytest.raises(RuntimeError):
            execute(witness)
        assert control_deadline.get() == 101.0
    finally:
        control_deadline.reset(token)
    assert not witness.output.exists()
    assert not witness.seen["reads"]


def test_exclusive_output_cannot_overwrite_retained_proof(witness):
    original_identity = witness.original.stat()
    witness.output = witness.original
    with pytest.raises(FileExistsError):
        execute(witness)
    assert witness.original.read_bytes() == witness.original_raw
    assert witness.original.stat().st_ino == original_identity.st_ino
    assert not witness.seen["reads"]
    assert control_deadline.get() is None


@pytest.mark.parametrize("kind", ["file_symlink", "parent_symlink", "public_parent"])
def test_output_requires_private_unaliased_location(witness, tmp_path, kind):
    if kind == "file_symlink":
        witness.output.symlink_to(witness.original)
    elif kind == "parent_symlink":
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        alias = tmp_path / "alias"
        alias.symlink_to(private, target_is_directory=True)
        witness.output = alias / "context"
    else:
        tmp_path.chmod(0o755)
    with pytest.raises((OSError, ValueError)):
        execute(witness)
    assert witness.original.read_bytes() == witness.original_raw
    assert not witness.seen["reads"]


def test_fixed_context_expires_without_renewing_or_rewriting_old_proof(witness):
    result = execute(witness)
    assert result["aos_closure_confirmed"] is False
    assert witness.seen["context"] == 1
    assert witness.seen["reads"] == [witness.raw] * 3
    assert witness.seen["deadlines"] == [100.25] * 3
    assert witness.output.read_bytes() == witness.raw
    assert stat.S_IMODE(witness.output.stat().st_mode) == 0o600
    assert witness.original.read_bytes() == witness.original_raw
    assert control_deadline.get() is None
    for key in (
        "observation_minted",
        "observation_reissued",
        "original_deadline_renewed",
        "gpu_release_proven",
    ):
        assert result[key] is False


def test_shorter_parent_deadline_limits_wait_and_is_restored(witness):
    token = control_deadline.set(100.05)
    try:
        assert execute(witness)["aos_closure_confirmed"] is False
        assert witness.seen["reads"] == [witness.raw]
        assert witness.seen["deadlines"] == [100.05]
        assert control_deadline.get() == 100.05
    finally:
        control_deadline.reset(token)


def test_closure_receives_exact_published_context_bytes(witness):
    def closed(raw):
        assert raw == witness.raw == witness.output.read_bytes()
        assert witness.original.read_bytes() == witness.original_raw
        return True

    witness.providers.read_closure = closed
    assert execute(witness)["aos_closure_confirmed"] is True
    assert witness.seen["context"] == 1
    assert not witness.seen["sleeps"]
    assert control_deadline.get() is None


@pytest.mark.parametrize("clock", ["monotonic", "boottime"])
def test_closure_finishing_after_deadline_cannot_confirm(witness, clock):
    def too_late(_raw):
        setattr(witness.clock, clock, 101.0)
        return True

    witness.providers.read_closure = too_late
    assert execute(witness)["aos_closure_confirmed"] is False
    assert witness.seen["context"] == 1


def test_closure_error_restores_nested_deadlines_and_preserves_outputs(witness):
    def denied(_raw):
        raise RuntimeError("private conflicting closure")

    witness.providers.read_closure = denied
    token = control_deadline.set(102.0)
    try:
        with pytest.raises(RuntimeError):
            execute(witness)
        assert control_deadline.get() == 102.0
    finally:
        control_deadline.reset(token)
    assert witness.output.read_bytes() == witness.raw
    assert witness.original.read_bytes() == witness.original_raw


def arguments(monkeypatch, witness):
    monkeypatch.setattr(
        "sys.argv",
        [
            "recovery-observer",
            "--config",
            str(witness.config),
            "--expected-config-sha256",
            "1" * 64,
            "--witness",
            "--output",
            str(witness.output),
        ],
    )


def test_main_timeout_exit3_without_reissue(monkeypatch, capsys, witness):
    arguments(monkeypatch, witness)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["aos_closure_confirmed"] is False
    assert result["observation_reissued"] is False
    assert witness.seen["context"] == 1


@pytest.mark.parametrize(
    "error", [OSError, ValueError, RuntimeError, KeyError, TypeError, AssertionError]
)
def test_main_redacts_unknown_provider_exceptions(monkeypatch, capsys, witness, error):
    arguments(monkeypatch, witness)

    def deny():
        raise error("PRIVATE_SECRET_AND_PATH")

    witness.providers.context = deny
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err
    assert json.loads(captured.out) == {
        "result": "denied_or_uncertain",
        "observation_reissued": False,
    }
    assert not witness.output.exists()
