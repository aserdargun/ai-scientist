"""CPU-only CLI boundary checks; no real proof, authority or host DB access."""

import json
import os
from types import SimpleNamespace

import pytest

from lab.llm.aos_no_admission_providers import ProviderDenied
from scripts import aos_no_admission_observer as cli


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ProviderDenied("canonical_inventory_unknown"), "canonical_inventory_unknown"),
        (ProviderDenied("private/request/path and secret"), "unclassified"),
        (RuntimeError("canonical_inventory_unknown"), "unclassified"),
    ],
)
def test_cli_failure_exposes_only_allowlisted_provider_codes(monkeypatch, capsys, error, code):
    monkeypatch.setattr(
        "sys.argv", ["observer", "--config", "/unused", "--expected-config-sha256", "1" * 64]
    )

    def deny(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli, "execute", deny)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "result": "denied_or_uncertain",
        "diagnostic_code": code,
        "reissue_permitted": False,
    }


def provider(monkeypatch, *, denied=False):
    events = []

    def authority(_target):
        events.append("authority")
        if denied:
            raise RuntimeError("not an authenticated live observer")

    p = SimpleNamespace(
        inspect=lambda: {"runtime_authorized": False},
        target={"request_id": "a" * 32},
        observer_authority=authority,
        original_reader=lambda _target: b"original-fixture",
        physical_reader=lambda _target, _original: events.append("physical"),
        canonical_database="never-open-this-fixture.sqlite",
        read_closure=lambda _checksum: events.append("closure") or True,
    )
    monkeypatch.setattr(cli, "_providers", lambda *_args: p)
    monkeypatch.setattr(cli, "ControlStore", lambda _path: events.append("store") or object())
    return p, events


def test_default_inspection_has_no_database_or_proof_mutation(monkeypatch, tmp_path):
    _, events = provider(monkeypatch)
    result = cli.execute(tmp_path / "config", "1" * 64, issue=False, output=None)
    assert result["runtime_authorized"] is False
    assert not events
    assert not list(tmp_path.iterdir())


def test_denied_live_authority_precedes_output_and_store_construction(monkeypatch, tmp_path):
    _, events = provider(monkeypatch, denied=True)
    with pytest.raises(RuntimeError):
        cli.execute(tmp_path / "config", "1" * 64, issue=True, output=tmp_path / "proof")
    assert events == ["authority"]
    assert not (tmp_path / "proof").exists()


def test_existing_proof_is_preserved_before_store_construction(monkeypatch, tmp_path):
    _, events = provider(monkeypatch)
    tmp_path.chmod(0o700)
    path = tmp_path / "proof"
    path.write_bytes(b"immutable-old-proof")
    with pytest.raises(FileExistsError):
        cli.execute(tmp_path / "config", "1" * 64, issue=True, output=path)
    assert path.read_bytes() == b"immutable-old-proof"
    assert events == ["authority", "physical"]


@pytest.mark.parametrize("destination", ["symlink", "public"])
def test_unsafe_private_output_is_rejected(monkeypatch, tmp_path, destination):
    _, events = provider(monkeypatch)
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    if destination == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(parent, target_is_directory=True)
        parent = link
    else:
        parent.chmod(0o755)
    with pytest.raises(ValueError):
        cli.execute(tmp_path / "config", "1" * 64, issue=True, output=parent / "proof")
    assert "store" not in events


@pytest.mark.parametrize("confirmed", [True, False])
def test_issuer_waits_for_exact_closure_without_reissuing(monkeypatch, tmp_path, confirmed):
    p, events = provider(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli.time, "clock_gettime", lambda _kind: clock[0])
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: clock.__setitem__(0, 101.0))
    proof = {
        "observation_sha256": "b" * 64,
        "freshness": {"expires_boottime_us": 100_500_000},
    }

    def observe(_target, **_kwargs):
        events.append("observe")
        return proof

    monkeypatch.setattr(
        cli,
        "NoAdmissionObservationStore",
        lambda *_args, **_kwargs: SimpleNamespace(observe=observe),
    )
    seen = []
    p.read_closure = lambda checksum: seen.append(checksum) or confirmed
    tmp_path.chmod(0o700)
    path = tmp_path / "proof"
    result = cli.execute(tmp_path / "config", "1" * 64, issue=True, output=path)
    assert result["observation_committed"] is True
    assert result["aos_closure_confirmed"] is confirmed
    assert result["gpu_release_proven"] is result["reissue_permitted"] is False
    assert events.count("observe") == 1
    assert seen == [proof["observation_sha256"]]
    assert json.loads(path.read_bytes()) == proof
    assert os.stat(path).st_mode & 0o777 == 0o600
