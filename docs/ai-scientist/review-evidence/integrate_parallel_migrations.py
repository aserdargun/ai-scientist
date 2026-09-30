"""Apply the rehearsed migration chain to the unchanged, backed-up local Lab DB."""

from __future__ import annotations

import ast
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
EVIDENCE = ROOT / "docs/ai-scientist/review-evidence"
DATABASE = "swapp_lab"
FROZEN = {
    "0016_director_recovery.py": "764ae650a0e22e8d87dd46da744d847056995cbd18c0c212fe96ff610f689024",
    "0018_public_task_semantics.py": "14e86d8955339f9a7b5d38d73f06f69a98ad49b5fa9af79883bf263972540fae",
}


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def snapshot(engine) -> dict:
    with engine.connect() as connection:
        if connection.execute(text("SELECT current_database()")).scalar_one() != DATABASE:
            raise ValueError("unexpected database identity")
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
            tables[f"{schema}.{name}"] = {"count": len(rows), "sha256": sha("\n".join(canonical).encode())}
        return {
            "tables": tables,
            "revision": connection.execute(text("SELECT version_num FROM lab.alembic_version")).scalar_one(),
            "director_view_grant": connection.execute(text(
                "SELECT has_table_privilege('swapp_lab_director','lab.dev_task_results','SELECT')"
            )).scalar_one(),
        }


