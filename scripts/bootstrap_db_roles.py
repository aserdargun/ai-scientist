"""Create isolated, secret-backed Lab database roles on the local dev service."""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
SECRET_DIR = ROOT / "data/runtime/postgres"
ROLE_NAMES = {
    "migrator": "swapp_lab_migrator",
    "director": "swapp_lab_director",
    "planner": "swapp_lab_planner",
    "scorer": "swapp_lab_scorer",
}


def _write_secret(path: Path, value: str) -> None:
    """Atomically write a new mode-0600 credential without following symlinks."""
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to overwrite existing credential file: {path.name}")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _dsn(username: str, password: str) -> str:
    """Build a loopback-only SQLAlchemy psycopg DSN."""
    return f"postgresql+psycopg://{username}:{quote(password, safe='')}@127.0.0.1:55432/swapp_lab"


def bootstrap() -> None:
    """Provision roles and local-only credential files once."""
    if SECRET_DIR.is_symlink():
        raise RuntimeError("credential directory cannot be a symlink")
    SECRET_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(SECRET_DIR, 0o700)
    admin_path = SECRET_DIR / "admin.secret"
    admin_password = admin_path.read_text(encoding="utf-8").strip()
    if not admin_password:
        raise RuntimeError("admin credential is empty")

    secret_paths = {name: SECRET_DIR / f"{name}.secret" for name in ROLE_NAMES}
    dsn_paths = {name: SECRET_DIR / f"{name}.dsn" for name in ROLE_NAMES}
    director_token_path = SECRET_DIR / "director.token"
    scorer_token_path = SECRET_DIR / "scorer.token"
    legacy_paths = [
        *(secret_paths[name] for name in ("migrator", "director", "scorer")),
        *(dsn_paths[name] for name in ("migrator", "director", "scorer")),
        director_token_path,
        scorer_token_path,
    ]
    legacy_present = [path.exists() or path.is_symlink() for path in legacy_paths]
    planner_files = (secret_paths["planner"], dsn_paths["planner"])
    planner_presence = [path.exists() or path.is_symlink() for path in planner_files]
    if any(planner_presence) and not all(planner_presence):
        raise RuntimeError("planner credentials are partial; refusing ambiguous role recovery")
    planner_present = all(planner_presence)
    if any(legacy_present) and not all(legacy_present):
        raise RuntimeError("credential set is partial; refusing ambiguous role recovery")
    if all(legacy_present):
        credentials = {
            name: secret_paths[name].read_text(encoding="utf-8").strip()
            for name in ("migrator", "director", "scorer")
        }
        if planner_present:
            credentials["planner"] = secret_paths["planner"].read_text(encoding="utf-8").strip()
        else:
            credentials["planner"] = secrets.token_urlsafe(36)
            _write_secret(secret_paths["planner"], credentials["planner"])
            _write_secret(dsn_paths["planner"], _dsn(ROLE_NAMES["planner"], credentials["planner"]))
        director_token = director_token_path.read_text(encoding="utf-8").strip()
        scorer_token = scorer_token_path.read_text(encoding="utf-8").strip()
        for name, username in ROLE_NAMES.items():
            expected_dsn = _dsn(username, credentials[name])
            if dsn_paths[name].read_text(encoding="utf-8").strip() != expected_dsn:
                raise RuntimeError("saved DSN does not match its protected role credential")
    else:
        credentials = {name: secrets.token_urlsafe(36) for name in ROLE_NAMES}
        director_token = secrets.token_urlsafe(48)
        scorer_token = secrets.token_urlsafe(48)
        for role, username in ROLE_NAMES.items():
            _write_secret(secret_paths[role], credentials[role])
            _write_secret(dsn_paths[role], _dsn(username, credentials[role]))
        _write_secret(director_token_path, director_token)
        _write_secret(scorer_token_path, scorer_token)
    conninfo = {
        "host": "127.0.0.1",
        "port": 55432,
        "dbname": "swapp_lab",
        "user": "swapp_lab_admin",
        "password": admin_password,
        "application_name": "swapp_lab_role_bootstrap",
        "connect_timeout": 5,
    }
    try:
        with psycopg.connect(**conninfo, autocommit=True) as connection:
            existing_rows = connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                (list(ROLE_NAMES.values()),),
            ).fetchall()
            existing_names = {row[0] for row in existing_rows}
            for role, name in ROLE_NAMES.items():
                statement = (
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT CONNECTION LIMIT 10 PASSWORD {}"
                    if name in existing_names
                    else "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT CONNECTION LIMIT 10 PASSWORD {}"
                )
                connection.execute(
                    sql.SQL(statement).format(sql.Identifier(name), sql.Literal(credentials[role]))
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE swapp_lab TO {}").format(
                        sql.Identifier(name)
                    )
                )
            connection.execute(
                sql.SQL("GRANT CREATE ON DATABASE swapp_lab TO {}").format(
                    sql.Identifier(ROLE_NAMES["migrator"])
                )
            )
            for schema in ("lab", "scorer"):
                schema_exists = connection.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)
                ).fetchone()
                if not schema_exists:
                    connection.execute(
                        sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                            sql.Identifier(schema), sql.Identifier(ROLE_NAMES["migrator"])
                        )
                    )
                else:
                    connection.execute(
                        sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                            sql.Identifier(schema), sql.Identifier(ROLE_NAMES["migrator"])
                        )
                    )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA lab TO {}, {}").format(
                    sql.Identifier(ROLE_NAMES["director"]), sql.Identifier(ROLE_NAMES["scorer"])
                )
            )
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA scorer TO {}").format(
                    sql.Identifier(ROLE_NAMES["planner"])
                )
            )
    except Exception as exc:
        raise RuntimeError(f"database role bootstrap failed ({type(exc).__name__})") from None

    print("Created dedicated migrator, director, and scorer roles.")
    print("Credential files use mode 0600 under data/runtime/postgres/ (values omitted).")


if __name__ == "__main__":
    try:
        bootstrap()
    except Exception as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)
