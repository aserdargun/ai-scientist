"""Fixed 13-file deployment; inert until independently reviewed execution flag."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from common055_release import (ROOT,SOURCE,HERE,PAYLOADS,UNITS,sha,save,command,
 verify_scope,lock,units,unit_record,container,engine,snapshot,assert_prestate,restore)

def main():
 if sys.argv[1:]!=['--execute-reviewed-deploy']:
  print(json.dumps({'inert':True,'source':str(SOURCE),'files':sorted(PAYLOADS),
   'phases':['verify frozen evidence/source/owned services/DB quiet state','lock CPU+dispatch',
   'stop exact three owned services','private source backup + pg_dump','copy exact13 files',
   'apply only0031 migration','restore normal launcher','verify preserved critical state']})); return 2
 os.umask(0o077)
 record={'schema':'release0363-deployment.v1','scope':verify_scope(),'steps':[]}
 output=HERE/'deployment0363.json'; backup=HERE/'pre-0363-source'; dump=HERE/'pre-0363-ledger.dump'
 assert not output.exists() and not backup.exists() and not dump.exists()
 manifest=json.loads((HERE/'stopped055-validated-overlay.json').read_text())
 assert set(manifest['payloads'])==PAYLOADS and manifest['original_pg_prefix_preserved']
 gate=json.loads((HERE/'gate-bc060ee6d4dd.json').read_text())
 checks=json.loads((HERE/'gate-bc060ee6d4dd-commands.json').read_text())
 pg=json.loads((HERE/'stopped-proposal-055-pg-8a57376c3776.json').read_text())
 image=json.loads((SOURCE/'docs/ai-scientist/review-evidence/stopped-proposal-055-image-214300695c93.json').read_text())
 assert gate['exit_code']==0 and gate['source_unchanged'] and checks['overall_exit_code']==0
 assert all(c['exit_code']==0 for c in checks['commands'])
 assert pg['exit_code']==0 and pg['test_passed_count']==40 and pg['test_execution']['all_collected_tests_passed']
 assert image['all_passed'] and image['source_unchanged'] and image['image']==manifest['image']
 assert (ROOT/'harness/VERSION').read_text().strip()=='0.36.2'
 for name,value in manifest['payloads'].items():
  assert sha(SOURCE/name)==value
  expected=manifest['main_hashes_before_promotion'][name]
  assert (sha(ROOT/name) if (ROOT/name).is_file() else None)==expected, 'concurrent owned source change: '+name
 for name,value in gate['source_after'].items():
  if name.startswith(('harness/','lab/','vendor/')) and name not in PAYLOADS:
   assert sha(ROOT/name)==value, 'concurrent runtime change: '+name
 before=snapshot(); assert_prestate(before,'0030_director_resume')
 record.update(before=before,postgres=container(),manifest_sha256=sha(HERE/'stopped055-validated-overlay.json'))
 locks=[]; stopped=False; copied=False; migrated=False; copy_started=False; old={}
 try:
  locks.append(lock('data/runtime/parallel-m0/cpu-check.lock'))
  locks.append(lock('data/runtime/director-dispatch.lock'))
  identity=units(); record['before_units']=dict(identity)
  assert snapshot()==before and container()==record['postgres']
  for name in UNITS:
   assert unit_record(name)==identity[name]
   command(['systemctl','--user','stop',name],timeout=25); stopped=True
   assert snapshot()==before
   # Remaining active services may be checked independently, never foreign services.
   identity.pop(name)
   for other in identity:
    from ops.start_lab import check_unit
    module,port=UNITS[other]; assert check_unit(other,module,port)['ActiveState']=='active'
  record['steps'].append('three_owned_services_stopped'); save(output,record)
  backup.mkdir(mode=0o700)
  for name in sorted(PAYLOADS):
   target=ROOT/name; assert not target.is_symlink()
   old[name]=sha(target) if target.is_file() else None
   if target.exists():
    (backup/name).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(target,backup/name)
  save(backup/'manifest.json',old)
  with dump.open('xb') as stream:
   dump.chmod(0o600)
   result=subprocess.run(['docker','exec',record['postgres']['id'],'pg_dump','-U','swapp_lab_admin','-d','swapp_lab','-Fc'],
    stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.PIPE,check=False,timeout=60)
   assert result.returncode==0 and dump.stat().st_size>100000
  record['database_backup']={'path':str(dump),'bytes':dump.stat().st_size,'sha256':sha(dump)}
  record['steps'].append('private_source_and_database_backups_complete'); save(output,record)
  copy_started=True
  for name,value in manifest['payloads'].items():
   (ROOT/name).parent.mkdir(parents=True,exist_ok=True); shutil.copy2(SOURCE/name,ROOT/name)
   assert sha(ROOT/name)==value
  copied=True; record['steps'].append('exact13_gated_payloads_copied'); save(output,record)
  bound_migrator=engine(); bound_migrator.dispose()
  assert sha(ROOT/'data/runtime/postgres/migrator.dsn')==before['migrator_dsn_sha256']
  command([str(ROOT/'.venv/bin/python'),'-m','alembic','upgrade','0031_stopped_proposal'],timeout=60,cwd=ROOT,
   env=os.environ|{'LAB_MIGRATOR_DSN_FILE':str(ROOT/'data/runtime/postgres/migrator.dsn')})
  migrated=True
  after=snapshot(); assert_prestate(after,'0031_stopped_proposal')
  assert {k:v for k,v in before.items() if k!='revision'}=={k:v for k,v in after.items() if k!='revision'}
  record['steps'].append('0031_migrated_without_critical_row_changes')
  record['after_units']=restore(); stopped=False
  assert snapshot()==after and container()==record['postgres']
  record.update(status='deployed',version='0.36.3',after=after,migration_applied=True,
   changed_files=manifest['payloads']); save(output,record)
  print(json.dumps({'status':'deployed','version':'0.36.3','files':13,'evidence':str(output)})); return 0
 except BaseException as exc:
  record.update(failure_type=type(exc).__name__,source_copied=copied,migration_confirmed=migrated)
  rollback_source = copy_started and not copied
  restore_allowed = True
  if copied:
   try:
    actual_revision = snapshot()['revision']
    record['failure_database_revision'] = actual_revision
    if actual_revision == '0030_director_resume':
     rollback_source = True
    elif actual_revision == '0031_stopped_proposal':
     record['migration_confirmed_after_command_failure'] = True
    else:
     restore_allowed = False
   except BaseException as revision_error:
    record['failure_revision_read_error_type'] = type(revision_error).__name__
    restore_allowed = False
  if rollback_source:
   for name,value in old.items():
    if value is None: (ROOT/name).unlink(missing_ok=True)
    else:
     shutil.copy2(backup/name,ROOT/name)
     assert sha(ROOT/name)==value
   record['source_copy_rolled_back']=True
  if stopped and restore_allowed:
   try: record['after_restore_units']=restore()
   except BaseException as restore_error: record['restore_failure_type']=type(restore_error).__name__
  elif stopped:
   record['owned_services_left_stopped_for_unknown_database_revision']=True
  save(output,record)
  print(json.dumps({'status':'failed','failure_type':type(exc).__name__,'evidence':str(output)})); return 1
 finally:
  for fd in reversed(locks): os.close(fd)
if __name__=='__main__': sys.exit(main())
