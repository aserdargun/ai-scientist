import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from lab.reporting import read_run_pairs

root = Path('/home/cachyos/ai-scientist')
engine = create_engine((root/'data/runtime/postgres/director.dsn').read_text().strip(),
    hide_parameters=True, connect_args={'options': '-c statement_timeout=5000 -c default_transaction_read_only=on'})
rows = []
try:
    for run in ['26c9e91f-6db4-4733-bcb9-7ab698b2021b',
                '716a8941-e913-4307-982d-f643e800a216',
                '53424e63-903a-4e48-83a7-7b89eaad0cca']:
        pairs = read_run_pairs(engine, UUID(run), artifact_root=root/'data/runtime/director-artifacts'/run)
        kinds = Counter(); models = Counter(); tiers = Counter(); exclusions = Counter()
        flags = Counter(); licenses = Counter(); basetokens = 0
        for pair in pairs:
            doc, trj = pair.document, pair.trajectory
            kinds[doc.kind] += 1; models[trj.model_id] += 1; tiers[trj.quality_tier] += 1
            for flag in ['secrets_scrubbed', 'people_scrubbed', 'raw_values_scrubbed']:
                if not getattr(trj, flag): flags[flag] += 1
            exclusions.update(trj.exclusions)
            licenses.update({p.license_id for p in trj.source_provenance})
            basetokens += doc.llm_input_tokens + doc.llm_output_tokens
        rows.append({'run_id': run, 'verified_pairs': len(pairs), 'kinds': dict(kinds),
            'models': dict(models), 'quality_tiers': dict(tiers), 'unverified_scrub_flags': dict(flags),
            'recorded_exclusions': dict(exclusions), 'source_license_record_counts': dict(licenses),
            'recorded_model_tokens': basetokens,
            'training_export_eligibility': 'not_verified_no_export_pipeline',
            'experiment_trajectory_messages_hashes_verified': True})
finally:
    engine.dispose()
out = {'schema': 'training-record-readiness-audit.v1', 'created_at': datetime.now(timezone.utc).isoformat(),
    'scope': 'three_completed_CPU_projects_only', 'database_transaction_read_only': True,
    'raw_messages_or_sensor_data_exported': False, 'models_called': 0, 'runs': rows,
    'verified_pairs_total': sum(row['verified_pairs'] for row in rows),
    'training_export_produced': False,
    'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'next_requirements': ['versioned_derived_export_manifest', 'verified_scrub_results',
        'training_and_redistribution_rights_policy', 'rollback_chain_eligibility',
        'family_time_source_split_and_dedup', 'pinned_base_tokenizer_template_adapter_identity']}
path=root/'docs/ai-scientist/review-evidence/training052-record-readiness.json'
path.write_text(json.dumps(out, indent=2, ensure_ascii=False)+'\n')
print(json.dumps({'verified_pairs_total':out['verified_pairs_total'], 'per_run_pairs':[row['verified_pairs'] for row in rows],
    'training_export_produced':False,'proof':str(path.relative_to(root))}))
