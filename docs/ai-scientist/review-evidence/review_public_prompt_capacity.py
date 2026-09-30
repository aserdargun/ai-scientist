"""Offline pinned-tokenizer capacity check of production public-suite task cards.

Unmeasured score placeholders are zero and explicitly not research results.
"""
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
from uuid import uuid4
import json,hashlib
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from lab.director.suite_manifest import load_suite_manifest
from lab.director.loop import DirectorLoop
from lab.director.baselines import baseline_candidate_source
from lab.director.fake_llm import AgentContext
from lab.director.local_llm import LocalQwenProposalProvider,LOCAL_SMOKE_S2_PROFILE
ROOT=Path('/home/cachyos/ai-scientist')
OUT=ROOT/'docs/ai-scientist/review-evidence/public-prompt-capacity-review.json'
assert not OUT.exists()
url=make_url((ROOT/'data/runtime/parallel-m0/public-suite/data/runtime/postgres/planner.dsn').read_text().strip())
assert url.database=='swapp_lab_m0_public_suite_3ddaa2d8'
engine=create_engine(url)
try: doc,tasks,digest=load_suite_manifest(ROOT/'data/runtime/parallel-m0/integration-024/default-public-suite.json',engine)
finally:engine.dispose()
zero={t.task_id:0.0 for t in tasks}
state=SimpleNamespace(champion_seed0_by_task=zero,champion_seed1_by_task=zero)
calibration=SimpleNamespace(tasks=[SimpleNamespace(task_id=t.task_id,base_score=0.0,reference_score=0.0) for t in tasks])
cards=DirectorLoop._task_cards(SimpleNamespace(tasks=tasks),state,calibration)
context=AgentContext(phase='proposal',experiment_number=1,system='S2',move_type='features',task_cards=cards,champion_source=baseline_candidate_source('robust_z').decode(),recent_feedback=())
provider=LocalQwenProposalProvider(run_id=uuid4(),owner='lab',principal_resolver=None,runtime_database=ROOT/'data/runtime/parallel-m0/integration-024/no-gpu-prompt.sqlite3',registry_entry_sha256='1'*64)
profile=replace(LOCAL_SMOKE_S2_PROFILE,max_context_tokens=16384,model_max_len=20480)
messages=provider._message_variants(context,profile)
counts=provider._count_prompt_variants(messages,profile,timeout_seconds=10)
record={'schema':'public-prompt-capacity-review.v1','scope':'Actual offline pinned tokenizer and production task-card method, 27 public tasks, optimistic zero-valued unmeasured score placeholders. No GPU/model or scoring.','task_count':len(tasks),'suite_manifest_sha256':digest,'prompt_sha256':hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest(),'prompt_tokens':list(counts),'current_profile_context_tokens':LOCAL_SMOKE_S2_PROFILE.max_context_tokens,'fits_current_profile':all(n<=LOCAL_SMOKE_S2_PROFILE.max_context_tokens for n in counts),'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
OUT.write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
