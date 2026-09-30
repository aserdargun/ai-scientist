"""Close bounded existing unfinished baselines while retaining completed record pairs."""

from alembic import op

revision = "0034_multi_baseline_stop"
down_revision = "0033_measured_baseline_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Replace only the existing stop admission guard; preserve its ACL and owner."""
    op.execute(r"""

CREATE OR REPLACE FUNCTION lab.begin_stopped_baseline_closure(p_stop uuid) RETURNS double precision
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.director_recoveries%ROWTYPE; g lab.director_owner_generations%ROWTYPE;
 a lab.director_stop_closures%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_director' OR p_stop IS NULL THEN RAISE EXCEPTION
 'Director stop identity required' ; END IF;
 SELECT * INTO r FROM lab.director_recoveries WHERE recovery_id=p_stop;
 IF r.run_id IS NULL OR r.action IS DISTINCT FROM 'stop_and_finalize' OR
 r.state NOT IN ( 'started' , 'pending' ) THEN RAISE EXCEPTION 'stop recovery intent missing' ;
 END IF;
 PERFORM lab.lock_run_plan(r.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=r.run_id AND state= 'stop_requested' AND stop_requested
 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'stop closure requires the stopped request'; END IF;
 SELECT generation.* INTO g FROM lab.director_owner_generations generation
 JOIN lab.director_execution_control c ON c.run_id=generation.run_id AND
 c.current_generation=generation.generation
 WHERE c.run_id=r.run_id AND c.mode='active';
 IF g.run_id IS NULL OR

 (g.worker_pid,g.worker_start_ticks,g.worker_boot_id,g.worker_unit,g.worker_invocation_id,g.worker_cgroup)
 IS DISTINCT FROM
 (r.owner_pid,r.owner_start_ticks,r.owner_boot_id,r.owner_unit,r.owner_invocation_id,r.owner_cgroup)
 THEN RAISE EXCEPTION 'stop intent differs from current historical owner'; END IF;
 IF EXISTS(SELECT 1 FROM lab.baseline_calibrations WHERE run_id=r.run_id) OR
 EXISTS(
 SELECT 1 FROM scorer.task_scores s
 LEFT JOIN scorer.run_tasks t USING(run_id,experiment_id,evaluation_kind,task_id,seed)
 LEFT JOIN scorer.score_jobs j ON j.job_id=s.score_job_id
 LEFT JOIN scorer.task_completions c ON
 (c.run_id,c.experiment_id,c.evaluation_kind,c.task_id,c.seed)=
 (s.run_id,s.experiment_id,s.evaluation_kind,s.task_id,s.seed)
 LEFT JOIN scorer.dataset_profiles p ON (p.dataset_id,p.split_id,p.session_id)=
 (t.dataset_id,t.split_id,t.session_id)
 LEFT JOIN lab.experiments e ON e.experiment_id=s.experiment_id AND e.run_id=s.run_id
 LEFT JOIN lab.director_execution_contracts x ON x.run_id=s.run_id
 WHERE s.run_id=r.run_id AND (
 t.run_id IS NULL OR e.kind IS DISTINCT FROM 'baseline' OR s.evaluation_kind<>'baseline'
 OR s.seed NOT BETWEEN 0 AND 2 OR j.state IS DISTINCT FROM 'completed'
 OR (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed,j.candidate_sha256)
 IS DISTINCT FROM (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed,t.candidate_sha256)
 OR j.admitted_generation IS DISTINCT FROM g.generation
 OR j.execution_sha256 IS DISTINCT FROM g.execution_sha256
 OR s.worker_invocation_id IS NULL OR s.worker_invocation_id !~ '^[0-9a-f]{32}$'
 OR s.claim_token IS NOT NULL
 OR c.completion_kind IS DISTINCT FROM 'scored'
 OR t.candidate_sha256 IS DISTINCT FROM e.candidate_sha256
 OR s.score->>'candidate_sha256' IS DISTINCT FROM t.candidate_sha256
 OR s.score->>'dataset_id' IS DISTINCT FROM t.dataset_id
 OR s.score->>'split_id' IS DISTINCT FROM t.split_id
 OR s.score->>'session_id' IS DISTINCT FROM t.session_id
 OR s.score->>'profile_sha256' IS DISTINCT FROM p.profile_sha256
 OR s.score->>'task_family' IS DISTINCT FROM p.task_family
 OR s.score->>'harness_sha256' IS DISTINCT FROM x.execution_json->>'harness_sha256'
 OR s.score->>'candidate_output_sha256' IS DISTINCT FROM j.artifact_sha256
 OR p.visibility IS DISTINCT FROM 'dev')) OR
 EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=r.run_id AND kind<>'baseline') OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=r.run_id) NOT BETWEEN 1 AND 3 OR
 (SELECT count(DISTINCT baseline_name) FROM lab.experiments WHERE run_id=r.run_id)
 IS DISTINCT FROM (SELECT count(*) FROM lab.experiments WHERE run_id=r.run_id) OR
 EXISTS(SELECT 1 FROM lab.experiments e WHERE e.run_id=r.run_id AND (
 e.baseline_name NOT IN ('robust_z','iforest','ecod_train_frozen')
 OR e.baseline_name IS NULL OR e.status NOT IN ('proposed','primary_running','scored','abandoned')
 OR (e.status IN ('scored','abandoned') AND (
 NOT EXISTS(SELECT 1 FROM lab.experiment_records d JOIN lab.trajectory_records v
 USING(experiment_id) WHERE d.experiment_id=e.experiment_id
 AND d.experiment_json->>'run_id'=r.run_id::text
 AND d.experiment_json->>'kind'='baseline'
 AND d.experiment_json->>'status'=e.status
 AND d.experiment_json->>'baseline_name'=e.baseline_name
 AND d.experiment_json->>'candidate_sha256'=e.candidate_sha256
 AND d.experiment_json->>'inputs_sha256'=e.inputs_sha256
 AND v.trajectory_json->>'run_id'=r.run_id::text
 AND v.trajectory_json->>'kind'='baseline'
 AND v.trajectory_json->>'baseline_name'=e.baseline_name
 AND v.trajectory_json->>'inputs_sha256'=e.inputs_sha256)))
 OR (e.status='scored' AND (
 (SELECT count(*) FROM scorer.run_tasks t WHERE t.run_id=r.run_id
 AND t.experiment_id=e.experiment_id) NOT BETWEEN 3 AND 3072
 OR (SELECT count(*) FROM scorer.task_scores s WHERE s.run_id=r.run_id
 AND s.experiment_id=e.experiment_id) IS DISTINCT FROM
 (SELECT count(*) FROM scorer.run_tasks t WHERE t.run_id=r.run_id
 AND t.experiment_id=e.experiment_id))))) OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=r.run_id)>9216 THEN
 RAISE EXCEPTION
 'stop closure requires one to three trusted uncalibrated baselines with verified cells'; END IF;
 INSERT INTO
 lab.director_stop_closures(recovery_id,run_id,expected_generation,execution_sha256,owner_json)
 VALUES(p_stop,r.run_id,g.generation,g.execution_sha256,jsonb_build_object(
 'payload_sha256',(SELECT payload_sha256 FROM lab.runs WHERE run_id=r.run_id),
 'worker_pid',r.owner_pid,'worker_start_ticks',r.owner_start_ticks,'worker_boot_id',r.owner_boot_id,
 'worker_unit' ,r.owner_unit, 'worker_invocation_id' ,r.owner_invocation_id, 'worker_cgroup'
 ,r.owner_cgroup)) ON CONFLICT DO NOTHING;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF (a.run_id,a.expected_generation,a.execution_sha256) IS DISTINCT FROM
 (r.run_id,g.generation,g.execution_sha256)
 THEN RAISE EXCEPTION 'stop closure identity changed'; END IF;
 RETURN greatest(0,extract(epoch FROM a.created_at+interval '120 seconds'-clock_timestamp()));
END $$;
    
    """)


def downgrade() -> None:
    raise RuntimeError("restoring one-baseline stop scope requires reviewed catalog reversal")
