"""Expose guard bypasses and metadata leakage without executing candidate code."""
from datetime import UTC,datetime
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
from lab.sandbox.evaluation import _arrow_bytes,_relative_difference,check_hardcoding

ROOT=Path(__file__).resolve().parents[3]
source=ROOT/'lab/sandbox/evaluation.py'
source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
frame=pd.DataFrame({'sensor_a':[1.,2.,3.]})
frame.attrs={'eval_labels':[0,1,0],'private_marker':'owned-review-canary'}
table=ipc.open_stream(pa.BufferReader(_arrow_bytes(frame))).read_all()
observed={
    'metadata':{k.decode():v.decode() for k,v in (table.schema.metadata or {}).items()},
    'restored_attrs':table.to_pandas().attrs,
    'difference_for_doubled_small_scores':_relative_difference(np.array([1e-12,2e-12]),np.array([2e-12,4e-12])),
    'negative64_guard':check_hardcoding(('A=['+', '.join(str(-float(i)) for i in range(64))+']').encode(),task_ids=frozenset(),evaluation_instants=frozenset()).code,
    'interior_time_guard':check_hardcoding(b'T="2026-09-24T13:15:00+03:00"',task_ids=frozenset(),evaluation_instants=frozenset({'2026-09-24T10:00:00Z','2026-09-24T11:00:00Z'})).code,
}
record={'checked_at':datetime.now(UTC).isoformat(),'source_sha256':{'lab/sandbox/evaluation.py':source_hash},
    'scope':'Historical negative evidence against WIP evaluator; own three-row fake-label metadata fixture and AST/array checks only. No candidate execution, GPU or AOS changes.',
    'observation':observed,
    'violations_reproduced':{'private_attrs_survive_candidate_arrow':observed['restored_attrs']==frame.attrs,
        'doubling_small_scores_passes_1e7_relative_gate':observed['difference_for_doubled_small_scores']<=1e-7,
        'negative_numeric_literal_list_passes':observed['negative64_guard']=='hardcoding_pass',
        'timestamp_in_eval_interval_passes':observed['interior_time_guard']=='hardcoding_pass'},
    'source_unchanged':hashlib.sha256(source.read_bytes()).hexdigest()==source_hash,
    'command':'.venv/bin/python docs/ai-scientist/review-evidence/review_guard_preflight.py',
    'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
Path(__file__).with_name('guard-preflight-before.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record,indent=2))
if not record['source_unchanged'] or not all(record['violations_reproduced'].values()):raise SystemExit(1)
