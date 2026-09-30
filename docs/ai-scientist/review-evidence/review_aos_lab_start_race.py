"""Reproduce a lease change while the isolated AOS Lab start decision awaits."""
from __future__ import annotations
import asyncio, hashlib, importlib.util, json, sys
from datetime import UTC, datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
AOS=ROOT/'data/runtime/aos-coexistence/rebase-f16d3ced7e6d430eb9b2e11913dac720/merged'
sys.path.insert(0,str(AOS/'src'))
from aos.contracts import Prediction
from aos.lab_external import LabExternalJobCoordinator
spec=importlib.util.spec_from_file_location('aos_lab_test_fixture',AOS/'tests/test_lab_external_jobs.py')
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
async def observe():
    connection=fixture._database();controller=fixture.FakeController();client=fixture.FakeLabClient()
    started=asyncio.Event();release=asyncio.Event()
    class DelayedDecision:
        identity={'deployment_id':'fixture-delayed-start','kind':'deterministic_fixture','real_model':False}
        async def decide(self,state,options):
            started.set();await release.wait()
            return Prediction(selected_option='start',probabilities={o.id:1.0 if o.id=='start' else 0.0 for o in options})
    coordinator=LabExternalJobCoordinator(connection,controller,client,allowed_suites=frozenset({'synthetic.allowed.v1'}),decision_engine=DelayedDecision())
    job=asyncio.create_task(coordinator.start('session-a',fixture._request()))
    await asyncio.wait_for(started.wait(),2)
    before=controller.state()
    await coordinator.stop_for_desktop_takeover('session-a')
    after=controller.control('take-control')
    release.set()
    error=None;result=None
    try: result=await asyncio.wait_for(job,5)
    except Exception as exc: error=type(exc).__name__
    record={'lease_before':before,'lease_after':after,'lab_start_calls_after_lease_change':len(client.start_calls),'exception_type':error,'result_state':result.get('state') if result else None,'persisted_external_jobs':connection.execute('SELECT count(*) FROM aos_external_jobs').fetchone()[0]}
    connection.close();return record
paths=['src/aos/lab_external.py','src/aos/desktop_console.py','scripts/serve_desktop.py','tests/test_lab_external_jobs.py']
hashes={p:hashlib.sha256((AOS/p).read_bytes()).hexdigest() for p in paths}
result={'schema':'aos-lab-start-await-race-review.v1','checked_at':datetime.now(UTC).isoformat(),'scope':'Read-only isolated AOS sources; in-memory real SQLite schema; fixture controller/client/delayed decision; no network, model, desktop, or live AOS calls.','aos_source_root':str(AOS.relative_to(ROOT)),'source_before':hashes,'observation':asyncio.run(observe())}
result['source_unchanged']=all(hashlib.sha256((AOS/p).read_bytes()).hexdigest()==v for p,v in hashes.items())
result['revoked_lease_start_prevented']=result['observation']['lab_start_calls_after_lease_change']==0
result['actual_exit_code']=0 if result['revoked_lease_start_prevented'] else 1
result['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
output=Path(__file__).with_name('aos-lab-start-await-race-before.json');assert not output.exists();output.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
sys.exit(result['actual_exit_code'])
