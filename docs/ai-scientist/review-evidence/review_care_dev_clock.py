"""Inspect only CARE farm A source clocks/splits; do not materialize candidate tasks."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[3]

def main():
    start=time.monotonic()
    manifest_path=ROOT/'docs/ai-scientist/review-evidence/care-source-manifest.json'
    manifest=json.loads(manifest_path.read_bytes())
    archive=ROOT/manifest['archive']
    before=archive.stat()
    if before.st_size!=manifest['archive_bytes']:raise ValueError('archive size changed')
    record={'schema':'care-dev-clock-review.v1','checked_at':datetime.now(UTC).isoformat(),
        'archive_sha256_previously_verified':manifest['sha256'],
        'manifest_sha256':hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        'scope':'Only farm A development source members. Farm B holdout and farm C sealed members are not opened. Source structure/clock review only; no production split, grid, mask, feature-selection or public-data acceptance.',
        'files':[]}
    with zipfile.ZipFile(archive) as bundle:
        members=sorted(name for name in bundle.namelist() if name.startswith('CARE_To_Compare/Wind Farm A/datasets/') and name.endswith('.csv'))
        if len(members)!=22:raise ValueError('expected 22 farm A members')
        metadata=pd.read_csv(bundle.open('CARE_To_Compare/Wind Farm A/event_info.csv'),sep=';').set_index('event_id')
        for member in members:
            raw=bundle.read(member)
            raw_sha=hashlib.sha256(raw).hexdigest()
            header=raw.splitlines()[0].decode().split(';')
            sensors=[name for name in header if name.endswith('_avg')]
            columns=['time_stamp','asset_id','id','train_test','status_type_id',*sensors]
            frame=pd.read_csv(io.BytesIO(raw),sep=';',usecols=columns)
            times=pd.to_datetime(frame['time_stamp'],errors='raise')
            delta=times.diff().dt.total_seconds().dropna().to_numpy()
            train=frame['train_test'].eq('train').to_numpy()
            test=frame['train_test'].eq('prediction').to_numpy()
            train_times=times[train]
            train_delta=train_times.diff().dt.total_seconds().dropna().to_numpy()
            values=frame.loc[train,sensors].to_numpy(dtype=float)
            status=frame['status_type_id']
            event_id=int(Path(member).stem)
            event=metadata.loc[event_id]
            event_start=frame.loc[frame['id'].eq(int(event['event_start_id'])),'time_stamp']
            event_end=frame.loc[frame['id'].eq(int(event['event_end_id'])),'time_stamp']
            item={'member':member,'bytes':len(raw),'sha256':raw_sha,'rows':len(frame),
                'avg_sensor_count':len(sensors),'source_timezone':str(times.dt.tz) if times.dt.tz is not None else None,
                'split_counts':{str(k):int(v) for k,v in frame['train_test'].value_counts().items()},
                'split_transitions':int(np.sum(frame['train_test'].to_numpy()[1:]!=frame['train_test'].to_numpy()[:-1])),
                'train_prefix_then_prediction_suffix':bool(train.any() and test.any() and (train|test).all() and np.array_equal(np.flatnonzero(train),np.arange(train.sum()))),
                'timestamp_nonpositive_steps':int(np.sum(delta<=0)), 'timestamp_non_600s_steps':int(np.sum(delta!=600)),
                'maximum_interval_seconds':float(np.max(delta)),
                'timestamp_off_600s_grid_steps':int(np.sum(np.mod(delta,600)!=0)),
                'id_nonunit_steps':int(np.sum(np.diff(frame['id'].to_numpy())!=1)),
                'source_split_gap_seconds':float((times[test].iloc[0]-times[train].iloc[-1]).total_seconds()),
                'train_median_interval_seconds':float(np.median(train_delta)),
                'train_grid_gap_count':int(np.sum(train_delta>600)),
                'train_avg_nonfinite_values':int(np.sum(~np.isfinite(values))),
                'train_avg_rows_with_nonfinite':int(np.sum(~np.isfinite(values).all(axis=1))),
                'train_status_counts':{str(k):int(v) for k,v in status[train].value_counts(dropna=False).items()},
                'eval_status_counts':{str(k):int(v) for k,v in status[test].value_counts(dropna=False).items()},
                'source_event_label':str(event['event_label']),
                'event_start_id_timestamp_matches':bool(len(event_start)==1 and pd.Timestamp(event_start.iloc[0])==pd.Timestamp(event['event_start'])),
                'event_end_id_timestamp_matches':bool(len(event_end)==1 and pd.Timestamp(event_end.iloc[0])==pd.Timestamp(event['event_end']))}
            record['files'].append(item)
            print(json.dumps({'member':member,'rows':len(frame),'elapsed_s':round(time.monotonic()-start,2)}),flush=True)
            del raw,frame,values
    after=archive.stat()
    record['archive_stat_unchanged']=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
    record['summary']={'files':len(record['files']),'rows':sum(x['rows'] for x in record['files']),
        'all_train_then_prediction':all(x['train_prefix_then_prediction_suffix'] for x in record['files']),
        'all_event_id_timestamp_matches':all(x['event_start_id_timestamp_matches'] and x['event_end_id_timestamp_matches'] for x in record['files']),
        'files_with_non_600s_intervals':sum(x['timestamp_non_600s_steps']>0 for x in record['files']),
        'files_with_train_nonfinite_avg_values':sum(x['train_avg_nonfinite_values']>0 for x in record['files'])}
    record['elapsed_seconds']=round(time.monotonic()-start,3)
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_care_dev_clock.py (owned transient cgroup: 512MiB, swap0, 1CPU, 24Tasks, 120s)'
    Path(__file__).with_name('care-dev-clock-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record['summary'],indent=2))
    if not record['archive_stat_unchanged']:raise SystemExit(1)

if __name__=='__main__':main()
