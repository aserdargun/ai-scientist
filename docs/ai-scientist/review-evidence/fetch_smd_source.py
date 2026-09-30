"""Fetch immutable official SMD bytes; preserve machine and source split identities."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import shutil
import time
import urllib.request
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
REVISION = '7fb0e0acf89ea49908896bcc9f9e80fcfff6baf4'
CACHE = ROOT / 'data/public/smd' / REVISION
OUT = Path(__file__).with_name('smd-source-manifest.json')


def fetch(url, maximum):
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'swapp-ai-scientist-source-review'})
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read(maximum + 1)
            if len(body) > maximum:
                raise ValueError('download size exceeded')
            return body
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def one(entry):
    relative = Path(entry['path'])
    if relative.is_absolute() or '..' in relative.parts or entry['type'] != 'blob':
        raise ValueError('invalid upstream path')
    target = CACHE / relative
    url = f'https://raw.githubusercontent.com/NetManAIOps/OmniAnomaly/{REVISION}/{relative.as_posix()}'
    body = target.read_bytes() if target.exists() else fetch(url, entry['size'])
    blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
    if len(body) != entry['size'] or blob != entry['sha']:
        raise ValueError(f'upstream Git blob mismatch: {relative}')
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(body)
    record = {'path': str(target.relative_to(ROOT)), 'upstream_path': relative.as_posix(), 'source_url': url,
              'bytes': len(body), 'git_blob_sha1': blob, 'sha256': hashlib.sha256(body).hexdigest()}
    if relative.suffix == '.txt':
        split = relative.parts[-2]
        record.update(entity=relative.stem, source_split=split)
        if split in ('train', 'test', 'test_label'):
            values = np.loadtxt(io.BytesIO(body), delimiter=',', ndmin=2)
            record.update(rows=int(values.shape[0]), columns=int(values.shape[1]),
                          nonfinite_values=int(np.count_nonzero(~np.isfinite(values))))
            if record['nonfinite_values']:
                raise ValueError('nonfinite source values')
            if split == 'test_label':
                if values.shape[1] != 1 or not np.isin(values, (0,1)).all():
                    raise ValueError('invalid binary labels')
                record['anomaly_positive_rows'] = int(values.sum())
                record['anomaly_events'] = int(np.count_nonzero(np.diff(np.r_[0, values[:,0]]) == 1))
            elif values.shape[1] != 38:
                raise ValueError('expected 38 anonymous sensor columns')
            else:
                record['sensor_columns'] = [f'sensor_{index:02d}' for index in range(38)]
    return record


def main():
    started = time.monotonic()
    url = f'https://api.github.com/repos/NetManAIOps/OmniAnomaly/git/trees/{REVISION}?recursive=1'
    tree = json.loads(fetch(url, 2_000_000))
    if tree.get('truncated'):
        raise ValueError('truncated source tree')
    selected = [item for item in tree['tree'] if item['type'] == 'blob' and
                (item['path'] in ('README.md', 'LICENSE') or item['path'].startswith('ServerMachineDataset/'))]
    total = sum(item['size'] for item in selected)
    if shutil.disk_usage(ROOT).free < 20 * 1024**3 + total:
        raise RuntimeError('20 GiB free disk reserve would be breached')
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(one, sorted(selected, key=lambda item: item['path'])))
    machines = {}
    for record in records:
        if record.get('source_split') in ('train', 'test', 'test_label', 'interpretation_label'):
            machines.setdefault(record['entity'], {})[record['source_split']] = record
    if len(machines) != 28:
        raise ValueError('expected 28 separate machines')
    for entity, parts in machines.items():
        if set(parts) != {'train', 'test', 'test_label', 'interpretation_label'}:
            raise ValueError('incomplete machine source parts')
        if parts['test']['rows'] != parts['test_label']['rows']:
            raise ValueError('test/label row mismatch')
    manifest = {'schema': 'smd-source-manifest.v1', 'checked_at': datetime.now(UTC).isoformat(),
                'source_repository': 'https://github.com/NetManAIOps/OmniAnomaly', 'revision': REVISION,
                'tree_request_url': url, 'license': 'MIT',
                'dataset_license_url': f'https://github.com/NetManAIOps/OmniAnomaly/blob/{REVISION}/ServerMachineDataset/LICENSE',
                'usage_profile': 'noncommercial_research', 'domain': 'server_telemetry',
                'source_split_policy': 'upstream train is former half and test is latter half; machines remain separate',
                'timestamp_in_source': False, 'physical_sampling_seconds': 60,
                'sampling_source': 'https://netman.aiops.org/wp-content/uploads/2019/08/OmniAnomaly_camera-ready.pdf',
                'sampling_source_section': 'Appendix A: DATASETS',
                'sampling_evidence': 'docs/ai-scientist/review-evidence/smd-source-clock.json',
                'sampling_evidence_sha256': 'e09319f9f62505d6b49497484c9c2dd599e94d7bd484704d867c6bdb8f8652b3',
                'time_policy_status': 'source paper establishes 60 seconds; preserve relative sample axis, absolute time origin unknown',
                'train_labels_status': 'no train labels provided; do not invent event labels',
                'interpretation_labels_policy': 'trusted metadata only; never candidate features or context',
                'source_files': records, 'entities': sorted(machines), 'total_bytes': total,
                'total_train_rows': sum(parts['train']['rows'] for parts in machines.values()),
                'total_test_rows': sum(parts['test']['rows'] for parts in machines.values()),
                'elapsed_seconds': time.monotonic()-started,
                'command': '.venv/bin/python docs/ai-scientist/review-evidence/fetch_smd_source.py',
                'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'scope': 'source acquisition and structural verification only; not production loader, TSB slice mapping or split acceptance'}
    OUT.write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps({key: manifest[key] for key in ('revision','total_bytes','total_train_rows','total_test_rows','elapsed_seconds')}))


if __name__ == '__main__':
    main()
