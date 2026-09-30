"""Strict lexical validation for the shared scheduler and private test database."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _absolute_directory_chain(path: Path) -> tuple[Path, ...]:
    if not path.is_absolute():
        raise ValueError("GPU runtime directory must be absolute")
    components = [Path("/")]
    current = Path("/")
    for part in path.parts[1:]:
        current = current / part
        components.append(current)
    return tuple(components)


def _validate_private_directory_chain(path: Path, *, strict_root: Path) -> None:
    for directory in _absolute_directory_chain(path):
        try:
            info = directory.lstat()
        except OSError as exc:
            raise ValueError("GPU runtime directory is unavailable") from exc
        sticky_root_directory = bool(info.st_mode & stat.S_ISVTX) and info.st_uid == 0
        if (
            directory.is_symlink()
            or not directory.is_dir()
            or info.st_uid not in {0, os.getuid()}
            or (info.st_mode & 0o022 and not sticky_root_directory)
        ):
            raise ValueError("GPU runtime path has an unsafe directory component")
        if directory == strict_root and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("GPU runtime directory must be private to the service owner")


def validated_gpu_runtime_database(
    database: Path, *, project_root: Path, gpu_runtime_root: Path
) -> Path:
    """Accept only the fixed broker DB or a private direct-child test DB."""
    shared = Path.home() / ".local/state/swapp-gpu/arbiter.sqlite3"
    if not database.is_absolute() or database.is_symlink():
        raise ValueError("GPU runtime database must be absolute and cannot be a symlink")
    if database == shared:
        home = Path.home()
        for directory in _absolute_directory_chain(shared.parent):
            try:
                info = directory.lstat()
            except OSError as exc:
                raise ValueError("shared GPU runtime directory is unavailable") from exc
            is_home_or_descendant = directory == home or home in directory.parents
            sticky_root_directory = bool(info.st_mode & stat.S_ISVTX) and info.st_uid == 0
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or info.st_uid not in {0, os.getuid()}
                or (info.st_mode & 0o022 and not sticky_root_directory)
            ):
                raise ValueError("shared GPU runtime path has an unsafe directory component")
            if sticky_root_directory:
                continue
            if is_home_or_descendant and info.st_uid != os.getuid():
                raise ValueError("shared GPU home path is not owned by the service owner")
            if (
                directory
                in {
                    home / ".local/state",
                    home / ".local/state/swapp-gpu",
                }
                and info.st_mode & 0o077
            ):
                raise ValueError("shared GPU state directory must be private")
        parent = shared.parent
    else:
        if gpu_runtime_root != project_root / "data/runtime/gpu":
            raise ValueError("GPU runtime root differs from the fixed project path")
        if database.name in {"", ".", ".."} or database.parent != gpu_runtime_root:
            raise ValueError("GPU runtime database must be a direct child of its private root")
        _validate_private_directory_chain(gpu_runtime_root, strict_root=gpu_runtime_root)
        parent = gpu_runtime_root
    try:
        database_info = database.lstat()
    except FileNotFoundError:
        database_info = None
    except OSError as exc:
        raise ValueError("GPU runtime database cannot be inspected") from exc
    if database_info is not None and (
        stat.S_ISLNK(database_info.st_mode)
        or not stat.S_ISREG(database_info.st_mode)
        or database_info.st_uid != os.getuid()
        or database_info.st_mode & 0o077
    ):
        raise ValueError("GPU runtime database must be a private regular file")
    return parent / database.name