def main() -> int:
    os.umask(0o077)
    output = EVIDENCE / "parallel-migration-chain-main-integration.json"
    if output.exists():
        raise ValueError("refusing to overwrite main migration evidence")
    backup = json.loads((EVIDENCE / "pre-integrated-migrations-backup.json").read_text())
    rehearsal = json.loads((EVIDENCE / "parallel-migration-chain-rehearsal.json").read_text())
    archive = ROOT / backup["path"]
    if (not backup["passed"] or not backup["restore_tested"] or not rehearsal["passed"]
            or backup["database"] != DATABASE or archive.is_symlink()
            or archive.stat().st_mode & 0o077 or archive.stat().st_uid != os.getuid()
            or sha(archive.read_bytes()) != backup["sha256"]):
        raise ValueError("verified private backup/rehearsal preconditions failed")
    pinned = {}
    migration_parity = {}
    for name, expected in FROZEN.items():
        payload = (ROOT / "lab/db/migrations/versions" / name).read_bytes()
        original = (ROOT / rehearsal["directory"] / "migrations/versions" / name).read_bytes()
        if (sha(payload) != expected
                or sha(original) != rehearsal["source_sha256"][name]
                or ast.dump(ast.parse(payload), include_attributes=False)
                != ast.dump(ast.parse(original), include_attributes=False)):
            raise ValueError("main migration semantics differ from the successful rehearsal")
        migration_parity[name] = {
            "rehearsed_sha256": sha(original), "integrated_sha256": sha(payload),
            "exact_python_ast_parity": True,
        }
        pinned[name] = payload
    url = make_url((ROOT / "data/runtime/postgres/migrator.dsn").read_text().strip())
    if url.database != DATABASE:
        raise ValueError("configured migration database differs")
    engine = create_engine(url)
    record = {
        "schema": "parallel-migration-chain-main-integration.v1",
        "scope": "Apply rehearsed 0015->0016->0018 chain; preserve all historical run/data rows.",
        "database": DATABASE, "backup_sha256": backup["sha256"],
        "rehearsal_sha256": sha((EVIDENCE / "parallel-migration-chain-rehearsal.json").read_bytes()),
        "source_sha256": FROZEN, "migration_semantic_parity": migration_parity,
        "commands": [], "checks": {},
    }
    try:
        before = snapshot(engine)
        record["before"] = before
        if before != rehearsal["before"]:
            raise ValueError("main database changed since the restored backup; take a new backup/rehearsal")
        if (before["revision"] != "0018_public_task_semantics"
                or before["tables"]["scorer.dataset_task_semantics"]["count"]
                or "lab.director_run_owners" in before["tables"]):
            raise ValueError("main is not the unpopulated temporary migration chain")
        with engine.connect() as connection:
            if connection.execute(text(
                "SELECT count(*) FROM scorer.dataset_profiles WHERE task_family!='EVT'"
            )).scalar_one():
                raise ValueError("cannot drop non-EVT family data")
        # Only known Director entrypoints may write Lab runs. Refuse an active
        # worker rather than stopping it. Historical nonterminal rows are preserved.
        active = subprocess.run([
            "/usr/bin/systemctl", "--user", "list-units", "--state=active", "--no-legend",
            "--plain", "swapp-ai-scientist-director*.service",
        ], capture_output=True, text=True, check=True, timeout=5)
        if active.stdout.strip():
            raise ValueError("a Director service is active")
        private = ROOT / "data/runtime/parallel-m0" / ("main-migration-" + uuid4().hex)
        private.mkdir(mode=0o700)
        migration_root = private / "migrations"
        shutil.copytree(ROOT / "lab/db/migrations", migration_root,
                        ignore=shutil.ignore_patterns("__pycache__"))
        (migration_root / "versions/0016_director_recovery.py").unlink()
        ancestry = 'down_revision = "0016_director_recovery"'
        source18 = pinned["0018_public_task_semantics.py"].decode()
        if source18.count(ancestry) != 1:
            raise ValueError("unexpected migration ancestry")
        (migration_root / "versions/0018_public_task_semantics.py").write_text(
            source18.replace(ancestry, 'down_revision = "0015_external_run_mapping"')
        )
        config = private / "alembic.ini"
        ini = (ROOT / "alembic.ini").read_text()
        if ini.count("script_location = lab/db/migrations") != 1:
            raise ValueError("unexpected Alembic configuration")
        config.write_text(ini.replace("script_location = lab/db/migrations", f"script_location = {migration_root}"))
        record["temporary_directory"] = str(private.relative_to(ROOT))

        def migrate(action: str, target: str) -> None:
            command = [str(ROOT / ".venv/bin/python"), "-m", "alembic", "-c", str(config), action, target]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                    timeout=45, check=False, env={**os.environ,
                                        "LAB_MIGRATOR_DSN_FILE": str(ROOT / "data/runtime/postgres/migrator.dsn"),
                                        "PYTHONPATH": str(ROOT)})
            record["commands"].append({"command": command, "exit_code": result.returncode,
                                       "stdout": result.stdout, "stderr": result.stderr})
            if result.returncode:
                raise RuntimeError("main migration command failed; inspect evidence before any retry")

        migrate("downgrade", "0015_external_run_mapping")
        for name, payload in pinned.items():
            (migration_root / "versions" / name).write_bytes(payload)
        migrate("upgrade", "0018_public_task_semantics")
        after = snapshot(engine)
        record["after"] = after
        record["checks"] = {
            "all_commands_exit_zero": all(row["exit_code"] == 0 for row in record["commands"]),
            "exact_rehearsed_final_state": after == rehearsal["after"],
            "all_original_table_bytes_preserved": all(
                after["tables"].get(name) == value for name, value in before["tables"].items()
                if name != "lab.alembic_version"),
            "both_new_recovery_tables_empty": all(after["tables"].get(name, {}).get("count") == 0
                for name in ("lab.director_run_owners", "lab.director_recoveries")),
            "director_view_grant_preserved": before["director_view_grant"] and after["director_view_grant"],
        }
        record["passed"] = all(record["checks"].values())
    except Exception as exc:
        record.update({"passed": False, "error_type": type(exc).__name__})
        raise
    finally:
        engine.dispose()
        record["script_sha256"] = sha(Path(__file__).read_bytes())
        output.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"output": str(output.relative_to(ROOT)), "passed": record.get("passed", False),
                          "checks": record["checks"]}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
