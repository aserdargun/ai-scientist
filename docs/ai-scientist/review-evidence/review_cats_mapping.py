import json,zipfile,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
source=Path('data/public/original-sources/CATSv2/8338435/data.parquet')
parquet=pq.ParquetFile(source)
sensors=parquet.schema.names[:17]
slices=[]
with zipfile.ZipFile('data/public/tsb-ad-m/source-2026-09-24/TSB-AD-M.zip') as archive:
    for name in sorted(x for x in archive.namelist() if '_CATSv2_' in x):
        with archive.open(name) as reader: frame=pd.read_csv(reader,float_precision='round_trip')
        assert frame.columns.tolist()==sensors+['Label']
        slices.append({'member':name,'values':frame[sensors].to_numpy(dtype=float),'labels':frame['Label'].to_numpy(),'starts':[],'compared_rows':0,'max_abs_difference':0.0,'label_mismatch_rows':0})
position=0; cadence_counts={}; previous=None; first_time=None; last_time=None
for batch in parquet.iter_batches(batch_size=65536,columns=sensors+['__index_level_0__'],use_threads=False):
    values=np.column_stack([batch.column(i).to_numpy() for i in range(17)])
    timestamps=batch.column(17).to_numpy().astype('datetime64[us]').astype(np.int64)
    if first_time is None: first_time=int(timestamps[0])
    steps=np.diff(timestamps if previous is None else np.concatenate(([previous],timestamps)))
    for step,count in zip(*np.unique(steps,return_counts=True)): cadence_counts[str(int(step))]=cadence_counts.get(str(int(step)),0)+int(count)
    previous=int(timestamps[-1]); last_time=previous
    for item in slices:
        matches=np.flatnonzero(np.isclose(values[:,2],item['values'][0,2],atol=1e-12,rtol=0))
        for index in matches:
            if np.allclose(values[index],item['values'][0],atol=1e-12,rtol=0): item['starts'].append(position+int(index))
    position+=len(values)
assert all(len(item['starts'])==1 for item in slices),[item['starts'] for item in slices]
position=0
for batch in parquet.iter_batches(batch_size=65536,columns=sensors+['y'],use_threads=False):
    values=np.column_stack([batch.column(i).to_numpy() for i in range(17)])
    labels=batch.column(17).to_numpy()
    for item in slices:
        start=item['starts'][0]; end=start+len(item['values'])
        lo=max(position,start); hi=min(position+len(values),end)
        if lo>=hi: continue
        observed=values[lo-position:hi-position]; curated=item['values'][lo-start:hi-start]
        diff=float(np.max(np.abs(observed-curated)))
        item['max_abs_difference']=max(item['max_abs_difference'],diff)
        item['label_mismatch_rows']+=int(np.count_nonzero(labels[lo-position:hi-position]!=item['labels'][lo-start:hi-start]))
        item['compared_rows']+=hi-lo
    position+=len(values)
records=[]
for item in slices:
    assert item['compared_rows']==len(item['values'])
    records.append({key:value for key,value in item.items() if key not in ['values','labels']})
result={'source_record':8338435,'source_rows':parquet.metadata.num_rows,'source_row_groups':parquet.metadata.num_row_groups,'sensor_columns':sensors,'excluded_columns':['y','category','__index_level_0__'],'source_timestamp_unit':'microseconds, timezone unspecified (Parquet isAdjustedToUTC=false)','source_first_timestamp_us':first_time,'source_last_timestamp_us':last_time,'source_timestamp_step_counts_us':cadence_counts,'curated_slices':records,'method':'streamed 65536-row batches, first-row 17-feature matching then complete ordered values and labels comparison','source_sha256':json.loads(Path('docs/ai-scientist/review-evidence/cats-original-source.json').read_text())['sha256']}
Path('docs/ai-scientist/review-evidence/cats-curated-mapping-review.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
