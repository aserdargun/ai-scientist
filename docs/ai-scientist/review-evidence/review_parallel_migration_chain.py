"""Rehearse the final migration chain only on the named restored private clone."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
DATABASE = "swapp_lab_m0_migration_rehearsal_ab103ab3"
FROZEN = {
    "0016_director_recovery.py": (
        "recovery", "764ae650a0e22e8d87dd46da744d847056995cbd18c0c212fe96ff610f689024"
    ),
    "0018_public_task_semantics.py": (
        "public-suite", "b22838af02241fcfd03a282552536b06712595462da62eb93cff99d3b3556b17"
    ),
}


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot(engine) -> dict:
    with engine.connect() as connection:
        if connection.execute(text("SELECT current_database()")).scalar_one() != DATABASE:
            raise ValueError("migration rehearsal database identity differs")
        names = connection.execute(text(
            "SELECT table_schema,table_name FROM information_schema.tables "
            "WHERE table_schema IN ('lab','scorer') AND table_type='BASE TABLE' "
            "ORDER BY table_schema,table_name"
        )).all()
        tables = {}
        for schema, name in names:
            if not all(re.fullmatch(r"[a-z0-9_]+", item) for item in (schema, name)):
                raise ValueError("unexpected table identifier")
            rows = connection.execute(text(
                f"SELECT row_to_json(t)::text FROM {schema}.{name} AS t"
            )).scalars().all()
            canonical = sorted(json.dumps(json.loads(row), sort_keys=True, separators=(",", ":"))
                               for row in rows)
            tables[f"{schema}.{name}"] = {
                "count": len(rows), "sha256": sha("\n".join(canonical).encode())
            }
        return {
            "tables": tables,
            "revision": connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one(),
            "director_view_grant": connection.execute(text(
                "SELECT has_table_privilege('swapp_lab_director','lab.dev_task_results','SELECT')"
            )).scalar_one(),
        }


def main() -> int:
    os.umask(0o077)
    output = Path(__file__).with_name("parallel-migration-chain-rehearsal.json")
    if output.exists():
        raise ValueError("refusing to overwrite rehearsal evidence")
    private = ROOT / "data/runtime/parallel-m0" / ("migration-rehearsal-" + uuid4().hex)
    private.mkdir(mode=0o700)
    migration_root = private / "migrations"
    shutil.copytree(ROOT / "lab/db/migrations", migration_root, ignore=shutil.ignore_patterns("__pycache__"))
    pinned = {}
    for name, (branch, expected) in FROZEN.items():
        source = ROOT / "data/runtime/parallel-m0" / branch / "lab/db/migrations/versions" / name
        payload = source.read_bytes()
        if sha(payload) != expected:
            raise ValueError("migration source differs from the frozen review")
        pinned[name] = payload
    source_18 = pinned["0018_public_task_semantics.py"].decode()
    old = 'down_revision = "0016_director_recovery"'
    if source_18.count(old) != 1:
        raise ValueError("unexpected final migration ancestry")
    (migration_root / "versions/0018_public_task_semantics.py").write_text(
        source_18.replace(old, 'down_revision = "0015_external_run_mapping"')
    )
    config = private / "alembic.ini"
    ini = (ROOT / "alembic.ini").read_text()
    if ini.count("script_location = lab/db/migrations") != 1:
        raise ValueError("unexpected Alembic script-location setting")
    config.write_text(ini.replace("script_location = lab/db/migrations", f"script_location = {migration_root}"))
    url = make_url((ROOT / "data/runtime/postgres/migrator.dsn").read_text().strip())
    url = url.set(database=DATABASE)
    dsn_file = private / "migrator.dsn"
    dsn_file.write_text(url.render_as_string(hide_password=False))
    dsn_file.chmod(0o600)
    engine = create_engine(url)
    record = {
        "schema": "parallel-migration-chain-rehearsal.v1",
        "scope": "Restored private clone only; no main database/schema/role writes.",
        "database": DATABASE, "directory": str(private.relative_to(ROOT)),
        "source_sha256": {name: sha(value) for name, value in pinned.items()},
        "commands": [], "checks": {},
    }

    def migrate(action: str, target: str) -> None:
        command = [str(ROOT / ".venv/bin/python"), "-m", "alembic", "-c", str(config), action, target]
        result = subprocess.run(command, cwd=ROOT,
                                env={**os.environ, "LAB_MIGRATOR_DSN_FILE": str(dsn_file)},
                                capture_output=True, text=True, timeout=45, check=False)
        record["commands"].append({"command": command, "exit_code": result.returncode,
                                   "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError("bounded migration command failed")

    try:
        before = snapshot(engine)
        record["before"] = before
        if (before["revision"] != "0018_public_task_semantics"
                or before["tables"]["scorer.dataset_task_semantics"]["count"] != 0
                or "lab.director_run_owners" in before["tables"]):
            raise ValueError("clone is not the expected unpopulated temporary-chain state")
        with engine.connect() as connection:
            if connection.execute(text("SELECT count(*) FROM scorer.dataset_profiles WHERE task_family!='EVT'")).scalar_one():
                raise ValueError("cannot drop non-EVT family data")
        migrate("downgrade", "0015_external_run_mapping")
        for name, payload in pinned.items():
            (migration_root / "versions" / name).write_bytes(payload)
        migrate("upgrade", "0018_public_task_semantics")
        after = snapshot(engine)
        record["after"] = after
        record["checks"] = {
            "all_commands_exit_zero": all(row["exit_code"] == 0 for row in record["commands"]),
            "final_revision_0018": after["revision"] == "0018_public_task_semantics",
            "all_original_table_bytes_preserved": all(
                after["tables"].get(name) == value
                for name, value in before["tables"].items() if name != "lab.alembic_version"
            ),
            "both_new_recovery_tables_empty": all(after["tables"].get(name, {}).get("count") == 0
                for name in ("lab.director_run_owners", "lab.director_recoveries")),
            "director_view_grant_preserved": before["director_view_grant"] and after["director_view_grant"],
            "final_0018_ancestry_preserved": old in (migration_root / "versions/0018_public_task_semantics.py").read_text(),
        }
        record["passed"] = all(record["checks"].values())
    except Exception as exc:
        record.update({"passed": False, "error_type": type(exc).__name__})
    finally:
        engine.dispose()
        record["script_sha256"] = sha(Path(__file__).read_bytes())
        output.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"output": str(output.relative_to(ROOT)), "passed": record["passed"], "checks": record["checks"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
