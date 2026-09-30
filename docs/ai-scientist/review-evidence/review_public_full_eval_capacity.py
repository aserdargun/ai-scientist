"""Measure full public source-order evaluations without changing production policy.

The review process alone raises JSON admission to 128 MiB; matrix admission stays
64 MiB. No database writes or candidate/metric/model execution is performed.
"""
from __future__ import annotations
import hashlib,json,resource
from pathlib import Path
import lab.director.suite_manifest as manifest
from lab.director.public_suite import (materialize_care_development,materialize_skab_development,
 materialize_smd_development,materialize_tsb_development,finalize_public_suite_weights)
ROOT=Path('/home/cachyos/ai-scientist')
OUT=ROOT/'docs/ai-scientist/review-evidence/public-full-eval-capacity-review.json'
DEST=ROOT/'data/runtime/parallel-m0/integration-024/full-eval-capacity-manifest.json'
assert not OUT.exists() and not DEST.exists()
original_json_cap=manifest.MAX_SUITE_MANIFEST_BYTES
manifest.MAX_SUITE_MANIFEST_BYTES=128*1024**2
items=(*materialize_care_development(repository_root=ROOT),
 *materialize_skab_development(repository_root=ROOT),
 *materialize_smd_development(repository_root=ROOT),
 *materialize_tsb_development(repository_root=ROOT))
weighted,weights=finalize_public_suite_weights(items)
byte_count,digest=manifest.write_suite_manifest(tuple(x.task for x in weighted),DEST,suite_version=2)
checks=[]
for x in weighted:
 if x.task.family!='EVT':continue
 reg=x.scorer_registration
 usable=[y for y,m in zip(reg.labels,reg.masked_samples,strict=True) if not m]
 checks.append({'dataset_id':x.task.dataset_id,'profile_sha256':x.task.profile_sha256,
 'evaluation_rows':len(x.task._evaluation),'has_both_label_classes_after_mask':bool(any(usable) and not all(usable)),
 'enough_points_for_fixed_window':len(usable)>=reg.sliding_window})
record={'schema':'public-full-eval-capacity-review.v1','scope':'Full fixed source-prefix/embargo evaluations, review-only JSON cap override; no DB, scoring or model execution.',
 'json_cap_before':original_json_cap,'review_json_cap':manifest.MAX_SUITE_MANIFEST_BYTES,
 'matrix_cap':manifest.MAX_SUITE_MATRIX_BYTES,'manifest_bytes':byte_count,'manifest_sha256':digest,
 'matrix_bytes':sum(x.task._train.nbytes+x.task._evaluation.nbytes for x in weighted),
 'task_count':len(weighted),'evt_eligibility':checks,'max_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
 'all_evt_eligible':all(x['has_both_label_classes_after_mask'] and x['enough_points_for_fixed_window'] for x in checks),
 'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
OUT.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
raise SystemExit(0 if record['all_evt_eligible'] else 1)
