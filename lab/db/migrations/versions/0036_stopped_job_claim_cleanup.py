"""Clear live claim token and lease when closing an exactly drained stopped job."""

from alembic import op

revision = "0036_stopped_job_claim_cleanup"
down_revision = "0035_scorer_process_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Preserve existing stop authority and ACL; require token and lease cleanup."""
    op.execute(r"""
CREATE OR REPLACE FUNCTION lab.assert_stopped_job_transition(p_table text,p_operation text,
 p_new jsonb,p_old jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; j jsonb; target uuid; drain
 lab.director_stop_job_drains%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION
 'Scorer restart identity required'; END IF;
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
     p_new->>'task_id' IS DISTINCT FROM j->>'task_id' OR p_new->'seed' IS DISTINCT FROM
 j->'seed' THEN
   RAISE EXCEPTION 'restart task identity changed'; END IF;
  IF p_table='score_jobs' AND p_operation='UPDATE' AND p_old=j AND
     p_new->>'state'='failed' AND p_new->>'error_code'='scorer_error' AND
     p_new->>'claimed_by' IS NULL AND p_new->>'lease_until' IS NULL AND
     (p_new-ARRAY['state','error_code','updated_at','claimed_by','lease_until'])=
     (p_old-ARRAY['state','error_code','updated_at','claimed_by','lease_until']) THEN RETURN;
 END IF;
  IF p_table='task_terminal_outcomes' AND p_operation='INSERT' AND
     p_new->>'score_job_id'=drain.job_id::text AND p_new->>'outcome_code'='scorer_error' AND
     p_new->>'candidate_sha256'=j->>'candidate_sha256' AND
     p_new->>'admitted_generation'=a.expected_generation::text AND
 p_new->>'execution_sha256'=a.execution_sha256 AND
     p_new->>'recovery_invocation_id'=drain.recovery_invocation AND
     p_new->>'worker_invocation_id' IS NOT DISTINCT FROM j->>'claim_invocation_id' AND
     p_new->>'claim_token' IS NULL AND p_new->>'producer_role'='swapp_lab_scorer' THEN
 RETURN; END IF;
  IF p_table='task_completions' AND p_operation='INSERT' AND p_new->>'completion_kind'='terminal'
     AND EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes t WHERE t.run_id=target
      AND t.score_job_id=drain.job_id AND t.outcome_code='scorer_error'
      AND t.recovery_invocation_id=drain.recovery_invocation) THEN RETURN; END IF;
 END IF;
 RAISE EXCEPTION 'restart may only close the exact drained existing task';
END $$;

CREATE OR REPLACE FUNCTION lab.reconcile_stopped_score_job(p_stop uuid,p_job uuid,p_expected
 text,p_recovery text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; j scorer.score_jobs%ROWTYPE; target uuid;
BEGIN
 IF session_user <> 'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
    OR p_expected IS NULL OR octet_length(p_expected)>32768 THEN RAISE EXCEPTION
 'invalid child drain identity'; END IF;
 SELECT run_id INTO target FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF target IS NULL THEN RAISE EXCEPTION 'restart intent missing'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(uuid_send(target)),'hex'),1,
 16))::bit(64)::bigint);
 PERFORM 1 FROM lab.runs WHERE run_id=target AND state='stop_requested' AND stop_requested
 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'run stopped during child reconciliation'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT * INTO j FROM scorer.score_jobs WHERE job_id=p_job FOR UPDATE;
 IF a.created_at + interval '120 seconds' <= clock_timestamp() OR
    a.state IS DISTINCT FROM 'pending' OR j.run_id IS DISTINCT FROM target OR
    j.admitted_generation IS DISTINCT FROM a.expected_generation OR j.execution_sha256 IS
 DISTINCT FROM a.execution_sha256
    OR to_jsonb(j) IS DISTINCT FROM p_expected::jsonb THEN RAISE EXCEPTION
 'child changed after exact drain proof'; END IF;
 IF j.state NOT IN ('queued','running') THEN RETURN j.state; END IF;
 IF EXISTS(SELECT 1 FROM scorer.task_completions WHERE run_id=j.run_id AND
 experiment_id=j.experiment_id
   AND evaluation_kind=j.evaluation_kind AND task_id=j.task_id AND seed=j.seed) THEN
  RAISE EXCEPTION 'unfinished score job conflicts with a committed result'; END IF;
 INSERT INTO lab.director_stop_job_drains(recovery_id,job_id,original_job,recovery_invocation)
 VALUES(p_stop,p_job,to_jsonb(j),p_recovery);
 PERFORM set_config('lab.stop_request',p_stop::text,true);
 PERFORM set_config('lab.stop_job',p_job::text,true);
 INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
  candidate_sha256,outcome_code,producer_role,score_job_id,worker_invocation_id,
 recovery_invocation_id,
  admitted_generation,execution_sha256)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,j.candidate_sha256,
 'scorer_error',
  'swapp_lab_scorer',j.job_id,j.claim_invocation_id,p_recovery,j.admitted_generation,
 j.execution_sha256);
 INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,
 completion_kind)
 VALUES(j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,'terminal');
 UPDATE scorer.score_jobs SET state='failed',error_code='scorer_error',
 claimed_by=NULL,lease_until=NULL,updated_at=clock_timestamp()
 WHERE job_id=p_job;
 RETURN 'failed';
END $$;
""")


def downgrade() -> None:
    """Keep the corrected immutable transition installed."""
    raise RuntimeError("stopped job claim cleanup is forward only")
