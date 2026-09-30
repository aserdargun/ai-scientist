"""Bounded restart intent, immutable drain evidence, and generation CAS."""

# SQL bodies retain exact replacement strings used to extend existing fenced functions.
# ruff: noqa: E501
from alembic import op

revision = "0030_director_resume"
down_revision = "0029_holdout_registration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Only trusted role RPCs advance resume state; original execution stays immutable."""
    op.execute(r"""
CREATE TABLE lab.director_restart_observations (
 restart_id uuid PRIMARY KEY REFERENCES lab.director_restart_requests(restart_id),
 observation_json jsonb NOT NULL,
 observation_sha256 text NOT NULL CHECK(observation_sha256 ~ '^[0-9a-f]{64}$'),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON lab.director_restart_observations FROM PUBLIC,swapp_lab_director,
 swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_restart_observations TO swapp_lab_director,swapp_lab_scorer;
GRANT SELECT ON lab.director_restart_requests,lab.director_owner_generations,
 lab.director_execution_contracts,lab.director_execution_control TO swapp_lab_scorer;

CREATE OR REPLACE FUNCTION lab.guard_immutable_director_execution_row()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE request lab.director_restart_requests%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' AND session_user='swapp_lab_migrator' AND pg_trigger_depth()>1 THEN
  RETURN OLD;
 END IF;
 IF session_user='swapp_lab_director' AND TG_OP='INSERT' AND
    current_setting('lab.initial_director_claim',true)=NEW.run_id::text THEN RETURN NEW; END IF;
 IF session_user='swapp_lab_director' AND TG_TABLE_NAME IN
    ('director_owner_generations','director_execution_control') AND TG_OP IN ('INSERT','UPDATE') THEN
  SELECT * INTO request FROM lab.director_restart_requests
   WHERE restart_id=nullif(current_setting('lab.resume_claim',true),'')::uuid;
  IF request.state='drained' AND request.run_id=NEW.run_id AND
     EXISTS (SELECT 1 FROM lab.director_restart_observations o WHERE o.restart_id=request.restart_id
       AND o.observation_sha256=request.observation_sha256) THEN
   IF TG_TABLE_NAME='director_owner_generations' THEN
    IF TG_OP='INSERT' AND NEW.generation=request.expected_generation+1 AND
       NEW.restart_id=request.restart_id AND NEW.execution_sha256=request.execution_sha256
       THEN RETURN NEW; END IF;
   ELSIF TG_TABLE_NAME='director_execution_control' THEN
    IF TG_OP='UPDATE' AND OLD.current_generation=request.expected_generation AND
       NEW.current_generation=OLD.current_generation+1 AND OLD.mode='active' AND
       NEW.mode='active' AND NEW.restart_id IS NULL AND NEW.run_id=OLD.run_id
       THEN RETURN NEW; END IF;
   END IF;
  END IF;
 END IF;
 RAISE EXCEPTION 'Director execution identity is immutable outside validated restart CAS';
END $$;

CREATE OR REPLACE FUNCTION lab.guard_director_restart_request()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 IF session_user <> 'swapp_lab_director' OR TG_OP='DELETE' OR
    current_setting('lab.resume_request',true) IS DISTINCT FROM NEW.restart_id::text THEN
  RAISE EXCEPTION 'restart request requires its bounded lifecycle RPC';
 END IF;
 IF TG_OP='UPDATE' AND (
  (to_jsonb(NEW)-ARRAY['state','observation_sha256','claimant_generation','result_sha256','updated_at'])
  IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['state','observation_sha256','claimant_generation','result_sha256','updated_at']) OR
  NOT ((OLD.state='pending' AND NEW.state='drained') OR (OLD.state='drained' AND NEW.state='claimed'))
 ) THEN RAISE EXCEPTION 'restart transition is not monotonic'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION lab.begin_director_resume(p_restart uuid,p_run uuid,p_generation integer,
 p_payload text,p_execution text,p_request text) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab AS $$
DECLARE r lab.runs%ROWTYPE; c lab.director_execution_control%ROWTYPE;
 x lab.director_execution_contracts%ROWTYPE; attempt lab.director_restart_requests%ROWTYPE;
 doc jsonb; request_hash text;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_restart IS NULL OR p_run IS NULL OR
    p_generation IS NULL OR p_generation<1 OR p_generation>3 OR
    p_payload IS NULL OR p_execution IS NULL OR p_request IS NULL OR octet_length(p_request)>16384 THEN
  RAISE EXCEPTION 'resume request identity or bound is invalid';
 END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(p_run)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=p_run FOR UPDATE;
 SELECT * INTO c FROM lab.director_execution_control WHERE run_id=p_run FOR UPDATE;
 SELECT * INTO x FROM lab.director_execution_contracts WHERE run_id=p_run;
 doc:=p_request::jsonb; request_hash:=encode(sha256(convert_to(p_request,'UTF8')),'hex');
 IF r.run_id IS NULL OR r.state IS DISTINCT FROM 'running' OR r.stop_requested OR
    r.report_sha256 IS NOT NULL OR r.payload_sha256 IS DISTINCT FROM p_payload OR
    x.execution_sha256 IS DISTINCT FROM p_execution OR x.payload_sha256 IS DISTINCT FROM p_payload OR
    c.mode IS DISTINCT FROM 'active' OR doc->>'run_id' IS DISTINCT FROM p_run::text OR
    doc->>'expected_generation' IS DISTINCT FROM p_generation::text OR
    doc->>'execution_sha256' IS DISTINCT FROM p_execution OR
    doc->>'payload_sha256' IS DISTINCT FROM p_payload OR
    doc->>'schema' IS DISTINCT FROM 'director-resume-request.v1' THEN
  RAISE EXCEPTION 'resume immutable run identity differs';
 END IF;
 SELECT * INTO attempt FROM lab.director_restart_requests WHERE restart_id=p_restart;
 IF FOUND THEN
  IF (attempt.state<>'claimed' AND attempt.created_at + interval '120 seconds' <= clock_timestamp()) OR
     attempt.run_id IS DISTINCT FROM p_run OR attempt.expected_generation IS DISTINCT FROM p_generation
     OR attempt.request_sha256 IS DISTINCT FROM request_hash OR attempt.execution_sha256 IS DISTINCT FROM p_execution
     OR (attempt.state='claimed' AND c.current_generation IS DISTINCT FROM attempt.claimant_generation)
     OR (attempt.state<>'claimed' AND c.current_generation IS DISTINCT FROM p_generation) THEN
   RAISE EXCEPTION 'restart ID replay changed its immutable identity';
  END IF;
  RETURN to_jsonb(attempt);
 END IF;
 IF c.current_generation IS DISTINCT FROM p_generation OR
    EXISTS(SELECT 1 FROM lab.director_restart_requests WHERE run_id=p_run
      AND expected_generation=p_generation) OR
    (SELECT count(*) FROM lab.director_restart_requests WHERE run_id=p_run)>=3 THEN
  RAISE EXCEPTION 'restart generation already requested or retry bound exhausted';
 END IF;
 IF doc->'prior_owner' IS DISTINCT FROM (
  SELECT jsonb_build_object('worker_pid',worker_pid,'worker_start_ticks',worker_start_ticks,
   'worker_boot_id',worker_boot_id,'worker_unit',worker_unit,'worker_invocation_id',worker_invocation_id,
   'worker_cgroup',worker_cgroup) FROM lab.director_owner_generations
   WHERE run_id=p_run AND generation=p_generation AND execution_sha256=p_execution
 ) THEN RAISE EXCEPTION 'resume prior process identity differs'; END IF;
 PERFORM set_config('lab.resume_request',p_restart::text,true);
 INSERT INTO lab.director_restart_requests(restart_id,run_id,expected_generation,request_sha256,
  purpose,request_json,execution_sha256) VALUES(p_restart,p_run,p_generation,request_hash,'continue',doc,p_execution)
 RETURNING * INTO attempt;
 RETURN to_jsonb(attempt);
END $$;

CREATE FUNCTION lab.record_director_resume_drain(p_restart uuid,p_observation text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_restart_requests%ROWTYPE; doc jsonb; digest text; target uuid;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_observation IS NULL OR octet_length(p_observation)>4194304 THEN
  RAISE EXCEPTION 'resume drain proof is unavailable or too large'; END IF;
 SELECT run_id INTO target FROM lab.director_restart_requests WHERE restart_id=p_restart;
 IF target IS NULL THEN RAISE EXCEPTION 'restart intent missing'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(target)),'hex'),1,16))::bit(64)::bigint);
 PERFORM 1 FROM lab.runs WHERE run_id=target AND state='running' AND NOT stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'run stopped during restart'; END IF;
 SELECT * INTO a FROM lab.director_restart_requests WHERE restart_id=p_restart FOR UPDATE;
 doc:=p_observation::jsonb; digest:=encode(sha256(convert_to(p_observation,'UTF8')),'hex');
 IF a.created_at + interval '120 seconds' <= clock_timestamp() THEN
  RAISE EXCEPTION 'immutable restart cleanup window expired'; END IF;
 IF a.state='drained' THEN
  IF a.observation_sha256 IS DISTINCT FROM digest THEN RAISE EXCEPTION 'immutable drain proof changed'; END IF;
  RETURN digest;
 END IF;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR
    doc->>'schema' IS DISTINCT FROM 'director-resume-drain.v1' OR
    doc->>'restart_id' IS DISTINCT FROM p_restart::text OR doc->>'run_id' IS DISTINCT FROM target::text OR
    doc->>'generation' IS DISTINCT FROM a.expected_generation::text OR
    doc->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 OR
    doc->'prior_owner' IS DISTINCT FROM a.request_json->'prior_owner' OR
    doc->'owner_dead' IS DISTINCT FROM 'true'::jsonb OR doc->'children_drained' IS DISTINCT FROM 'true'::jsonb OR
    doc->'sandbox_drained' IS DISTINCT FROM 'true'::jsonb OR
    jsonb_typeof(doc->'checkpoints') IS DISTINCT FROM 'array' OR
    NOT EXISTS(SELECT 1 FROM lab.director_execution_control WHERE run_id=target AND mode='active'
       AND current_generation=a.expected_generation) OR
    EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=target AND state IN ('queued','running')) OR
    EXISTS(SELECT 1 FROM lab.holdout_reservations WHERE run_id=target AND state IN ('reserved','running')) THEN
  RAISE EXCEPTION 'resume drain proof or durable child state is incomplete';
 END IF;
 INSERT INTO lab.director_restart_observations VALUES(p_restart,doc,digest,clock_timestamp());
 PERFORM set_config('lab.resume_request',p_restart::text,true);
 UPDATE lab.director_restart_requests SET state='drained',observation_sha256=digest,
  updated_at=clock_timestamp() WHERE restart_id=p_restart;
 RETURN digest;
END $$;

CREATE FUNCTION lab.claim_resumed_director(p_restart uuid,p_observation text,p_process text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_restart_requests%ROWTYPE; c lab.director_execution_control%ROWTYPE;
 r lab.runs%ROWTYPE; x lab.director_execution_contracts%ROWTYPE; target uuid; proc jsonb; result jsonb;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_process IS NULL OR octet_length(p_process)>8192 THEN
  RAISE EXCEPTION 'resume claimant must be a bounded Director process'; END IF;
 proc:=p_process::jsonb;
 IF jsonb_typeof(proc) IS DISTINCT FROM 'object' OR NOT (proc ?& ARRAY[
  'payload_sha256','worker_pid','worker_start_ticks','worker_boot_id','worker_unit',
  'worker_invocation_id','worker_cgroup']) OR
  EXISTS(SELECT 1 FROM jsonb_each(proc) kv WHERE kv.value='null'::jsonb) OR
  jsonb_typeof(proc->'worker_pid') IS DISTINCT FROM 'number' OR
  jsonb_typeof(proc->'worker_start_ticks') IS DISTINCT FROM 'number' THEN
  RAISE EXCEPTION 'resume claimant fields are missing or null'; END IF;
 SELECT run_id INTO target FROM lab.director_restart_requests WHERE restart_id=p_restart;
 IF target IS NULL THEN RAISE EXCEPTION 'restart intent missing'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(target)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=target FOR UPDATE;
 SELECT * INTO c FROM lab.director_execution_control WHERE run_id=target FOR UPDATE;
 SELECT * INTO a FROM lab.director_restart_requests WHERE restart_id=p_restart FOR UPDATE;
 SELECT * INTO x FROM lab.director_execution_contracts WHERE run_id=target;
 IF r.state IS DISTINCT FROM 'running' OR r.stop_requested OR r.report_sha256 IS NOT NULL OR
    x.execution_sha256 IS DISTINCT FROM a.execution_sha256 OR
    x.payload_sha256 IS DISTINCT FROM r.payload_sha256 OR c.mode IS DISTINCT FROM 'active' OR
    a.observation_sha256 IS DISTINCT FROM p_observation OR
    NOT EXISTS(SELECT 1 FROM lab.director_restart_observations WHERE restart_id=p_restart
      AND observation_sha256=p_observation) OR
    proc->>'payload_sha256' IS DISTINCT FROM r.payload_sha256 OR
    proc->>'worker_unit' IS DISTINCT FROM
      'swapp-ai-scientist-director-resume-'||replace(target::text,'-','')||'-'||replace(p_restart::text,'-','')||'.service' OR
    (proc->>'worker_pid')::integer <= 1 OR (proc->>'worker_start_ticks')::bigint <= 0 OR
    (proc->>'worker_boot_id') !~ '^[0-9a-f-]{36}$' OR
    (proc->>'worker_invocation_id') !~ '^[0-9a-f]{32}$' OR
    proc->>'worker_cgroup' NOT LIKE '%/'||(proc->>'worker_unit') OR
    proc->>'worker_invocation_id'=a.request_json->'prior_owner'->>'worker_invocation_id' THEN
  RAISE EXCEPTION 'resume claimant or captured execution differs';
 END IF;
 IF a.state='claimed' THEN
  IF c.current_generation IS DISTINCT FROM a.claimant_generation OR NOT EXISTS(
    SELECT 1 FROM lab.director_owner_generations WHERE run_id=target AND generation=a.claimant_generation
     AND worker_invocation_id=proc->>'worker_invocation_id' AND worker_pid=(proc->>'worker_pid')::integer
     AND worker_start_ticks=(proc->>'worker_start_ticks')::bigint
     AND worker_boot_id=proc->>'worker_boot_id' AND worker_cgroup=proc->>'worker_cgroup') THEN
   RAISE EXCEPTION 'claimed restart belongs to another process; request a new generation'; END IF;
 ELSE
  IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
     a.state IS DISTINCT FROM 'drained' OR c.current_generation IS DISTINCT FROM a.expected_generation OR
     EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=target AND state IN ('queued','running')) OR
     EXISTS(SELECT 1 FROM lab.holdout_reservations WHERE run_id=target AND state IN ('reserved','running')) THEN
   RAISE EXCEPTION 'resume generation CAS lost or children are not drained'; END IF;
  PERFORM set_config('lab.resume_claim',p_restart::text,true);
  INSERT INTO lab.director_owner_generations(run_id,generation,execution_sha256,worker_pid,
   worker_start_ticks,worker_boot_id,worker_unit,worker_invocation_id,worker_cgroup,claimed_at,restart_id)
  VALUES(target,a.expected_generation+1,a.execution_sha256,(proc->>'worker_pid')::integer,
   (proc->>'worker_start_ticks')::bigint,proc->>'worker_boot_id',proc->>'worker_unit',
   proc->>'worker_invocation_id',proc->>'worker_cgroup',clock_timestamp(),p_restart);
  UPDATE lab.director_execution_control SET current_generation=a.expected_generation+1,
   updated_at=clock_timestamp() WHERE run_id=target AND current_generation=a.expected_generation;
  PERFORM set_config('lab.resume_request',p_restart::text,true);
  UPDATE lab.director_restart_requests SET state='claimed',claimant_generation=a.expected_generation+1,
   updated_at=clock_timestamp() WHERE restart_id=p_restart;
 END IF;
 RETURN jsonb_build_object('run_id',target,'generation',a.expected_generation+1,
  'worker_invocation_id',proc->>'worker_invocation_id','execution_sha256',a.execution_sha256,
  'started_at',x.started_at,'deadline_at',x.deadline_at,'closure_only',x.deadline_at<=clock_timestamp());
END $$;

CREATE FUNCTION lab.assert_director_receipt_history(p_run uuid,p_current integer,p_invocation text,
 p_execution text,p_historical integer) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab AS $$
DECLARE expected integer;
BEGIN
 PERFORM lab.assert_director_generation_identity(p_run,p_current,p_invocation,p_execution);
 IF p_historical IS NULL OR p_historical<1 OR p_historical>p_current THEN
  RAISE EXCEPTION 'historical receipt generation is invalid'; END IF;
 FOR expected IN p_historical..p_current-1 LOOP
  IF NOT EXISTS(SELECT 1 FROM lab.director_restart_requests a
   JOIN lab.director_restart_observations o USING(restart_id)
   JOIN lab.director_owner_generations g ON g.run_id=a.run_id AND g.generation=a.claimant_generation
   WHERE a.run_id=p_run AND a.expected_generation=expected AND a.claimant_generation=expected+1
    AND a.state='claimed' AND a.execution_sha256=p_execution AND g.execution_sha256=p_execution
    AND o.observation_sha256=a.observation_sha256 AND g.restart_id=a.restart_id) THEN
   RAISE EXCEPTION 'historical receipt has no validated drained ownership chain'; END IF;
 END LOOP;
END $$;
REVOKE ALL ON FUNCTION lab.begin_director_resume(uuid,uuid,integer,text,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.record_director_resume_drain(uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.claim_resumed_director(uuid,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.assert_director_receipt_history(uuid,integer,text,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.begin_director_resume(uuid,uuid,integer,text,text,text),
 lab.record_director_resume_drain(uuid,text),lab.claim_resumed_director(uuid,text,text),
 lab.assert_director_receipt_history(uuid,integer,text,text,integer) TO swapp_lab_director;
""")

    op.execute(r"""
CREATE TABLE lab.director_restart_job_drains(
 restart_id uuid NOT NULL REFERENCES lab.director_restart_requests(restart_id),
 job_id uuid NOT NULL REFERENCES scorer.score_jobs(job_id),
 original_job jsonb NOT NULL,
 recovery_invocation text NOT NULL CHECK(recovery_invocation ~ '^[0-9a-f]{32}$'),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(restart_id,job_id)
);
REVOKE ALL ON lab.director_restart_job_drains FROM PUBLIC,swapp_lab_director,
 swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_restart_job_drains TO swapp_lab_director,swapp_lab_scorer;

CREATE FUNCTION lab.assert_restart_job_transition(p_table text,p_operation text,p_new jsonb,p_old jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_restart_requests%ROWTYPE; j jsonb; target uuid; drain lab.director_restart_job_drains%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer restart identity required'; END IF;
 SELECT * INTO drain FROM lab.director_restart_job_drains
  WHERE restart_id=nullif(current_setting('lab.resume_request',true),'')::uuid
  AND job_id=nullif(current_setting('lab.resume_job',true),'')::uuid;
 SELECT * INTO a FROM lab.director_restart_requests WHERE restart_id=drain.restart_id;
 target:=a.run_id; j:=drain.original_job;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR target IS NULL OR NOT EXISTS(
  SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
   WHERE r.run_id=target AND r.state='running' AND NOT r.stop_requested AND c.mode='active'
    AND c.current_generation=a.expected_generation) OR
  j->>'run_id' IS DISTINCT FROM target::text OR
  j->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text OR
  j->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 THEN
  RAISE EXCEPTION 'restart child transition has no live immutable attempt'; END IF;
 IF p_new IS NULL THEN
  IF p_table IN ('score_jobs','task_terminal_outcomes','task_completions') THEN RETURN; END IF;
 ELSE
  IF p_new->>'run_id' IS DISTINCT FROM j->>'run_id' OR
     p_new->>'experiment_id' IS DISTINCT FROM j->>'experiment_id' OR
     p_new->>'evaluation_kind' IS DISTINCT FROM j->>'evaluation_kind' OR
     p_new->>'task_id' IS DISTINCT FROM j->>'task_id' OR p_new->'seed' IS DISTINCT FROM j->'seed' THEN
   RAISE EXCEPTION 'restart task identity changed'; END IF;
  IF p_table='score_jobs' AND p_operation='UPDATE' AND p_old=j AND
     p_new->>'state'='failed' AND p_new->>'error_code'='scorer_error' AND
     (p_new-ARRAY['state','error_code','updated_at'])=(p_old-ARRAY['state','error_code','updated_at']) THEN RETURN; END IF;
  IF p_table='task_terminal_outcomes' AND p_operation='INSERT' AND
     p_new->>'score_job_id'=drain.job_id::text AND p_new->>'outcome_code'='scorer_error' AND
     p_new->>'candidate_sha256'=j->>'candidate_sha256' AND
     p_new->>'admitted_generation'=a.expected_generation::text AND p_new->>'execution_sha256'=a.execution_sha256 AND
     p_new->>'recovery_invocation_id'=drain.recovery_invocation AND
     p_new->>'worker_invocation_id' IS NOT DISTINCT FROM j->>'claim_invocation_id' AND
     p_new->>'claim_token' IS NULL AND p_new->>'producer_role'='swapp_lab_scorer' THEN RETURN; END IF;
  IF p_table='task_completions' AND p_operation='INSERT' AND p_new->>'completion_kind'='terminal'
     AND EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes t WHERE t.run_id=target
      AND t.score_job_id=drain.job_id AND t.outcome_code='scorer_error'
      AND t.recovery_invocation_id=drain.recovery_invocation) THEN RETURN; END IF;
 END IF;
 RAISE EXCEPTION 'restart may only close the exact drained existing task';
END $$;

CREATE FUNCTION lab.reconcile_restart_score_job(p_restart uuid,p_job uuid,p_expected text,p_recovery text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_restart_requests%ROWTYPE; j scorer.score_jobs%ROWTYPE; target uuid;
BEGIN
 IF session_user <> 'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
    OR p_expected IS NULL OR octet_length(p_expected)>32768 THEN RAISE EXCEPTION 'invalid child drain identity'; END IF;
 SELECT run_id INTO target FROM lab.director_restart_requests WHERE restart_id=p_restart;
 IF target IS NULL THEN RAISE EXCEPTION 'restart intent missing'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(target)),'hex'),1,16))::bit(64)::bigint);
 PERFORM 1 FROM lab.runs WHERE run_id=target AND state='running' AND NOT stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'run stopped during child reconciliation'; END IF;
 SELECT * INTO a FROM lab.director_restart_requests WHERE restart_id=p_restart FOR UPDATE;
 SELECT * INTO j FROM scorer.score_jobs WHERE job_id=p_job FOR UPDATE;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR j.run_id IS DISTINCT FROM target OR
    j.admitted_generation IS DISTINCT FROM a.expected_generation OR j.execution_sha256 IS DISTINCT FROM a.execution_sha256
    OR to_jsonb(j) IS DISTINCT FROM p_expected::jsonb THEN RAISE EXCEPTION 'child changed after exact drain proof'; END IF;
 IF j.state NOT IN ('queued','running') THEN RETURN j.state; END IF;
 IF EXISTS(SELECT 1 FROM scorer.task_completions WHERE run_id=j.run_id AND experiment_id=j.experiment_id
   AND evaluation_kind=j.evaluation_kind AND task_id=j.task_id AND seed=j.seed) THEN
  RAISE EXCEPTION 'unfinished score job conflicts with a committed result'; END IF;
 INSERT INTO lab.director_restart_job_drains(restart_id,job_id,original_job,recovery_invocation)
 VALUES(p_restart,p_job,to_jsonb(j),p_recovery);
 PERFORM set_config('lab.resume_request',p_restart::text,true);
 PERFORM set_config('lab.resume_job',p_job::text,true);
 INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
  candidate_sha256,outcome_code,producer_role,score_job_id,worker_invocation_id,recovery_invocation_id,
  admitted_generation,execution_sha256)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,j.candidate_sha256,'scorer_error',
  'swapp_lab_scorer',j.job_id,j.claim_invocation_id,p_recovery,j.admitted_generation,j.execution_sha256);
 INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,completion_kind)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,'terminal');
 UPDATE scorer.score_jobs SET state='failed',error_code='scorer_error',updated_at=clock_timestamp()
 WHERE job_id=p_job;
 RETURN 'failed';
END $$;
REVOKE ALL ON FUNCTION lab.reconcile_restart_score_job(uuid,uuid,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.assert_restart_job_transition(text,text,jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.reconcile_restart_score_job(uuid,uuid,text,text) TO swapp_lab_scorer;
""")
    # Extend existing trigger bodies in place: all prior fences remain the fallback.
    # The narrow branch independently validates the durable attempt and exact job row.
    for name, statement in (
        ("lab.guard_scorer_owned_statement", True),
        ("lab.guard_scorer_owned_row", False),
        ("scorer.guard_terminal_task_invocation", False),
        ("scorer.capture_terminal_task_completion_v9", False),
    ):
        result = "NULL" if statement else "NEW"
        new = "NULL" if statement else "to_jsonb(NEW)"
        old = "NULL" if statement else "CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) ELSE NULL END"
        branch = f"""BEGIN
 IF session_user='swapp_lab_scorer' AND nullif(current_setting('lab.resume_job',true),'') IS NOT NULL THEN
  PERFORM lab.assert_restart_job_transition(TG_TABLE_NAME,TG_OP,{new},{old});
  RETURN {result};
 END IF;
"""
        # Only literal function names and branch fragments from the tuples above.
        op.execute(f"""DO $rewrite$ DECLARE body text; BEGIN
 SELECT pg_get_functiondef('{name}()'::regprocedure) INTO body;
 IF position('BEGIN' in body)=0 THEN RAISE EXCEPTION 'expected trigger body missing'; END IF;
 body:=overlay(body placing $branch${branch}$branch$ from position('BEGIN' in body) for 5);
 EXECUTE body;
 END $rewrite$;""")  # nosec B608

    op.execute(r"""
ALTER TABLE scorer.task_terminal_outcomes DROP CONSTRAINT ck_task_terminal_outcome_code;
ALTER TABLE scorer.task_terminal_outcomes ADD CONSTRAINT ck_task_terminal_outcome_code CHECK(
 outcome_code IN ('guard_rejected','candidate_rejected','candidate_crash','candidate_timeout',
 'scorer_error','cancelled','infrastructure_unattempted'));
CREATE TABLE lab.director_restart_abandonments(
 run_id uuid NOT NULL REFERENCES lab.runs(run_id),experiment_id text NOT NULL,
 restart_id uuid NOT NULL REFERENCES lab.director_restart_requests(restart_id),
 generation integer NOT NULL,execution_sha256 text NOT NULL,
 PRIMARY KEY(run_id,experiment_id)
);
REVOKE ALL ON lab.director_restart_abandonments FROM PUBLIC,swapp_lab_director,
 swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_restart_abandonments TO swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
CREATE FUNCTION lab.assert_restart_missing_transition(p_table text,p_operation text,p_new jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_restart_abandonments%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_planner' THEN RAISE EXCEPTION 'Planner reconciliation required'; END IF;
 SELECT * INTO a FROM lab.director_restart_abandonments
  WHERE run_id=nullif(current_setting('lab.owner_run_id',true),'')::uuid
   AND experiment_id=current_setting('lab.resume_missing',true);
 PERFORM lab.assert_director_generation_identity(a.run_id,a.generation,
  current_setting('lab.owner_invocation_id',true),a.execution_sha256);
 IF p_table NOT IN ('task_terminal_outcomes','task_completions') OR p_operation<>'INSERT' THEN
  RAISE EXCEPTION 'missing-cell recovery cannot admit or score work'; END IF;
 IF p_new IS NULL THEN RETURN; END IF;
 IF p_new->>'run_id' IS DISTINCT FROM a.run_id::text OR p_new->>'experiment_id' IS DISTINCT FROM a.experiment_id
 OR NOT EXISTS(SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=a.experiment_id
   AND t.evaluation_kind=p_new->>'evaluation_kind' AND t.task_id=p_new->>'task_id' AND t.seed=(p_new->>'seed')::integer)
 OR EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=a.run_id AND j.experiment_id=a.experiment_id
   AND j.evaluation_kind=p_new->>'evaluation_kind' AND j.task_id=p_new->>'task_id' AND j.seed=(p_new->>'seed')::integer)
 THEN RAISE EXCEPTION 'unattempted cell is not a missing planned task'; END IF;
 IF p_table='task_terminal_outcomes' AND (p_new->>'outcome_code' IS DISTINCT FROM 'infrastructure_unattempted'
  OR p_new->>'producer_role' IS DISTINCT FROM 'swapp_lab_planner' OR p_new->>'score_job_id' IS NOT NULL
  OR p_new->>'claim_token' IS NOT NULL OR p_new->>'worker_invocation_id' IS NOT NULL
  OR p_new->>'recovery_invocation_id' IS NOT NULL OR p_new->>'admitted_generation' IS DISTINCT FROM a.generation::text
  OR p_new->>'execution_sha256' IS DISTINCT FROM a.execution_sha256) THEN
  RAISE EXCEPTION 'unattempted terminal outcome is malformed'; END IF;
 IF p_table='task_completions' AND (p_new->>'completion_kind' IS DISTINCT FROM 'terminal' OR NOT EXISTS(
  SELECT 1 FROM scorer.task_terminal_outcomes t WHERE t.run_id=a.run_id AND t.experiment_id=a.experiment_id
   AND t.evaluation_kind=p_new->>'evaluation_kind' AND t.task_id=p_new->>'task_id'
   AND t.seed=(p_new->>'seed')::integer AND t.outcome_code='infrastructure_unattempted')) THEN
  RAISE EXCEPTION 'unattempted completion requires an actual terminal outcome'; END IF;
END $$;
CREATE FUNCTION lab.close_restart_unattempted_tasks(p_run uuid,p_generation integer,p_invocation text,
 p_execution text,p_experiment text,p_evidence text) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
DECLARE restart uuid; cell scorer.run_tasks%ROWTYPE; evidence jsonb; e jsonb;
BEGIN
 IF session_user<>'swapp_lab_planner' THEN RAISE EXCEPTION 'Planner recovery required'; END IF;
 PERFORM lab.assert_director_generation_identity(p_run,p_generation,p_invocation,p_execution);
 SELECT a.restart_id INTO restart FROM lab.director_restart_requests a
 JOIN lab.director_restart_job_drains d USING(restart_id)
 JOIN scorer.score_jobs j ON j.job_id=d.job_id
 WHERE a.run_id=p_run AND a.state='claimed' AND a.claimant_generation<=p_generation
  AND a.execution_sha256=p_execution AND j.experiment_id=p_experiment
  AND j.state='failed' AND j.error_code='scorer_error'
 ORDER BY a.claimant_generation DESC LIMIT 1;
 IF restart IS NULL AND p_evidence IS NOT NULL THEN
  IF octet_length(p_evidence)>1048576 THEN RAISE EXCEPTION 'evaluation recovery evidence exceeds bound'; END IF;
  evidence:=p_evidence::jsonb;e:=evidence->'payload';
  IF (evidence->>'payload_text')::jsonb IS DISTINCT FROM e OR
   encode(sha256(convert_to(evidence->>'payload_text','UTF8')),'hex') IS DISTINCT FROM
    evidence->'receipt'->>'payload_sha256' OR
   e->>'schema' IS DISTINCT FROM 'director-evaluation-evidence.v1' OR
   e->>'run_id' IS DISTINCT FROM p_run::text OR e->>'experiment_id' IS DISTINCT FROM p_experiment OR
   e->>'execution_sha256' IS DISTINCT FROM p_execution THEN
   RAISE EXCEPTION 'interrupted evaluation evidence identity differs'; END IF;
  SELECT a.restart_id INTO restart FROM lab.director_restart_requests a
   JOIN lab.director_restart_observations o USING(restart_id)
   WHERE a.run_id=p_run AND a.state='claimed' AND a.claimant_generation=p_generation
    AND a.expected_generation=(e->>'admitted_generation')::integer AND a.execution_sha256=p_execution
    AND o.observation_json->'checkpoints' @> jsonb_build_array(jsonb_build_object(
     'key',evidence->'receipt'->>'key','payload_sha256',evidence->'receipt'->>'payload_sha256',
     'sequence',evidence->'receipt'->'sequence'))
    AND EXISTS(SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=p_run AND t.experiment_id=p_experiment
     AND t.evaluation_kind=e->>'evaluation_kind' AND t.task_id=e->>'task_id' AND t.seed=(e->>'seed')::integer
     AND t.candidate_sha256=e->>'candidate_sha256')
    AND NOT EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=p_run AND j.experiment_id=p_experiment
     AND j.evaluation_kind=e->>'evaluation_kind' AND j.task_id=e->>'task_id' AND j.seed=(e->>'seed')::integer);
 END IF;
 IF restart IS NULL THEN RETURN false; END IF;
 INSERT INTO lab.director_restart_abandonments VALUES(p_run,p_experiment,restart,p_generation,p_execution)
 ON CONFLICT DO NOTHING;
 PERFORM set_config('lab.resume_missing',p_experiment,true);
 FOR cell IN SELECT t.* FROM scorer.run_tasks t WHERE t.run_id=p_run AND t.experiment_id=p_experiment
  AND NOT EXISTS(SELECT 1 FROM scorer.task_completions c WHERE c.run_id=t.run_id AND c.experiment_id=t.experiment_id
   AND c.evaluation_kind=t.evaluation_kind AND c.task_id=t.task_id AND c.seed=t.seed)
  AND NOT EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=t.run_id AND j.experiment_id=t.experiment_id
   AND j.evaluation_kind=t.evaluation_kind AND j.task_id=t.task_id AND j.seed=t.seed)
 LOOP
  INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
   candidate_sha256,outcome_code,producer_role,admitted_generation,execution_sha256)
  VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,cell.candidate_sha256,
   'infrastructure_unattempted','swapp_lab_planner',p_generation,p_execution);
  INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,completion_kind)
  VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,'terminal');
 END LOOP;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION lab.assert_restart_missing_transition(text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.close_restart_unattempted_tasks(uuid,integer,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.close_restart_unattempted_tasks(uuid,integer,text,text,text,text) TO swapp_lab_planner;
""")
    for name, statement in (
        ("lab.guard_scorer_owned_statement", True),
        ("lab.guard_scorer_owned_row", False),
        ("scorer.guard_terminal_task_invocation", False),
        ("scorer.capture_terminal_task_completion_v9", False),
    ):
        result = "NULL" if statement else "NEW"
        new = "NULL" if statement else "to_jsonb(NEW)"
        branch = f"""BEGIN
 IF session_user='swapp_lab_planner' AND nullif(current_setting('lab.resume_missing',true),'') IS NOT NULL THEN
  PERFORM lab.assert_restart_missing_transition(TG_TABLE_NAME,TG_OP,{new});
  RETURN {result};
 END IF;
"""
        # Only literal function names and branch fragments from the tuples above.
        op.execute(f"""DO $rewrite$ DECLARE body text; BEGIN
 SELECT pg_get_functiondef('{name}()'::regprocedure) INTO body;
 body:=overlay(body placing $branch${branch}$branch$ from position('BEGIN' in body) for 5);
 EXECUTE body;
 END $rewrite$;""")  # nosec B608

    op.execute(r"""
CREATE TABLE lab.director_resume_holdout_suffixes(
 restart_id uuid NOT NULL REFERENCES lab.director_restart_requests(restart_id),
 reservation_id uuid NOT NULL REFERENCES lab.holdout_reservations(reservation_id),
 bundle_sha256 text NOT NULL,bundle_json jsonb NOT NULL,
 PRIMARY KEY(restart_id,reservation_id)
);
REVOKE ALL ON lab.director_resume_holdout_suffixes FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_resume_holdout_suffixes TO swapp_lab_director,swapp_lab_scorer;
CREATE FUNCTION lab.append_resumed_holdout_suffix(p_bundle text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE b jsonb; a jsonb; s jsonb; m jsonb; prior jsonb; origin jsonb; before_budget jsonb;
 after_budget jsonb; episode jsonb; item jsonb; existing jsonb; entry jsonb; digest text;
 r lab.holdout_reservations%ROWTYPE; attempt lab.director_restart_requests%ROWTYPE;
 observation jsonb; key text; next_sequence integer; original_sequence integer; row_count integer;
BEGIN
 IF session_user<>'swapp_lab_director' OR p_bundle IS NULL OR octet_length(p_bundle)>8388608 THEN
  RAISE EXCEPTION 'holdout replay bundle role or bound is invalid'; END IF;
 b:=p_bundle::jsonb; digest:=encode(sha256(convert_to(p_bundle,'UTF8')),'hex');
 IF b->>'schema' IS DISTINCT FROM 'director-holdout-replay-bundle.v1' OR
    jsonb_array_length(b->'entries') IS DISTINCT FROM 3 THEN
  RAISE EXCEPTION 'holdout replay permits exactly application/state/marker'; END IF;
 PERFORM lab.assert_director_generation_identity((b->>'run_id')::uuid,
  (b->>'writer_generation')::integer,b->>'writer_invocation',b->>'execution_sha256');
 SELECT * INTO attempt FROM lab.director_restart_requests WHERE restart_id=(b->>'restart_id')::uuid;
 SELECT observation_json INTO observation FROM lab.director_restart_observations WHERE restart_id=attempt.restart_id;
 IF attempt.state IS DISTINCT FROM 'claimed' OR attempt.run_id::text IS DISTINCT FROM b->>'run_id'
  OR attempt.claimant_generation::text IS DISTINCT FROM b->>'writer_generation'
  OR attempt.created_at+interval '120 seconds'<=clock_timestamp() THEN
  RAISE EXCEPTION 'holdout replay lacks a current bounded restart claim'; END IF;
 SELECT * INTO r FROM lab.holdout_reservations WHERE reservation_id=(b->>'reservation_id')::uuid;
 PERFORM lab.assert_director_receipt_history(attempt.run_id,attempt.claimant_generation,
  b->>'writer_invocation',b->>'execution_sha256',r.admitted_generation);
 IF r.run_id IS DISTINCT FROM attempt.run_id OR r.state NOT IN ('passed','reverted','failed','exhausted')
  OR r.execution_sha256 IS DISTINCT FROM attempt.execution_sha256 OR
  (r.state IN ('passed','reverted') AND NOT EXISTS(SELECT 1 FROM scorer.holdout_results h
   WHERE h.reservation_id=r.reservation_id AND h.result_sha256=r.result_sha256)) THEN
  RAISE EXCEPTION 'holdout replay lacks an exact existing terminal Scorer result'; END IF;
 a:=b->'entries'->0->'payload';s:=b->'entries'->1->'payload';m:=b->'entries'->2->'payload';
 IF b->'entries'->0->'receipt'->>'phase' IS DISTINCT FROM 'holdout_application' OR
 b->'entries'->1->'receipt'->>'phase' IS DISTINCT FROM 'director_loop_state' OR
 b->'entries'->1->'receipt'->>'key' IS DISTINCT FROM
  ('director-state:'||(s->>'completed_proposals')||
   CASE WHEN (s->>'holdout_applied_keep_count')::integer>0
    THEN ':'||'holdout:'||(s->>'holdout_applied_keep_count') ELSE '' END||':'||r.trigger_kind) OR
 b->'entries'->2->'receipt'->>'phase' IS DISTINCT FROM 'holdout_state_application' OR
 b->'entries'->0->'receipt'->>'key' IS DISTINCT FROM 'holdout-application:'||r.trigger_kind||':'||r.trigger_index OR
 b->'entries'->2->'receipt'->>'key' IS DISTINCT FROM 'holdout-state-application:'||r.trigger_kind||':'||r.trigger_index OR
 a->>'run_id' IS DISTINCT FROM attempt.run_id::text OR s->>'run_id' IS DISTINCT FROM attempt.run_id::text OR
 m->>'run_id' IS DISTINCT FROM attempt.run_id::text THEN
 RAISE EXCEPTION 'holdout replay entries are outside the exact three-key suffix'; END IF;

 prior:=(b->>'prior_state_text')::jsonb; origin:=(b->>'budget_origin_text')::jsonb;
 IF prior IS DISTINCT FROM b->'prior_state'->'payload' OR origin IS DISTINCT FROM b->'budget_origin'->'payload'
 OR encode(sha256(convert_to(b->>'prior_state_text','UTF8')),'hex') IS DISTINCT FROM b->'prior_state'->'receipt'->>'payload_sha256'
 OR encode(sha256(convert_to(b->>'budget_origin_text','UTF8')),'hex') IS DISTINCT FROM b->'budget_origin'->'receipt'->>'payload_sha256'
 OR NOT observation->'checkpoints' @> jsonb_build_array(jsonb_build_object(
  'key',b->'prior_state'->'receipt'->>'key','payload_sha256',b->'prior_state'->'receipt'->>'payload_sha256',
  'sequence',b->'prior_state'->'receipt'->'sequence'))
 OR NOT observation->'checkpoints' @> jsonb_build_array(jsonb_build_object(
  'key',b->'budget_origin'->'receipt'->>'key','payload_sha256',b->'budget_origin'->'receipt'->>'payload_sha256',
  'sequence',b->'budget_origin'->'receipt'->'sequence')) THEN
  RAISE EXCEPTION 'holdout replay prior state/budget was not in verified drain inventory'; END IF;
 IF a->>'reservation_id' IS DISTINCT FROM r.reservation_id::text OR
 a->>'admitted_generation' IS DISTINCT FROM r.admitted_generation::text OR
 a->>'execution_sha256' IS DISTINCT FROM r.execution_sha256 OR a->>'state' IS DISTINCT FROM r.state OR
 nullif(a->'bit','null'::jsonb) IS DISTINCT FROM to_jsonb(r.result_bit) OR
 a->>'trigger_kind' IS DISTINCT FROM r.trigger_kind OR a->>'trigger_index' IS DISTINCT FROM r.trigger_index::text OR
 a->>'candidate_experiment_id' IS DISTINCT FROM r.candidate_experiment_id OR
 a->>'prior_state_sha256' IS DISTINCT FROM b->'prior_state'->'receipt'->>'payload_sha256' OR
 prior->>'champion_experiment_id' IS DISTINCT FROM r.candidate_experiment_id OR
 origin->>'trigger_kind' IS DISTINCT FROM r.trigger_kind OR origin->>'trigger_index' IS DISTINCT FROM r.trigger_index::text OR
 m->>'application_sha256' IS DISTINCT FROM b->'entries'->0->'receipt'->>'payload_sha256' OR
 m->>'state_sha256' IS DISTINCT FROM b->'entries'->1->'receipt'->>'payload_sha256' OR
 ((a ? 'expected_state_sha256' OR (r.trigger_kind='run_end' AND r.state='failed')) AND
  a->>'expected_state_sha256' IS DISTINCT FROM b->'entries'->1->'receipt'->>'payload_sha256') OR
 s->'budget' IS DISTINCT FROM a->'budget_snapshot' THEN
  RAISE EXCEPTION 'holdout replay suffix identity differs from its historical receipt'; END IF;
 before_budget:=origin->'budget';after_budget:=a->'budget_snapshot';
 SELECT value INTO episode FROM jsonb_array_elements(before_budget->'reservations')
  WHERE value->>'reservation_id'=origin->>'reservation_id';
 IF episode IS NULL OR after_budget->>'proposal_count' IS DISTINCT FROM before_budget->>'proposal_count'
 OR after_budget->>'model_tokens' IS DISTINCT FROM before_budget->>'model_tokens'
 OR after_budget->>'reserved_model_tokens' IS DISTINCT FROM before_budget->>'reserved_model_tokens'
 OR (after_budget->>'reserved_wall_seconds')::numeric IS DISTINCT FROM
    (before_budget->>'reserved_wall_seconds')::numeric-(episode->>'wall_seconds')::numeric
 OR after_budget->'reservations' IS DISTINCT FROM (SELECT coalesce(jsonb_agg(value),'[]'::jsonb)
    FROM jsonb_array_elements(before_budget->'reservations') WHERE value<>episode)
 OR (after_budget->>'elapsed_wall_seconds')::numeric<(before_budget->>'elapsed_wall_seconds')::numeric
 THEN RAISE EXCEPTION 'holdout replay reset counters or changed another reservation'; END IF;
 IF b->'budget_reconciled' IS NULL OR b->'budget_reconciled'='null'::jsonb THEN
  IF (after_budget->>'wall_seconds')::numeric IS DISTINCT FROM
   (before_budget->>'wall_seconds')::numeric+(episode->>'wall_seconds')::numeric THEN
   RAISE EXCEPTION 'unobserved holdout time must consume the complete existing reservation'; END IF;
 ELSE
  IF b->'budget_reconciled'->'payload'->'budget' IS DISTINCT FROM after_budget OR NOT EXISTS(
   SELECT 1 FROM lab.run_events WHERE run_id=attempt.run_id AND event_type='director.checkpoint'
    AND event_json=b->'budget_reconciled'->'receipt') THEN
   RAISE EXCEPTION 'holdout budget reconciliation is not its exact existing checkpoint'; END IF;
 END IF;
 IF (s-ARRAY['budget','holdout_approved_snapshot','holdout_last_status','holdout_applied_keep_count',
 'holdout_checks_passed','holdout_quota_exhausted','recent_feedback','champion_experiment_id',
 'champion_source_sha256','champion_tree_sha256','champion_source_blob_sha256','champion_seed0_by_task',
 'champion_seed1_by_task','champion_suite_seed_scores','champion_noise_sd','best_suite']) IS DISTINCT FROM
 (prior-ARRAY['budget','holdout_approved_snapshot','holdout_last_status','holdout_applied_keep_count',
 'holdout_checks_passed','holdout_quota_exhausted','recent_feedback','champion_experiment_id',
 'champion_source_sha256','champion_tree_sha256','champion_source_blob_sha256','champion_seed0_by_task',
 'champion_seed1_by_task','champion_suite_seed_scores','champion_noise_sd','best_suite']) THEN
  RAISE EXCEPTION 'holdout replay cannot advance proposal strategy or execution identity'; END IF;

 IF s->>'holdout_applied_keep_count' IS DISTINCT FROM
  (CASE WHEN r.trigger_kind='keep_interval' THEN r.trigger_index::text
   ELSE prior->>'holdout_applied_keep_count' END) OR
 s->'holdout_quota_exhausted' IS DISTINCT FROM
  (CASE WHEN r.state='exhausted' THEN 'true'::jsonb ELSE prior->'holdout_quota_exhausted' END) OR
 (r.state<>'passed' AND s->'holdout_approved_snapshot' IS DISTINCT FROM prior->'holdout_approved_snapshot') THEN
 RAISE EXCEPTION 'holdout replay changed approval or trigger accounting'; END IF;
 IF r.state='passed' THEN
  FOREACH key IN ARRAY ARRAY['experiment_id','source_sha256','tree_sha256','source_blob_sha256',
   'seed0_by_task','seed1_by_task','suite_seed_scores','noise_sd'] LOOP
   IF s->'holdout_approved_snapshot'->key IS DISTINCT FROM prior->('champion_'||key) THEN
    RAISE EXCEPTION 'passed receipt approval differs from its measured champion'; END IF;
  END LOOP;
  IF s->'holdout_approved_snapshot'->'best_suite' IS DISTINCT FROM prior->'best_suite' OR
   s->'holdout_approved_snapshot'->>'periodic_keep_watermark' IS DISTINCT FROM s->>'holdout_applied_keep_count' THEN
   RAISE EXCEPTION 'passed receipt approval counters differ'; END IF;
 END IF;
 IF s->'best_suite' IS DISTINCT FROM (CASE WHEN r.state IN ('reverted','failed')
  THEN prior->'holdout_approved_snapshot'->'best_suite' ELSE prior->'best_suite' END) OR
 s->>'holdout_last_status' IS DISTINCT FROM (CASE WHEN r.state='exhausted' THEN 'quota_exhausted' ELSE r.state END) OR
 (s->>'holdout_checks_passed')::integer IS DISTINCT FROM (prior->>'holdout_checks_passed')::integer+
  (CASE WHEN r.state='passed' THEN 1 ELSE 0 END) THEN
 RAISE EXCEPTION 'holdout replay state counters differ from the existing result'; END IF;
 FOREACH key IN ARRAY ARRAY['experiment_id','source_sha256','tree_sha256','source_blob_sha256',
  'seed0_by_task','seed1_by_task','suite_seed_scores','noise_sd'] LOOP
  IF s->('champion_'||key) IS DISTINCT FROM (CASE WHEN r.state IN ('reverted','failed')
   THEN prior->'holdout_approved_snapshot'->key ELSE prior->('champion_'||key) END) THEN
   RAISE EXCEPTION 'holdout replay champion differs from its exact approved or current snapshot'; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM lab.run_events e WHERE e.run_id=attempt.run_id AND e.event_type='director.checkpoint'
  AND e.event_json->>'phase'='director_loop_state' AND
  (e.event_json->>'sequence')::integer>(b->'prior_state'->'receipt'->>'sequence')::integer
  AND e.event_json->>'payload_sha256' IS DISTINCT FROM b->'entries'->1->'receipt'->>'payload_sha256') THEN
  RAISE EXCEPTION 'later state conflicts with holdout replay prefix'; END IF;
 SELECT bundle_json INTO existing FROM lab.director_resume_holdout_suffixes
 WHERE restart_id=attempt.restart_id AND reservation_id=r.reservation_id;
 IF FOUND AND existing IS DISTINCT FROM b THEN RAISE EXCEPTION 'immutable replay bundle changed'; END IF;
 INSERT INTO lab.director_resume_holdout_suffixes VALUES(attempt.restart_id,r.reservation_id,digest,b)
 ON CONFLICT DO NOTHING;
 PERFORM set_config('lab.resume_holdout_suffix',attempt.restart_id::text,true);
 SELECT coalesce(max((event_json->>'sequence')::integer),-1)+1 INTO next_sequence FROM lab.run_events
 WHERE run_id=attempt.run_id AND event_type='director.checkpoint';
 FOR entry IN SELECT value FROM jsonb_array_elements(b->'entries') LOOP
  IF (entry->>'payload_text')::jsonb IS DISTINCT FROM entry->'payload' OR
   encode(sha256(convert_to(entry->>'payload_text','UTF8')),'hex') IS DISTINCT FROM entry->'receipt'->>'payload_sha256'
   OR entry->'receipt'->>'blob_sha256' IS DISTINCT FROM entry->'receipt'->>'payload_sha256' THEN
   RAISE EXCEPTION 'holdout suffix payload hash is invalid'; END IF;
  SELECT event_json INTO existing FROM lab.run_events WHERE event_id=(entry->>'event_id')::uuid;
  IF FOUND THEN
   IF existing IS DISTINCT FROM entry->'receipt' THEN RAISE EXCEPTION 'existing holdout suffix conflicts'; END IF;
  ELSE
   IF (entry->'receipt'->>'sequence')::integer IS DISTINCT FROM next_sequence THEN
    RAISE EXCEPTION 'holdout suffix sequence changed'; END IF;
   INSERT INTO lab.run_events(event_id,run_id,event_type,event_json)
   VALUES((entry->>'event_id')::uuid,attempt.run_id,'director.checkpoint',entry->'receipt');
   next_sequence:=next_sequence+1;
  END IF;
 END LOOP;
 RETURN digest;
END $$;
REVOKE ALL ON FUNCTION lab.append_resumed_holdout_suffix(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.append_resumed_holdout_suffix(text) TO swapp_lab_director;
""")
    op.execute(r"""DO $rewrite$ DECLARE body text; BEGIN
 SELECT pg_get_functiondef('lab.guard_director_checkpoint_insert()'::regprocedure) INTO body;
 body:=overlay(body placing $branch$BEGIN
 IF session_user='swapp_lab_director' AND nullif(current_setting('lab.resume_holdout_suffix',true),'') IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM lab.director_resume_holdout_suffixes b,
    jsonb_array_elements(b.bundle_json->'entries') e WHERE b.restart_id=
    nullif(current_setting('lab.resume_holdout_suffix',true),'')::uuid
    AND e->>'event_id'=NEW.event_id::text AND e->'receipt'=NEW.event_json
    AND b.bundle_json->>'run_id'=NEW.run_id::text AND NEW.event_type='director.checkpoint') THEN
   RAISE EXCEPTION 'checkpoint is outside the exact approved three-entry replay suffix'; END IF;
  RETURN NEW;
 END IF;
$branch$ from position('BEGIN' in body) for 5);
 EXECUTE body;
 END $rewrite$;""")

    op.execute(r"""
CREATE FUNCTION lab.valid_director_restart_ancestry(p_run uuid,p_historical integer,p_current integer,p_execution text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
DECLARE expected integer;
BEGIN
 IF p_historical IS NULL OR p_current IS NULL OR p_historical<1 OR p_historical>p_current OR
  NOT EXISTS(SELECT 1 FROM lab.director_owner_generations WHERE run_id=p_run
   AND generation=p_historical AND execution_sha256=p_execution) THEN RETURN false; END IF;
 FOR expected IN p_historical..p_current-1 LOOP
  IF NOT EXISTS(SELECT 1 FROM lab.director_restart_requests a JOIN lab.director_restart_observations o USING(restart_id)
   JOIN lab.director_owner_generations g ON g.run_id=a.run_id AND g.generation=a.claimant_generation
   WHERE a.run_id=p_run AND a.expected_generation=expected AND a.claimant_generation=expected+1
    AND a.state='claimed' AND a.execution_sha256=p_execution AND g.execution_sha256=p_execution
    AND o.observation_sha256=a.observation_sha256 AND g.restart_id=a.restart_id) THEN RETURN false; END IF;
 END LOOP;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION lab.valid_director_restart_ancestry(uuid,integer,integer,text) FROM PUBLIC;
""")
    replacements = {
        "lab.prepare_terminal_finalization(uuid,integer,text,text,text,text,text,text)": [
            (
                " SELECT event_json INTO ae FROM lab.run_events WHERE run_id=p_run",
                """
 PERFORM lab.assert_director_receipt_history(p_run,p_generation,p_invocation,p_execution,
  (a->>'admitted_generation')::integer);
 IF (a->>'admitted_generation')::integer<p_generation AND NOT EXISTS(
  SELECT 1 FROM lab.director_restart_requests z JOIN lab.director_restart_observations o USING(restart_id)
  WHERE z.run_id=p_run AND z.claimant_generation=p_generation AND z.state='claimed' AND
   ((o.observation_json->'checkpoints' @> jsonb_build_array(jsonb_build_object('payload_sha256',ah),
     jsonb_build_object('payload_sha256',sh),jsonb_build_object('payload_sha256',mh))) OR EXISTS(
    SELECT 1 FROM lab.director_resume_holdout_suffixes b WHERE b.restart_id=z.restart_id
     AND b.bundle_json->'entries'->0->'receipt'->>'payload_sha256'=ah
     AND b.bundle_json->'entries'->1->'receipt'->>'payload_sha256'=sh
     AND b.bundle_json->'entries'->2->'receipt'->>'payload_sha256'=mh))) THEN
  RAISE EXCEPTION 'historical terminal chain is outside validated restart inventory'; END IF;
 SELECT event_json INTO ae FROM lab.run_events WHERE run_id=p_run""",
            ),
            (
                "a->>'admitted_generation' IS DISTINCT FROM p_generation::text",
                "a->>'admitted_generation' IS NULL",
            ),
            (
                "reservation.admitted_generation IS\n    DISTINCT FROM p_generation",
                "reservation.admitted_generation IS\n    DISTINCT FROM (a->>'admitted_generation')::integer",
            ),
            (
                "old.generation IS DISTINCT FROM p_generation OR old.invocation_id IS DISTINCT FROM\n    p_invocation",
                "NOT lab.valid_director_restart_ancestry(p_run,old.generation,p_generation,p_execution) OR\n    (old.generation=p_generation AND old.invocation_id IS DISTINCT FROM p_invocation)",
            ),
        ],
        "lab.verify_terminal_finalization(uuid,integer,text)": [
            (
                "f.generation IS DISTINCT FROM p_generation",
                "NOT lab.valid_director_restart_ancestry(p_run,f.generation,p_generation,p_execution)",
            ),
            ("c.current_generation=f.generation", "c.current_generation=p_generation"),
            (
                "o.worker_invocation_id=f.invocation_id",
                "EXISTS(SELECT 1 FROM lab.director_owner_generations historical WHERE historical.run_id=p_run\n         AND historical.generation=f.generation AND historical.worker_invocation_id=f.invocation_id)",
            ),
        ],
    }
    for signature, changes in replacements.items():
        for old, new in changes:
            op.execute(f"""DO $rewrite$ DECLARE body text; BEGIN
 SELECT pg_get_functiondef('{signature}'::regprocedure) INTO body;
 IF position($old${old}$old$ in body)=0 THEN RAISE EXCEPTION 'expected 0037 finalization body differs'; END IF;
 body:=replace(body,$old${old}$old$,$new${new}$new$);
 EXECUTE body;
 END $rewrite$;""")

    op.execute(r"""
CREATE FUNCTION lab.assert_scorer_report_receipt_ancestry(
 p_run uuid,p_generation integer,p_execution text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE item record; state text; pairs jsonb:='[]'::jsonb;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_run IS NULL OR p_generation IS NULL
    OR p_execution IS NULL THEN RAISE EXCEPTION 'report ancestry requires current Scorer identity'; END IF;
 SELECT r.state INTO state FROM lab.runs r WHERE r.run_id=p_run;
 IF state='stop_requested' THEN
  PERFORM lab.assert_scorer_run_stop_execution(p_run,p_generation,p_execution);
 ELSE
  PERFORM lab.assert_scorer_report_execution(p_run,p_generation,p_execution);
 END IF;
 FOR item IN SELECT admitted_generation,execution_sha256 FROM (
   SELECT j.admitted_generation,j.execution_sha256 FROM scorer.score_jobs j WHERE j.run_id=p_run
   UNION
   SELECT t.admitted_generation,t.execution_sha256 FROM scorer.task_terminal_outcomes t
    WHERE t.run_id=p_run
 ) receipts ORDER BY admitted_generation,execution_sha256 LOOP
  IF item.admitted_generation IS NULL OR item.execution_sha256 IS DISTINCT FROM p_execution
    OR NOT lab.valid_director_restart_ancestry(p_run,item.admitted_generation,p_generation,p_execution) THEN
   RAISE EXCEPTION 'report receipt lacks exact execution and validated restart ancestry';
  END IF;
  pairs:=pairs||jsonb_build_array(jsonb_build_object('admitted_generation',item.admitted_generation,
    'execution_sha256',item.execution_sha256));
 END LOOP;
 IF jsonb_array_length(pairs)=0 THEN RAISE EXCEPTION 'report ancestry has no durable receipt'; END IF;
 RETURN jsonb_build_object('run_id',p_run,'admitted_generation',p_generation,
   'execution_sha256',p_execution,'receipt_execution_pairs',pairs);
END $$;
REVOKE ALL ON FUNCTION lab.assert_scorer_report_receipt_ancestry(uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.assert_scorer_report_receipt_ancestry(uuid,integer,text) TO swapp_lab_scorer;
""")

    op.execute(r"""
CREATE TABLE lab.director_stop_closures(
 recovery_id uuid PRIMARY KEY REFERENCES lab.director_recoveries(recovery_id),
 run_id uuid NOT NULL UNIQUE REFERENCES lab.runs(run_id), expected_generation integer NOT NULL,
 execution_sha256 text NOT NULL, owner_json jsonb NOT NULL, state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','drained')),
 recovery_invocation text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lab.director_stop_job_drains(
 recovery_id uuid NOT NULL REFERENCES lab.director_stop_closures(recovery_id),
 job_id uuid NOT NULL REFERENCES scorer.score_jobs(job_id),original_job jsonb NOT NULL,
 recovery_invocation text NOT NULL CHECK(recovery_invocation ~ '^[0-9a-f]{32}$'),
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(recovery_id,job_id)
);
REVOKE ALL ON lab.director_stop_closures,lab.director_stop_job_drains FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_stop_closures,lab.director_stop_job_drains TO swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
CREATE FUNCTION lab.begin_stopped_baseline_closure(p_stop uuid) RETURNS double precision
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.director_recoveries%ROWTYPE; g lab.director_owner_generations%ROWTYPE;
 a lab.director_stop_closures%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_stop IS NULL THEN RAISE EXCEPTION 'Director stop identity required'; END IF;
 SELECT * INTO r FROM lab.director_recoveries WHERE recovery_id=p_stop;
 IF r.run_id IS NULL OR r.action IS DISTINCT FROM 'stop_and_finalize' OR
    r.state NOT IN ('started','pending') THEN RAISE EXCEPTION 'stop recovery intent missing'; END IF;
 PERFORM lab.lock_run_plan(r.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=r.run_id AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'stop closure requires the stopped request'; END IF;
 SELECT generation.* INTO g FROM lab.director_owner_generations generation
 JOIN lab.director_execution_control c ON c.run_id=generation.run_id AND c.current_generation=generation.generation
 WHERE c.run_id=r.run_id AND c.mode='active';
 IF g.run_id IS NULL OR
 (g.worker_pid,g.worker_start_ticks,g.worker_boot_id,g.worker_unit,g.worker_invocation_id,g.worker_cgroup)
 IS DISTINCT FROM (r.owner_pid,r.owner_start_ticks,r.owner_boot_id,r.owner_unit,r.owner_invocation_id,r.owner_cgroup)
 THEN RAISE EXCEPTION 'stop intent differs from current historical owner'; END IF;
 IF EXISTS(SELECT 1 FROM lab.baseline_calibrations WHERE run_id=r.run_id) OR
 EXISTS(SELECT 1 FROM scorer.task_scores WHERE run_id=r.run_id) OR
 EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=r.run_id AND kind<>'baseline') OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=r.run_id)<>1 OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=r.run_id)>3072 THEN
 RAISE EXCEPTION 'stop closure supports one unscored baseline only'; END IF;
 INSERT INTO lab.director_stop_closures(recovery_id,run_id,expected_generation,execution_sha256,owner_json)
 VALUES(p_stop,r.run_id,g.generation,g.execution_sha256,jsonb_build_object(
 'payload_sha256',(SELECT payload_sha256 FROM lab.runs WHERE run_id=r.run_id),
 'worker_pid',r.owner_pid,'worker_start_ticks',r.owner_start_ticks,'worker_boot_id',r.owner_boot_id,
 'worker_unit',r.owner_unit,'worker_invocation_id',r.owner_invocation_id,'worker_cgroup',r.owner_cgroup)) ON CONFLICT DO NOTHING;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF (a.run_id,a.expected_generation,a.execution_sha256) IS DISTINCT FROM (r.run_id,g.generation,g.execution_sha256)
 THEN RAISE EXCEPTION 'stop closure identity changed'; END IF;
 RETURN greatest(0,extract(epoch FROM a.created_at+interval '120 seconds'-clock_timestamp()));
END $$;
REVOKE ALL ON FUNCTION lab.begin_stopped_baseline_closure(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.begin_stopped_baseline_closure(uuid) TO swapp_lab_director;
""")
    op.execute(r"""CREATE FUNCTION lab.assert_stopped_job_transition(p_table text,p_operation text,p_new jsonb,p_old jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; j jsonb; target uuid; drain lab.director_stop_job_drains%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer restart identity required'; END IF;
 SELECT * INTO drain FROM lab.director_stop_job_drains
  WHERE recovery_id=nullif(current_setting('lab.stop_request',true),'')::uuid
  AND job_id=nullif(current_setting('lab.stop_job',true),'')::uuid;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=drain.recovery_id;
 target:=a.run_id; j:=drain.original_job;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR target IS NULL OR NOT EXISTS(
  SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
   WHERE r.run_id=target AND r.state='stop_requested' AND r.stop_requested AND c.mode='active'
    AND c.current_generation=a.expected_generation) OR
  j->>'run_id' IS DISTINCT FROM target::text OR
  j->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text OR
  j->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 THEN
  RAISE EXCEPTION 'restart child transition has no live immutable attempt'; END IF;
 IF p_new IS NULL THEN
  IF p_table IN ('score_jobs','task_terminal_outcomes','task_completions') THEN RETURN; END IF;
 ELSE
  IF p_new->>'run_id' IS DISTINCT FROM j->>'run_id' OR
     p_new->>'experiment_id' IS DISTINCT FROM j->>'experiment_id' OR
     p_new->>'evaluation_kind' IS DISTINCT FROM j->>'evaluation_kind' OR
     p_new->>'task_id' IS DISTINCT FROM j->>'task_id' OR p_new->'seed' IS DISTINCT FROM j->'seed' THEN
   RAISE EXCEPTION 'restart task identity changed'; END IF;
  IF p_table='score_jobs' AND p_operation='UPDATE' AND p_old=j AND
     p_new->>'state'='failed' AND p_new->>'error_code'='scorer_error' AND
     (p_new-ARRAY['state','error_code','updated_at'])=(p_old-ARRAY['state','error_code','updated_at']) THEN RETURN; END IF;
  IF p_table='task_terminal_outcomes' AND p_operation='INSERT' AND
     p_new->>'score_job_id'=drain.job_id::text AND p_new->>'outcome_code'='scorer_error' AND
     p_new->>'candidate_sha256'=j->>'candidate_sha256' AND
     p_new->>'admitted_generation'=a.expected_generation::text AND p_new->>'execution_sha256'=a.execution_sha256 AND
     p_new->>'recovery_invocation_id'=drain.recovery_invocation AND
     p_new->>'worker_invocation_id' IS NOT DISTINCT FROM j->>'claim_invocation_id' AND
     p_new->>'claim_token' IS NULL AND p_new->>'producer_role'='swapp_lab_scorer' THEN RETURN; END IF;
  IF p_table='task_completions' AND p_operation='INSERT' AND p_new->>'completion_kind'='terminal'
     AND EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes t WHERE t.run_id=target
      AND t.score_job_id=drain.job_id AND t.outcome_code='scorer_error'
      AND t.recovery_invocation_id=drain.recovery_invocation) THEN RETURN; END IF;
 END IF;
 RAISE EXCEPTION 'restart may only close the exact drained existing task';
END $$;

CREATE FUNCTION lab.reconcile_stopped_score_job(p_stop uuid,p_job uuid,p_expected text,p_recovery text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; j scorer.score_jobs%ROWTYPE; target uuid;
BEGIN
 IF session_user <> 'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
    OR p_expected IS NULL OR octet_length(p_expected)>32768 THEN RAISE EXCEPTION 'invalid child drain identity'; END IF;
 SELECT run_id INTO target FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF target IS NULL THEN RAISE EXCEPTION 'restart intent missing'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(target)),'hex'),1,16))::bit(64)::bigint);
 PERFORM 1 FROM lab.runs WHERE run_id=target AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'run stopped during child reconciliation'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT * INTO j FROM scorer.score_jobs WHERE job_id=p_job FOR UPDATE;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR j.run_id IS DISTINCT FROM target OR
    j.admitted_generation IS DISTINCT FROM a.expected_generation OR j.execution_sha256 IS DISTINCT FROM a.execution_sha256
    OR to_jsonb(j) IS DISTINCT FROM p_expected::jsonb THEN RAISE EXCEPTION 'child changed after exact drain proof'; END IF;
 IF j.state NOT IN ('queued','running') THEN RETURN j.state; END IF;
 IF EXISTS(SELECT 1 FROM scorer.task_completions WHERE run_id=j.run_id AND experiment_id=j.experiment_id
   AND evaluation_kind=j.evaluation_kind AND task_id=j.task_id AND seed=j.seed) THEN
  RAISE EXCEPTION 'unfinished score job conflicts with a committed result'; END IF;
 INSERT INTO lab.director_stop_job_drains(recovery_id,job_id,original_job,recovery_invocation)
 VALUES(p_stop,p_job,to_jsonb(j),p_recovery);
 PERFORM set_config('lab.stop_request',p_stop::text,true);
 PERFORM set_config('lab.stop_job',p_job::text,true);
 INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
  candidate_sha256,outcome_code,producer_role,score_job_id,worker_invocation_id,recovery_invocation_id,
  admitted_generation,execution_sha256)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,j.candidate_sha256,'scorer_error',
  'swapp_lab_scorer',j.job_id,j.claim_invocation_id,p_recovery,j.admitted_generation,j.execution_sha256);
 INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,completion_kind)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,'terminal');
 UPDATE scorer.score_jobs SET state='failed',error_code='scorer_error',updated_at=clock_timestamp()
 WHERE job_id=p_job;
 RETURN 'failed';
END $$;
REVOKE ALL ON FUNCTION lab.reconcile_stopped_score_job(uuid,uuid,text,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.assert_stopped_job_transition(text,text,jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.reconcile_stopped_score_job(uuid,uuid,text,text) TO swapp_lab_scorer;
""")
    op.execute(r"""
CREATE FUNCTION lab.finish_stopped_children(p_stop uuid,p_recovery text) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
 THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 IF a.run_id IS NULL OR a.created_at+interval '120 seconds'<=clock_timestamp() OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
 WHERE r.run_id=a.run_id AND r.state='stop_requested' AND r.stop_requested AND c.mode='active'
 AND c.current_generation=a.expected_generation) THEN RAISE EXCEPTION 'stop cleanup identity expired'; END IF;
 IF EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=a.run_id AND state IN ('queued','running'))
 THEN RAISE EXCEPTION 'stop jobs remain active'; END IF;
 IF NOT EXISTS(SELECT 1 FROM lab.director_stop_job_drains WHERE recovery_id=p_stop)
 THEN RAISE EXCEPTION 'stopped baseline needs exact orphan-job evidence'; END IF;
 UPDATE lab.director_stop_closures SET state='drained',recovery_invocation=coalesce(recovery_invocation,p_recovery)
 WHERE recovery_id=p_stop;
 RETURN 'drained';
END $$;
REVOKE ALL ON FUNCTION lab.finish_stopped_children(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.finish_stopped_children(uuid,text) TO swapp_lab_scorer;
CREATE FUNCTION lab.assert_stopped_missing_transition(p_table text,p_operation text,p_new jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; experiment text;
BEGIN
 IF session_user<>'swapp_lab_planner' THEN RAISE EXCEPTION 'Planner stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures
 WHERE recovery_id=nullif(current_setting('lab.stop_request',true),'')::uuid;
 experiment:=current_setting('lab.stop_missing',true);
 IF a.run_id IS NULL OR a.state IS DISTINCT FROM 'drained' OR
 a.created_at+interval '120 seconds'<=clock_timestamp() OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
 WHERE r.run_id=a.run_id AND r.state='stop_requested' AND r.stop_requested
 AND c.mode='active' AND c.current_generation=a.expected_generation)
 OR NOT EXISTS(SELECT 1 FROM lab.experiments e WHERE e.run_id=a.run_id AND e.experiment_id=experiment
 AND e.kind='baseline' AND e.status IN ('proposed','primary_running','abandoned'))
 THEN RAISE EXCEPTION 'stopped baseline closure not authorized'; END IF;
 IF p_table NOT IN ('task_terminal_outcomes','task_completions') OR p_operation<>'INSERT'
 THEN RAISE EXCEPTION 'stop closure cannot admit or score'; END IF;
 IF p_new IS NULL THEN RETURN; END IF;
 IF p_new->>'run_id' IS DISTINCT FROM a.run_id::text OR p_new->>'experiment_id' IS DISTINCT FROM experiment
 OR p_new->>'evaluation_kind' IS DISTINCT FROM 'baseline' OR NOT EXISTS(
 SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=experiment
 AND t.evaluation_kind='baseline' AND t.task_id=p_new->>'task_id' AND t.seed=(p_new->>'seed')::integer)
 OR EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=a.run_id AND j.experiment_id=experiment
 AND j.evaluation_kind='baseline' AND j.task_id=p_new->>'task_id' AND j.seed=(p_new->>'seed')::integer)
 THEN RAISE EXCEPTION 'stop task is not an unattempted existing cell'; END IF;
 IF p_table='task_terminal_outcomes' AND (p_new->>'outcome_code' IS DISTINCT FROM 'infrastructure_unattempted'
 OR p_new->>'producer_role' IS DISTINCT FROM 'swapp_lab_planner' OR p_new->>'score_job_id' IS NOT NULL
 OR p_new->>'claim_token' IS NOT NULL OR p_new->>'worker_invocation_id' IS NOT NULL
 OR p_new->>'recovery_invocation_id' IS NOT NULL OR p_new->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text
 OR p_new->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 OR NOT EXISTS(
 SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=experiment
 AND t.evaluation_kind='baseline' AND t.task_id=p_new->>'task_id' AND t.seed=(p_new->>'seed')::integer
 AND t.candidate_sha256=p_new->>'candidate_sha256')) THEN RAISE EXCEPTION 'stop outcome malformed'; END IF;
 IF p_table='task_completions' AND (p_new->>'completion_kind' IS DISTINCT FROM 'terminal' OR NOT EXISTS(
 SELECT 1 FROM scorer.task_terminal_outcomes t WHERE t.run_id=a.run_id AND t.experiment_id=experiment
 AND t.evaluation_kind='baseline' AND t.task_id=p_new->>'task_id' AND t.seed=(p_new->>'seed')::integer
 AND t.outcome_code='infrastructure_unattempted')) THEN RAISE EXCEPTION 'stop completion has no outcome'; END IF;
END $$;
CREATE FUNCTION lab.close_stopped_unattempted_tasks(p_stop uuid,p_experiment text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; cell scorer.run_tasks%ROWTYPE;
BEGIN
 IF session_user<>'swapp_lab_planner' OR p_stop IS NULL OR p_experiment IS NULL
 THEN RAISE EXCEPTION 'Planner stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF a.run_id IS NULL THEN RAISE EXCEPTION 'stop closure missing'; END IF;
 PERFORM lab.lock_run_plan(a.run_id);
 PERFORM set_config('lab.stop_request',p_stop::text,true);
 PERFORM set_config('lab.stop_missing',p_experiment,true);
 PERFORM lab.assert_stopped_missing_transition('task_terminal_outcomes','INSERT',NULL);
 FOR cell IN SELECT t.* FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=p_experiment
 AND t.evaluation_kind='baseline' AND NOT EXISTS(SELECT 1 FROM scorer.task_completions c WHERE
 c.run_id=t.run_id AND c.experiment_id=t.experiment_id AND c.evaluation_kind=t.evaluation_kind
 AND c.task_id=t.task_id AND c.seed=t.seed) AND NOT EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE
 j.run_id=t.run_id AND j.experiment_id=t.experiment_id AND j.evaluation_kind=t.evaluation_kind
 AND j.task_id=t.task_id AND j.seed=t.seed)
 LOOP
 INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
 candidate_sha256,outcome_code,producer_role,admitted_generation,execution_sha256)
 VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,cell.candidate_sha256,
 'infrastructure_unattempted','swapp_lab_planner',a.expected_generation,a.execution_sha256);
 INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,completion_kind)
 VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,'terminal');
 END LOOP;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION lab.assert_stopped_missing_transition(text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION lab.close_stopped_unattempted_tasks(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.close_stopped_unattempted_tasks(uuid,text) TO swapp_lab_planner;
""")
    op.execute(r"""DO $rewrite$ DECLARE body text; BEGIN SELECT pg_get_functiondef('lab.guard_scorer_owned_statement()'::regprocedure) INTO body; IF position('BEGIN' in body)=0 THEN RAISE EXCEPTION 'expected trigger body missing'; END IF; body:=overlay(body placing $branch$BEGIN
 IF session_user='swapp_lab_scorer' AND nullif(current_setting('lab.stop_job',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_job_transition(TG_TABLE_NAME,TG_OP,NULL,NULL); RETURN NULL; END IF;
 IF session_user='swapp_lab_planner' AND nullif(current_setting('lab.stop_missing',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_missing_transition(TG_TABLE_NAME,TG_OP,NULL); RETURN NULL; END IF;
$branch$ from position('BEGIN' in body) for 5); EXECUTE body; END $rewrite$;""")
    op.execute(r"""DO $rewrite$ DECLARE body text; BEGIN SELECT pg_get_functiondef('lab.guard_scorer_owned_row()'::regprocedure) INTO body; IF position('BEGIN' in body)=0 THEN RAISE EXCEPTION 'expected trigger body missing'; END IF; body:=overlay(body placing $branch$BEGIN
 IF session_user='swapp_lab_scorer' AND nullif(current_setting('lab.stop_job',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_job_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW),CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) ELSE NULL END); RETURN NEW; END IF;
 IF session_user='swapp_lab_planner' AND nullif(current_setting('lab.stop_missing',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_missing_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW)); RETURN NEW; END IF;
$branch$ from position('BEGIN' in body) for 5); EXECUTE body; END $rewrite$;""")
    op.execute(r"""DO $rewrite$ DECLARE body text; BEGIN SELECT pg_get_functiondef('scorer.guard_terminal_task_invocation()'::regprocedure) INTO body; IF position('BEGIN' in body)=0 THEN RAISE EXCEPTION 'expected trigger body missing'; END IF; body:=overlay(body placing $branch$BEGIN
 IF session_user='swapp_lab_scorer' AND nullif(current_setting('lab.stop_job',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_job_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW),CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) ELSE NULL END); RETURN NEW; END IF;
 IF session_user='swapp_lab_planner' AND nullif(current_setting('lab.stop_missing',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_missing_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW)); RETURN NEW; END IF;
$branch$ from position('BEGIN' in body) for 5); EXECUTE body; END $rewrite$;""")
    op.execute(r"""DO $rewrite$ DECLARE body text; BEGIN SELECT pg_get_functiondef('scorer.capture_terminal_task_completion_v9()'::regprocedure) INTO body; IF position('BEGIN' in body)=0 THEN RAISE EXCEPTION 'expected trigger body missing'; END IF; body:=overlay(body placing $branch$BEGIN
 IF session_user='swapp_lab_scorer' AND nullif(current_setting('lab.stop_job',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_job_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW),CASE WHEN TG_OP='UPDATE' THEN to_jsonb(OLD) ELSE NULL END); RETURN NEW; END IF;
 IF session_user='swapp_lab_planner' AND nullif(current_setting('lab.stop_missing',true),'') IS NOT NULL THEN
 PERFORM lab.assert_stopped_missing_transition(TG_TABLE_NAME,TG_OP,to_jsonb(NEW)); RETURN NEW; END IF;
$branch$ from position('BEGIN' in body) for 5); EXECUTE body; END $rewrite$;""")


def downgrade() -> None:
    """Resumed generation history cannot safely be erased by an automatic downgrade."""
    raise RuntimeError("0030 requires an explicit drained history-preserving rollback plan")
