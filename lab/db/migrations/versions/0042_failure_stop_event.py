"""Atomically bind exception stops to their immutable first-stop event."""

from alembic import op

revision = "0042_failure_stop_event"
down_revision = "0041_zero_job_baseline_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve the RPC identity and grants; never repair historical stop rows."""
    op.execute(r"""
CREATE OR REPLACE FUNCTION lab.close_director_run_if_owned(
 p_run_id uuid,p_generation integer,p_invocation_id text,p_execution_sha256 text,
 p_target_state text,p_error_type text,p_failure_reason text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,lab AS $$
DECLARE run_item lab.runs%ROWTYPE; control_item lab.director_execution_control%ROWTYPE;
 lock_key bigint; event_type text; stop_at timestamptz;
BEGIN
 IF session_user<>'swapp_lab_director' OR p_run_id IS NULL OR
 p_generation IS NULL OR p_generation<1 OR
 p_invocation_id IS NULL OR p_invocation_id !~ '^[0-9a-f]{32}$' OR
 p_execution_sha256 IS NULL OR p_execution_sha256 !~ '^[0-9a-f]{64}$' OR
 p_target_state IS NULL OR p_target_state NOT IN ('failed','stop_requested') OR
 p_error_type IS NULL OR p_error_type !~ '^[A-Za-z0-9_.]{1,128}$' OR
 p_failure_reason IS NULL OR length(p_failure_reason)>160 OR
 p_failure_reason ~ '[^ -~]' THEN
  RAISE EXCEPTION 'Director closure request is malformed';
 END IF;
 -- Share the private lifecycle lock with plan/job admission before row locks.
 PERFORM lab.lock_run_plan(p_run_id);
 lock_key:=('x'||substr(encode(sha256(uuid_send(p_run_id)),'hex'),1,16))::bit(64)::bigint;
 PERFORM pg_advisory_xact_lock(lock_key);
 SELECT * INTO run_item FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
 SELECT * INTO control_item FROM lab.director_execution_control
 WHERE run_id=p_run_id FOR UPDATE;
 IF run_item.run_id IS NULL OR control_item.run_id IS NULL OR
 NOT (run_item.state='running' OR
  (p_target_state='stop_requested' AND run_item.state='stop_requested'
   AND run_item.stop_requested)) OR
 control_item.mode IS DISTINCT FROM 'active' OR
 control_item.current_generation IS DISTINCT FROM p_generation OR
 NOT EXISTS(SELECT 1 FROM lab.director_owner_generations o WHERE o.run_id=p_run_id
  AND o.generation=p_generation AND o.worker_invocation_id=p_invocation_id
  AND o.execution_sha256=p_execution_sha256) OR
 NOT EXISTS(SELECT 1 FROM lab.director_execution_contracts e WHERE e.run_id=p_run_id
  AND e.execution_sha256=p_execution_sha256) THEN
  RAISE EXCEPTION 'Director closure owner is stale or run is inactive';
 END IF;
 IF run_item.state='stop_requested' THEN
  -- Replay only this exact failure-stop request. A legacy missing event, or an
  -- API stop, must never acquire a later first-stop timestamp through this RPC.
  SELECT min(v.created_at) INTO stop_at FROM lab.run_events v WHERE v.run_id=p_run_id
   AND v.event_type='run.stop_requested'
   AND v.event_json=jsonb_build_object('dispatcher','director.v2',
    'error_type',p_error_type,'reason',p_failure_reason,'generation',p_generation,
    'invocation_id',p_invocation_id,'execution_sha256',p_execution_sha256);
  IF stop_at IS NULL THEN RAISE EXCEPTION
   'Director stopped retry lacks original failure-stop event'; END IF;
  RETURN jsonb_build_object('state','stop_requested',
   'event_type','run.dispatch_recovery_required');
 END IF;
 PERFORM set_config('lab.closure_run_id',p_run_id::text,true);
 PERFORM set_config('lab.closure_generation',p_generation::text,true);
 PERFORM set_config('lab.closure_invocation_id',p_invocation_id,true);
 PERFORM set_config('lab.closure_execution_sha256',p_execution_sha256,true);
 stop_at:=clock_timestamp();
 UPDATE lab.runs SET state=p_target_state,stop_requested=(p_target_state='stop_requested'),
  updated_at=stop_at WHERE run_id=p_run_id AND state='running';
 IF p_target_state='stop_requested' THEN
  INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)
  VALUES(gen_random_uuid(),p_run_id,'run.stop_requested',
   jsonb_build_object('dispatcher','director.v2','error_type',p_error_type,
    'reason',p_failure_reason,'generation',p_generation,'invocation_id',p_invocation_id,
    'execution_sha256',p_execution_sha256),stop_at);
 END IF;
 event_type:=CASE WHEN p_target_state='failed' THEN 'run.failed'
  ELSE 'run.dispatch_recovery_required' END;
 INSERT INTO lab.run_events(event_id,run_id,event_type,event_json,created_at)
 VALUES(gen_random_uuid(),p_run_id,event_type,
  jsonb_build_object('dispatcher','director.v2','error_type',p_error_type,
   'reason',p_failure_reason),stop_at);
 RETURN jsonb_build_object('state',p_target_state,'event_type',event_type);
END $$;
""")


def downgrade() -> None:
    raise RuntimeError("failure-stop first-event history cannot be erased")
