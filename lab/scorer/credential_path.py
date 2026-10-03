"""Select a deployment-owned Scorer credential pathname without reading secrets.

Only service configuration supplies this environment variable. Director and API
supervisors transport the pathname; Scorer processes alone read its contents.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_DSN_FILE = Path(__file__).resolve().parents[2] / "data/runtime/postgres/scorer.dsn"
SCORER_DSN_ENVIRONMENT = "LAB_SCORER_DSN_FILE"
MAX_CREDENTIAL_PATH_BYTES = 4096
MAX_CREDENTIAL_FILE_BYTES = 16 * 1024


def configured_scorer_dsn_file(default: Path = DEFAULT_DSN_FILE) -> Path:
    """Keep the historical default or validate an explicit private deployment file.

    Validation inspects paths and metadata only. An invalid explicit setting is
    an error, never permission to use another ledger's default credentials.
    """
    value = os.environ.get(SCORER_DSN_ENVIRONMENT)
    if value is None:
        return default
    if (
        not value
        or any(character in value for character in ("\x00", "\n", "\r"))
        or len(value.encode("utf-8")) > MAX_CREDENTIAL_PATH_BYTES
    ):
        raise ValueError("configured Scorer credential pathname is malformed")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("configured Scorer credential pathname must be absolute")
    try:
        for parent in reversed(path.parents):
            parent_info = parent.lstat()
            root_sticky = parent_info.st_uid == 0 and bool(parent_info.st_mode & stat.S_ISVTX)
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid not in {0, os.getuid()}
                or (parent_info.st_mode & 0o022 and not root_sticky)
            ):
                raise ValueError("configured Scorer credential path has an unsafe parent")
        info = path.lstat()
    except OSError as exc:
        raise ValueError("configured Scorer credential file is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
        or not 1 <= info.st_size <= MAX_CREDENTIAL_FILE_BYTES
    ):
        raise ValueError("configured Scorer credential file must be private and current-owned")
    return path


def scorer_deployment_environment() -> list[str]:
    """Return only the validated pathname argument for a configured child service."""
    if SCORER_DSN_ENVIRONMENT not in os.environ:
        return []
    return [f"--setenv={SCORER_DSN_ENVIRONMENT}={configured_scorer_dsn_file()}"]
