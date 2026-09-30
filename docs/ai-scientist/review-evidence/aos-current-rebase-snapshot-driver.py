"""Capture public AOS package sources and check the old patch without applying it."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

ROOT = Path('/home/cachyos/ai-scientist')
SOURCE = Path('/home/cachyos/aos')
EVIDENCE = ROOT / 'docs/ai-scientist/review-evidence'
PUBLIC_CODE = ('src', 'services', 'scripts', 'tests', 'schemas', 'database/migrations')
PRIVATE_ROOTS = {'adapters', 'data', 'datasets', 'models', 'runs'}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_files(manifest: bytes) -> tuple[dict[str, str], list[str]]:
    declared: dict[str, str] = {}
    for line in manifest.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if match is None:
            raise ValueError('invalid package manifest row')
        expected, name = match.groups()
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name in declared:
            raise ValueError('invalid or repeated package path')
        if path.parts[0] in PRIVATE_ROOTS and path.parts[1:] != ('README.md',):
            raise ValueError('manifest contains runtime/private content')
        if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.gguf', '.safetensors', '.pt'}:
            raise ValueError('manifest contains a database or model')
        declared[name] = expected
    extra = {'MANIFEST.sha256', 'AI_SCIENTIST_COORDINATION.md'}
    for folder in PUBLIC_CODE:
        for path in (SOURCE / folder).rglob('*'):
            if path.suffix in {'.py', '.json', '.sql'} and '__pycache__' not in path.parts:
                if path.is_file():
                    extra.add(path.relative_to(SOURCE).as_posix())
    return declared, sorted(set(declared) | extra)


def public_bytes(name: str) -> bytes:
    path = SOURCE / name
    current = path
    while current != SOURCE:
        if current.is_symlink():
            raise ValueError('public source contains symlink')
        current = current.parent
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 8 * 1024**2:
        raise ValueError('public source is not a bounded regular file')
    data = path.read_bytes()
    if len(data) != metadata.st_size:
        raise ValueError('public source changed size while reading')
    return data


def main() -> None:
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise RuntimeError('insufficient free disk')
    manifest = public_bytes('MANIFEST.sha256')
    declared, selected = package_files(manifest)
    contents = {name: public_bytes(name) for name in selected}
    total = sum(map(len, contents.values()))
    if total > 64 * 1024**2:
        raise ValueError('source snapshot exceeds 64 MiB')
    hashes = {name: digest(content) for name, content in contents.items()}
    identifier = uuid4().hex[:12]
    destination = ROOT / 'data/runtime/aos-coexistence' / ('source-rebase-20260927-' + identifier)
    destination.mkdir(mode=0o700)
    for name, content in contents.items():
        target = destination / name
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_bytes(content)
        executable = bool((SOURCE / name).stat().st_mode & stat.S_IXUSR)
        target.chmod(0o700 if executable else 0o600)
    copied = {name: digest((destination / name).read_bytes()) for name in selected}
    source_after = {name: digest(public_bytes(name)) for name in selected}
    selected_after = package_files(public_bytes('MANIFEST.sha256'))[1]
    if copied != hashes or source_after != hashes or selected_after != selected:
        raise RuntimeError('source or file inventory changed; snapshot is not frozen')
    git_env = {
        key: value for key, value in os.environ.items()
        if not key.startswith('GIT_')
    }
    git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
    initialized = subprocess.run(
        ['git', 'init', '--quiet', '--template=', '--initial-branch=source-review', str(destination)],
        capture_output=True, text=True, timeout=10, env=git_env, check=False,
    )
    if initialized.returncode:
        raise RuntimeError('private source-only Git initialization failed')
    patch = EVIDENCE / 'aos-lab-gpu-broker.patch'
    patch_sha = digest(patch.read_bytes())
    checked = subprocess.run(
        ['git', 'apply', '--check', str(patch)], cwd=destination, env=git_env,
        capture_output=True, text=True, timeout=20, check=False,
    )
    if len(checked.stdout) + len(checked.stderr) > 64 * 1024:
        raise RuntimeError('patch check output exceeded limit')
    if copied != {name: digest((destination / name).read_bytes()) for name in selected}:
        raise RuntimeError('check unexpectedly modified snapshot source')
    if patch_sha != digest(patch.read_bytes()):
        raise RuntimeError('patch changed during applicability check')
    disagreements = [name for name, expected in declared.items() if hashes[name] != expected]
    report = {
        'schema': 'aos-current-rebase-preparation.v1',
        'recorded_at': datetime.now(UTC).isoformat(),
        'scope': 'Public source-only snapshot and non-applying patch check. No AOS source/runtime edits, service/model/DB launch or package/runtime acceptance.',
        'source': str(SOURCE), 'snapshot': str(destination.relative_to(ROOT)),
        'declared_files': len(declared), 'snapshot_files': len(selected), 'total_bytes': total,
        'manifest_sha256': digest(manifest), 'manifest_disagreements': disagreements,
        'additional_public_files': sorted(set(selected) - set(declared)),
        'source_sha256': hashes, 'source_unchanged_during_copy': True,
        'snapshot_byte_parity': True, 'snapshot_unchanged_by_patch_check': True,
        'patch': str(patch.relative_to(ROOT)), 'patch_sha256': patch_sha,
        'git_init_exit_code': initialized.returncode,
        'patch_check': {
            'command': ['git', 'apply', '--check', str(patch)],
            'actual_exit_code': checked.returncode,
            'stdout': checked.stdout, 'stderr': checked.stderr,
        },
        'driver_sha256': digest(Path(__file__).read_bytes()),
        'next_action': 'Reconcile isolated integration with current sources; source review and CPU tests before any separately coordinated live AOS/GPU work.',
    }
    evidence = EVIDENCE / ('aos-current-rebase-preparation-' + identifier + '.json')
    evidence.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({key: report[key] for key in (
        'snapshot', 'declared_files', 'snapshot_files', 'total_bytes',
        'manifest_disagreements', 'additional_public_files',
    )}, ensure_ascii=False))
    print(json.dumps({'evidence': str(evidence.relative_to(ROOT)), 'patch_check_exit_code': checked.returncode}))


if __name__ == '__main__':
    main()
