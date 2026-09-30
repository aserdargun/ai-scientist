"""CPU-only safety checks for the unexecuted 0.30 lifecycle review driver."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

_DRIVER = (
    Path(__file__).parents[1]
    / "docs/ai-scientist/review-evidence/holdout-030-lifecycle-review.py"
)
_SPEC = importlib.util.spec_from_file_location("holdout_030_lifecycle_review", _DRIVER)
assert _SPEC is not None and _SPEC.loader is not None
review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(review)


def _receipt_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(review, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "data/runtime/holdout-lifecycle-receipts"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    return root


def test_receipt_preflight_rejects_existing_destination_without_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _receipt_root(monkeypatch, tmp_path)
    existing = root / "existing.json"
    existing.write_text("keep", encoding="ascii")

    with pytest.raises(FileExistsError):
        review._reserve_receipt(str(existing))

    assert existing.read_text(encoding="ascii") == "keep"


def test_receipt_preflight_rejects_symlink_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _receipt_root(monkeypatch, tmp_path)
    target = root / "target.json"
    target.write_text("keep", encoding="ascii")
    alias = root / "alias.json"
    alias.symlink_to(target)

    with pytest.raises(RuntimeError, match="symlink"):
        review._reserve_receipt(str(alias))

    assert target.read_text(encoding="ascii") == "keep"


def test_receipt_preflight_rejects_destination_outside_private_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _receipt_root(monkeypatch, tmp_path)
    outside = tmp_path / "outside.json"

    with pytest.raises(RuntimeError, match="directly inside"):
        review._reserve_receipt(str(outside))

    assert not outside.exists()
    assert not list(root.iterdir())


def test_snapshot_scorer_alias_must_match_private_role_dsn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    alias = snapshot / "data/runtime/postgres/scorer.dsn"
    alias.parent.mkdir(parents=True)
    alias.write_text("postgresql+psycopg://wrong@127.0.0.1/db\n", encoding="ascii")
    alias.chmod(0o600)
    monkeypatch.setattr(review, "DEFAULT_DSN_FILE", alias)
    dsn_dir = tmp_path / "dsns"
    dsn_dir.mkdir(mode=0o700)
    (dsn_dir / "scorer.dsn").write_text(
        "postgresql+psycopg://expected@127.0.0.1/db\n", encoding="ascii"
    )

    with pytest.raises(RuntimeError, match="does not match"):
        review._verify_snapshot_scorer_alias(dsn_dir)

    assert alias.read_text(encoding="ascii").startswith("postgresql+psycopg://wrong")


def test_database_scope_keeps_published_nat_and_sql_server_ports_separate() -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def one(self):
            return self.value

        def scalars(self):
            return self

        def all(self):
            return ["0021_run_end_unavailable"]

    def make_engine(role: str):
        @contextmanager
        def connect():
            class Connection:
                def execute(self, statement):
                    statements.append(str(statement))
                    if "alembic_version" in str(statement):
                        return Result(None)
                    return Result(
                        (
                            "swapp_lab_m0_holdout_030_unit",
                            f"swapp_lab_{role}",
                            "172.18.0.2",
                            5432,
                            "2026-09-27 10:00:00+00",
                        )
                    )

            yield Connection()

        return SimpleNamespace(
            url=f"postgresql+psycopg://swapp_lab_{role}:secret@127.0.0.1:49152/"
            "swapp_lab_m0_holdout_030_unit",
            connect=connect,
        )

    engines = {role: make_engine(role) for role in review.EXPECTED_ROLE_NAMES}

    assert (
        review._verify_database_scope(engines, "0021_run_end_unavailable")
        == "swapp_lab_m0_holdout_030_unit"
    )
    assert any("host(inet_server_addr())" in statement for statement in statements)
    assert "SELECT version_num FROM lab.alembic_version" in statements


def test_preflight_cleanup_preserves_replaced_receipt_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _receipt_root(monkeypatch, tmp_path)
    reservation = review._reserve_receipt(str(root / "receipt.json"))
    path = reservation[0]
    path.unlink()
    path.write_text("replacement", encoding="ascii")

    assert not review._remove_reserved_receipt(reservation)
    assert path.read_text(encoding="ascii") == "replacement"
