"""Expose narrow committed Scorer identity without raw scores or claim changes."""

from alembic import op

revision = "0035_scorer_process_provenance"
down_revision = "0034_multi_baseline_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one provenance-only RPC; no table or historical-data modifications."""
    op.execute(r"""
CREATE FUNCTION lab.completed_score_job_process_receipt(p_job uuid) RETURNS jsonb
 LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE receipt jsonb;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_job IS NULL
 THEN RAISE EXCEPTION 'Director completed job identity required'; END IF;
 SELECT jsonb_build_object(
  'schema','completed-score-job-process-receipt.v1',
  'job_id',j.job_id::text,'run_id',j.run_id::text,'experiment_id',j.experiment_id,
  'evaluation_kind',j.evaluation_kind,'task_id',j.task_id,'seed',j.seed,'attempt',j.attempt,
  'worker_unit','swapp-ai-scientist-scorer-' || replace(j.job_id::text,'-','') || '.service',
  'worker_invocation_id',s.worker_invocation_id
 ) INTO receipt
 FROM scorer.score_jobs j JOIN scorer.task_scores s ON s.score_job_id=j.job_id
 AND (s.run_id,s.experiment_id,s.evaluation_kind,s.task_id,s.seed)=
     (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 JOIN scorer.task_completions c ON
 (c.run_id,c.experiment_id,c.evaluation_kind,c.task_id,c.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 JOIN scorer.run_tasks t ON
 (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 JOIN scorer.dataset_profiles p ON (p.dataset_id,p.split_id,p.session_id)=
 (t.dataset_id,t.split_id,t.session_id)
 WHERE j.job_id=p_job AND j.state='completed' AND j.claimed_by IS NULL
 AND j.lease_until IS NULL AND j.attempt>=1 AND s.claim_token IS NULL
 AND s.worker_invocation_id ~ '^[0-9a-f]{32}$' AND c.completion_kind='scored'
 AND j.evaluation_kind IN ('baseline','primary','confirmation') AND p.visibility='dev'
 AND t.candidate_sha256=j.candidate_sha256;
 IF receipt IS NULL THEN RAISE EXCEPTION 'completed job provenance receipt missing'; END IF;
 RETURN receipt;
END $$;
ALTER FUNCTION lab.completed_score_job_process_receipt(uuid) OWNER TO swapp_lab_migrator;
REVOKE ALL ON FUNCTION lab.completed_score_job_process_receipt(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.completed_score_job_process_receipt(uuid) TO swapp_lab_director;
    """)


def downgrade() -> None:
    """Remove only the new receipt authority; existing functions and rows unchanged."""
    op.execute("DROP FUNCTION lab.completed_score_job_process_receipt(uuid)")
