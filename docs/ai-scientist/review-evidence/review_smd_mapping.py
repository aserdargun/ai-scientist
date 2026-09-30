"""Map all TSB SMD segments to immutable upstream test rows, preserving labels."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import re
from zipfile import ZipFile
import numpy as np

ROOT=Path(__file__).resolve().parents[3]
REVISION='7fb0e0acf89ea49908896bcc9f9e80fcfff6baf4'
SOURCE=ROOT/'data/public/smd'/REVISION/'ServerMachineDataset'
ARCHIVE=ROOT/'data/public/tsb-ad-m/source-2026-09-24/TSB-AD-M.zip'


def main():
    records=[]
    with ZipFile(ARCHIVE) as archive:
        names=sorted(name for name in archive.namelist() if '_SMD_' in name and name.endswith('.csv'))
        curated={}
        for name in names:
            payload=archive.read(name)
            values=np.loadtxt(io.BytesIO(payload),delimiter=',',skiprows=1)
            if values.shape[1] != 39 or not np.isfinite(values).all() or not np.isin(values[:,-1],(0,1)).all():
                raise ValueError('invalid TSB SMD shape/values')
            curated[name]=(values,hashlib.sha256(payload).hexdigest())
    matches={name:[] for name in names}
    for path in sorted((SOURCE/'test').glob('*.txt')):
        values=np.loadtxt(path,delimiter=',')
        labels=np.loadtxt(SOURCE/'test_label'/path.name,delimiter=',')
        for name,(segment,digest) in curated.items():
            possible=np.flatnonzero(np.isclose(values[:,0],segment[0,0],atol=1e-10,rtol=0))
            possible=possible[possible+len(segment)<=len(values)]
            if not len(possible):
                continue
            possible=possible[np.all(np.isclose(values[possible],segment[0,:-1],atol=1e-10,rtol=0),axis=1)]
            for start in possible:
                view=values[start:start+len(segment)]
                if not np.allclose(view,segment[:,:-1],atol=1e-10,rtol=0):
                    continue
                matches[name].append({'entity':path.stem,'source_split':'test','source_start_index':int(start),
                                      'source_end_index_exclusive':int(start+len(segment)),
                                      'source_test_rows':len(values),
                                      'max_abs_sensor_difference':float(np.max(np.abs(view-segment[:,:-1]))),
                                      'original_label_mismatches':int(np.count_nonzero(labels[start:start+len(segment)]!=segment[:,-1]))})
    for name in names:
        segment,digest=curated[name]
        records.append({'archive_member':name,'sha256':digest,'rows':len(segment),'sensor_columns':38,
                        'source_matches':matches[name],'unique_match':len(matches[name])==1,
                        'tsb_source_protocol_train_end':int(re.search(r'_tr_(\d+)_',name).group(1)),
                        'tsb_train_boundary_policy':'upstream curated protocol; label-derived metadata stays trusted',
                        'candidate_filename_policy':'opaque ID only; remove label-bearing original filename',
                        'tsb_anomaly_positive_rows':int(segment[:,-1].sum())})
    result={'checked_at':datetime.now(UTC).isoformat(),'source_revision':REVISION,
            'archive_sha256':hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),
            'source_manifest_sha256':hashlib.sha256(Path(__file__).with_name('smd-source-manifest.json').read_bytes()).hexdigest(),
            'files':records,'total_curated_rows':sum(row['rows'] for row in records),
            'all_uniquely_mapped':all(row['unique_match'] for row in records),
            'source_split_semantics':'TSB source protocol boundary is not the original train/test boundary',
            'scope':'source mapping only; trusted loader, actual split materialization and metric acceptance remain open',
            'command':'OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 .venv/bin/python docs/ai-scientist/review-evidence/review_smd_mapping.py',
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    Path(__file__).with_name('smd-curated-mapping-review.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'files':len(records),'rows':result['total_curated_rows'],'all_uniquely_mapped':result['all_uniquely_mapped'],
                      'unmatched':[row['archive_member'] for row in records if not row['unique_match']],
                      'label_mismatches':sum(match['original_label_mismatches'] for row in records for match in row['source_matches'])}))


if __name__=='__main__':
    main()
