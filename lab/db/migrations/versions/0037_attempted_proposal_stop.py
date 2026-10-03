"""Fence one admitted primary proposal stop without a candidate quality verdict."""

# Fixed SQL retains old guards and their existing role-specific RPC ACLs.
# ruff: noqa: E501
from alembic import op

revision = "0037_attempted_proposal_stop"
down_revision = "0036_stopped_job_claim_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Reuse one original stop authority; preserve zero-admission and producer fences."""
    op.execute(r"""
ALTER TABLE lab.director_stopped_proposals
 ADD COLUMN stop_mode text NOT NULL DEFAULT 'unattempted'
 CHECK(stop_mode IN ('unattempted','primary_admitted')),
 ADD COLUMN admitted_inventory_text text,
 ADD COLUMN admitted_inventory jsonb,
 ADD COLUMN admitted_inventory_sha256 text,
 ADD COLUMN baseline_inventory_sha256 text,
 ADD COLUMN stop_envelope_sha256 text,
 ADD COLUMN terminal_inventory_text text,
 ADD COLUMN terminal_inventory_sha256 text,
 ADD CONSTRAINT attempted_stop_marker_shape CHECK(
 (stop_mode='unattempted' AND admitted_inventory IS NULL
 AND admitted_inventory_text IS NULL AND admitted_inventory_sha256 IS NULL
 AND baseline_inventory_sha256 IS NULL AND stop_envelope_sha256 IS NULL
 AND terminal_inventory_text IS NULL AND terminal_inventory_sha256 IS NULL) OR
 (stop_mode='primary_admitted' AND admitted_inventory_text IS NOT NULL
 AND octet_length(admitted_inventory_text)<=2097152
 AND admitted_inventory IS NOT NULL AND jsonb_typeof(admitted_inventory)='array'
 AND admitted_inventory=admitted_inventory_text::jsonb
 AND admitted_inventory_sha256 IS NOT NULL AND admitted_inventory_sha256 ~ '^[0-9a-f]{64}$'
 AND baseline_inventory_sha256 IS NOT NULL AND baseline_inventory_sha256 ~ '^[0-9a-f]{64}$'
 AND stop_envelope_sha256 IS NOT NULL AND stop_envelope_sha256 ~ '^[0-9a-f]{64}$'
 AND ((terminal_inventory_text IS NULL AND terminal_inventory_sha256 IS NULL)
 OR (terminal_inventory_text IS NOT NULL AND terminal_inventory_sha256 IS NOT NULL AND terminal_inventory_sha256 ~ '^[0-9a-f]{64}$'))));

CREATE FUNCTION lab.assert_attempted_stop_window(p_run uuid,p_stop uuid)
 RETURNS double precision LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
DECLARE deadline timestamptz; first_stop timestamptz; closure_created timestamptz;
BEGIN
 SELECT deadline_at INTO deadline FROM lab.director_execution_contracts WHERE run_id=p_run;
 SELECT min(created_at) INTO first_stop FROM lab.run_events
 WHERE run_id=p_run AND event_type='run.stop_requested';
 SELECT created_at INTO closure_created FROM lab.director_stop_closures
 WHERE recovery_id=p_stop AND run_id=p_run;
 IF deadline IS NULL OR first_stop IS NULL THEN RAISE EXCEPTION
 'attempted stop original deadline missing'; END IF;
 deadline:=least(deadline,first_stop+interval '120 seconds',
 coalesce(closure_created+interval '120 seconds',deadline));
 IF deadline<=clock_timestamp() THEN RAISE EXCEPTION
 'attempted stop original deadline expired'; END IF;
 RETURN extract(epoch FROM deadline-clock_timestamp());
END $$;

CREATE FUNCTION lab.attempted_retained_inventory_sha256(p_run uuid) RETURNS text
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
 SELECT encode(sha256(convert_to(jsonb_build_object(
 'tasks',(SELECT jsonb_agg(to_jsonb(t) ORDER BY experiment_id,evaluation_kind,task_id,seed)
 FROM scorer.run_tasks t WHERE run_id=p_run),
 'scores',(SELECT jsonb_agg(to_jsonb(s) ORDER BY score_job_id) FROM scorer.task_scores s
 WHERE run_id=p_run),
 'scored_completions',(SELECT jsonb_agg(to_jsonb(c) ORDER BY experiment_id,evaluation_kind,task_id,seed)
 FROM scorer.task_completions c WHERE run_id=p_run AND completion_kind='scored'),
 'baseline_jobs',(SELECT jsonb_agg(to_jsonb(j) ORDER BY job_id) FROM scorer.score_jobs j
 JOIN lab.experiments e USING(experiment_id) WHERE j.run_id=p_run AND e.kind='baseline'),
 'records',(SELECT jsonb_agg(to_jsonb(r) ORDER BY r.experiment_id) FROM lab.experiment_records r
 JOIN lab.experiments e USING(experiment_id) WHERE e.run_id=p_run AND e.kind='baseline'),
 'trajectories',(SELECT jsonb_agg(to_jsonb(t) ORDER BY t.experiment_id) FROM lab.trajectory_records t
 JOIN lab.experiments e USING(experiment_id) WHERE e.run_id=p_run AND e.kind='baseline'),
 'calibration',(SELECT to_jsonb(c) FROM lab.baseline_calibrations c WHERE run_id=p_run)
 )::text,'UTF8')),'hex')
 $$;

CREATE FUNCTION lab.valid_attempted_worker_terminal(p_job uuid,p_original jsonb,
 p_generation integer,p_execution text) RETURNS boolean LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT EXISTS(SELECT 1 FROM scorer.score_jobs j
 JOIN scorer.task_terminal_outcomes o ON o.score_job_id=j.job_id
 AND (o.run_id,o.experiment_id,o.evaluation_kind,o.task_id,o.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 JOIN scorer.task_completions c ON
 (c.run_id,c.experiment_id,c.evaluation_kind,c.task_id,c.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 WHERE j.job_id=p_job AND j.state='failed' AND j.error_code='scorer_error'
 AND j.claimed_by IS NULL AND j.lease_until IS NULL AND j.claim_unit IS NULL
 AND j.claim_invocation_id IS NULL AND j.attempt>=1
 AND j.admitted_generation=p_generation AND j.execution_sha256=p_execution
 AND o.outcome_code='scorer_error' AND o.producer_role='swapp_lab_scorer'
 AND o.recovery_invocation_id IS NULL AND o.claim_token ~ '^[A-Za-z0-9_.:-]{1,128}$'
 AND o.worker_invocation_id ~ '^[0-9a-f]{32}$'
 AND o.candidate_sha256=j.candidate_sha256 AND o.admitted_generation=p_generation
 AND o.execution_sha256=p_execution AND c.completion_kind='terminal'
 AND (p_original->>'state'='failed' AND to_jsonb(j)=p_original OR
 p_original->>'state'='running' AND o.claim_token=p_original->>'claimed_by'
 AND o.worker_invocation_id=p_original->>'claim_invocation_id'
 AND p_original->>'claim_unit'='swapp-ai-scientist-scorer-'||replace(p_job::text,'-','')||'.service'
 AND (to_jsonb(j)-ARRAY['state','error_code','updated_at','claimed_by','lease_until','claim_unit','claim_invocation_id'])=
 (p_original-ARRAY['state','error_code','updated_at','claimed_by','lease_until','claim_unit','claim_invocation_id']))
 AND NOT EXISTS(SELECT 1 FROM scorer.task_scores WHERE score_job_id=p_job))
 $$;

CREATE FUNCTION lab.assert_attempted_proposal_shape(p_run uuid,p_experiment text,
 p_generation integer,p_execution text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
DECLARE n integer; candidate_count integer;
BEGIN
 SELECT task_count INTO n FROM lab.baseline_calibrations WHERE run_id=p_run;
 IF n IS NULL OR n NOT BETWEEN 1 AND 1024 OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=p_run AND kind='baseline')<>3 OR
 (SELECT count(DISTINCT baseline_name) FROM lab.experiments WHERE run_id=p_run
 AND kind='baseline' AND baseline_name IN ('robust_z','iforest','ecod_train_frozen'))<>3 OR
 EXISTS(SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run AND e.kind='baseline' AND
 (e.status<>'scored' OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=p_run AND experiment_id=e.experiment_id)<>3*n OR
 (SELECT count(*) FROM scorer.score_jobs WHERE run_id=p_run AND experiment_id=e.experiment_id)<>3*n OR
 (SELECT count(*) FROM scorer.task_scores WHERE run_id=p_run AND experiment_id=e.experiment_id)<>3*n OR
 NOT EXISTS(SELECT 1 FROM lab.experiment_records r
 JOIN lab.trajectory_records t USING(experiment_id) WHERE r.experiment_id=e.experiment_id
 AND r.experiment_json->>'kind'='baseline' AND r.experiment_json->>'status'='scored'
 AND r.experiment_json->>'candidate_sha256'=e.candidate_sha256
 AND r.experiment_json->>'inputs_sha256'=e.inputs_sha256
 AND t.trajectory_json->>'kind'='baseline' AND t.trajectory_json->>'inputs_sha256'=e.inputs_sha256))) OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=p_run AND kind='proposal')<>1 OR
 NOT EXISTS(SELECT 1 FROM lab.experiments e WHERE run_id=p_run AND experiment_id=p_experiment
 AND kind='proposal' AND status IN ('primary_running','abandoned')
 AND calibration_sha256=(SELECT calibration_sha256 FROM lab.baseline_calibrations WHERE run_id=p_run)) OR
 EXISTS(SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run AND
 ((experiment_id=p_experiment AND (evaluation_kind<>'primary' OR seed<>0)) OR
 (experiment_id<>p_experiment AND (evaluation_kind<>'baseline' OR seed NOT BETWEEN 0 AND 2)))) OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=p_run AND experiment_id=p_experiment)<>n OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=p_run AND experiment_id<>p_experiment)<>9*n OR
 (SELECT count(*) FROM scorer.score_jobs WHERE run_id=p_run AND experiment_id<>p_experiment)<>9*n OR
 (SELECT count(*) FROM scorer.task_scores WHERE run_id=p_run AND experiment_id<>p_experiment)<>9*n THEN
 RAISE EXCEPTION 'attempted stop requires one admitted primary and complete frozen calibration'; END IF;
 SELECT count(*) INTO candidate_count FROM scorer.score_jobs WHERE run_id=p_run
 AND experiment_id=p_experiment;
 IF candidate_count NOT BETWEEN 1 AND n OR EXISTS(
 SELECT 1 FROM scorer.score_jobs j JOIN lab.experiments e USING(experiment_id)
 LEFT JOIN scorer.run_tasks t ON (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 WHERE j.run_id=p_run AND (t.run_id IS NULL OR j.candidate_sha256 IS DISTINCT FROM e.candidate_sha256
 OR j.execution_sha256 IS DISTINCT FROM p_execution OR
 (j.experiment_id=p_experiment AND (j.admitted_generation IS DISTINCT FROM p_generation
 OR j.evaluation_kind<>'primary' OR j.seed<>0)) OR
 (j.experiment_id<>p_experiment AND (e.kind<>'baseline' OR j.state<>'completed' OR
 NOT lab.valid_director_restart_ancestry(p_run,j.admitted_generation,p_generation,p_execution))) OR
 j.state NOT IN ('queued','running','completed','failed') OR
 (j.experiment_id=p_experiment AND j.state='failed' AND
 NOT lab.valid_attempted_worker_terminal(j.job_id,to_jsonb(j),p_generation,p_execution)
 AND NOT EXISTS(SELECT 1 FROM lab.director_stopped_proposals p
 JOIN lab.director_stop_job_drains d USING(recovery_id)
 WHERE p.experiment_id=p_experiment AND p.stop_mode='primary_admitted' AND d.job_id=j.job_id)) OR
 (j.state='completed' AND NOT EXISTS(SELECT 1 FROM scorer.task_scores s
 JOIN scorer.task_completions c USING(run_id,experiment_id,evaluation_kind,task_id,seed)
 JOIN scorer.dataset_profiles d ON (d.dataset_id,d.split_id,d.session_id)=
 (t.dataset_id,t.split_id,t.session_id)
 JOIN lab.director_execution_contracts x ON x.run_id=j.run_id
 WHERE s.score_job_id=j.job_id AND s.claim_token IS NULL
 AND (s.run_id,s.experiment_id,s.evaluation_kind,s.task_id,s.seed)=
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)
 AND s.admitted_generation=j.admitted_generation AND s.execution_sha256=p_execution
 AND s.worker_invocation_id ~ '^[0-9a-f]{32}$' AND c.completion_kind='scored'
 AND s.score->>'candidate_sha256'=j.candidate_sha256
 AND s.score->>'candidate_output_sha256'=j.artifact_sha256
 AND s.score->>'dataset_id'=t.dataset_id AND s.score->>'split_id'=t.split_id
 AND s.score->>'session_id'=t.session_id AND s.score->>'profile_sha256'=d.profile_sha256
 AND s.score->>'task_family'=d.task_family AND d.visibility='dev'
 AND s.score->>'harness_sha256'=x.execution_json->>'harness_sha256')))) THEN
 RAISE EXCEPTION 'attempted stop job ancestry or original scored identity changed'; END IF;
END $$;

CREATE FUNCTION lab.assert_attempted_terminal_inventory(p_stop uuid) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE;
 original jsonb; current_job jsonb; target_job uuid;
BEGIN
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_mode IS DISTINCT FROM 'primary_admitted' OR
 p.baseline_inventory_sha256 IS DISTINCT FROM lab.attempted_retained_inventory_sha256(a.run_id)
 OR (SELECT count(*) FROM scorer.score_jobs WHERE run_id=a.run_id AND experiment_id=p.experiment_id)
 IS DISTINCT FROM jsonb_array_length(p.admitted_inventory) THEN
 RAISE EXCEPTION 'attempted terminal retained inventory changed'; END IF;
 FOR original IN SELECT value FROM jsonb_array_elements(p.admitted_inventory) LOOP
 target_job:=(original->>'job_id')::uuid;
 SELECT to_jsonb(j) INTO current_job FROM scorer.score_jobs j WHERE j.job_id=target_job;
 IF original->>'state'='completed' THEN
 IF current_job IS DISTINCT FROM original THEN RAISE EXCEPTION
 'attempted stop altered retained completed job'; END IF;
 ELSIF lab.valid_attempted_worker_terminal(target_job,original,a.expected_generation,a.execution_sha256) THEN
 IF EXISTS(SELECT 1 FROM lab.director_stop_job_drains WHERE recovery_id=p_stop AND job_id=target_job)
 THEN RAISE EXCEPTION 'worker terminal must not acquire a recovery drain'; END IF;
 ELSE
 IF original->>'state' NOT IN ('queued','running') OR current_job->>'state' IS DISTINCT FROM 'failed'
 OR current_job->>'error_code' IS DISTINCT FROM 'scorer_error'
 OR current_job->>'claimed_by' IS NOT NULL OR current_job->>'lease_until' IS NOT NULL
 OR (current_job-ARRAY['state','error_code','updated_at','claimed_by','lease_until']) IS DISTINCT FROM
 (original-ARRAY['state','error_code','updated_at','claimed_by','lease_until']) OR
 NOT EXISTS(SELECT 1 FROM lab.director_stop_job_drains d
 JOIN scorer.task_terminal_outcomes o ON o.score_job_id=d.job_id
 JOIN scorer.task_completions c USING(run_id,experiment_id,evaluation_kind,task_id,seed)
 WHERE d.recovery_id=p_stop AND d.job_id=target_job AND d.original_job=original
 AND o.outcome_code='scorer_error' AND o.producer_role='swapp_lab_scorer'
 AND o.recovery_invocation_id=d.recovery_invocation
 AND o.worker_invocation_id IS NOT DISTINCT FROM original->>'claim_invocation_id'
 AND o.admitted_generation=a.expected_generation AND o.execution_sha256=a.execution_sha256
 AND o.candidate_sha256=original->>'candidate_sha256' AND o.claim_token IS NULL
 AND c.completion_kind='terminal') OR EXISTS(SELECT 1 FROM scorer.task_scores WHERE score_job_id=target_job)
 THEN RAISE EXCEPTION 'attempted terminal child lacks exact native drain'; END IF;
 END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM lab.director_stop_job_drains d WHERE d.recovery_id=p_stop
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.admitted_inventory) v
 WHERE v->>'job_id'=d.job_id::text AND v->>'state' IN ('queued','running'))) THEN
 RAISE EXCEPTION 'attempted stop has unrelated drain'; END IF;
END $$;

CREATE FUNCTION lab.attempted_terminal_inventory_text(p_stop uuid) RETURNS text
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('jobs',(SELECT jsonb_agg(to_jsonb(j) ORDER BY j.job_id)
 FROM scorer.score_jobs j WHERE j.run_id=p.run_id AND j.experiment_id=s.experiment_id),
 'drains',(SELECT jsonb_agg(to_jsonb(d) ORDER BY job_id) FROM lab.director_stop_job_drains d
 WHERE recovery_id=p_stop),
 'outcomes',(SELECT jsonb_agg(to_jsonb(o) ORDER BY score_job_id) FROM scorer.task_terminal_outcomes o
 WHERE o.run_id=p.run_id AND o.experiment_id=s.experiment_id),
 'completions_count',(SELECT count(*) FROM scorer.task_completions c
 WHERE c.run_id=p.run_id AND c.experiment_id=s.experiment_id),
 'completions_sha256',(SELECT encode(sha256(convert_to(
 coalesce(jsonb_agg(to_jsonb(c) ORDER BY task_id,seed),'[]'::jsonb)::text,'UTF8')),'hex')
 FROM scorer.task_completions c WHERE c.run_id=p.run_id AND c.experiment_id=s.experiment_id))::text
 FROM lab.director_stop_closures p JOIN lab.director_stopped_proposals s USING(recovery_id)
 WHERE p.recovery_id=p_stop
 $$;

""")
    op.execute(r"""
CREATE FUNCTION lab.begin_attempted_proposal_stop(p_stop uuid,p_proposal text)
RETURNS double precision LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.director_recoveries%ROWTYPE; g lab.director_owner_generations%ROWTYPE;
 a lab.director_stop_closures%ROWTYPE; e lab.experiments%ROWTYPE; x lab.director_execution_contracts%ROWTYPE;
 envelope jsonb; envelope_raw text; inventory text; v jsonb; h text; reservation text; reconciled text; existing lab.director_stopped_proposals%ROWTYPE;
BEGIN
 IF session_user<>'swapp_lab_director' OR p_stop IS NULL OR p_proposal IS NULL OR octet_length(p_proposal)>4194304
 THEN RAISE EXCEPTION 'Director bounded stop proposal identity required'; END IF;
 envelope_raw:=p_proposal; envelope:=p_proposal::jsonb;
 IF envelope->>'schema' IS DISTINCT FROM 'attempted-proposal-stop.v1' OR
 jsonb_typeof(envelope->'expected_owner') IS DISTINCT FROM 'object' OR
 jsonb_typeof(envelope->'proposal_text') IS DISTINCT FROM 'string' OR
 jsonb_typeof(envelope->'admitted_inventory_text') IS DISTINCT FROM 'string' THEN
 RAISE EXCEPTION 'attempted stop envelope missing exact bindings'; END IF;
 inventory:=envelope->>'admitted_inventory_text';
 IF octet_length(inventory)>2097152 OR jsonb_typeof(inventory::jsonb) IS DISTINCT FROM 'array'
 THEN RAISE EXCEPTION 'attempted inventory exceeds bounded array'; END IF;
 p_proposal:=envelope->>'proposal_text';
 SELECT * INTO r FROM lab.director_recoveries WHERE recovery_id=p_stop;
 IF r.run_id IS NULL OR r.action IS DISTINCT FROM 'stop_and_finalize' OR r.state NOT IN ('started','pending')
 THEN RAISE EXCEPTION 'stop recovery intent missing'; END IF;
 PERFORM lab.lock_run_plan(r.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=r.run_id AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'proposal closure requires stopped request'; END IF;
 SELECT generation.* INTO g FROM lab.director_owner_generations generation JOIN lab.director_execution_control c
 ON c.run_id=generation.run_id AND c.current_generation=generation.generation WHERE c.run_id=r.run_id AND c.mode='active';
 SELECT * INTO x FROM lab.director_execution_contracts WHERE run_id=r.run_id;
 IF g.run_id IS NULL OR x.execution_sha256 IS DISTINCT FROM g.execution_sha256 OR
 x.payload_sha256 IS DISTINCT FROM (SELECT payload_sha256 FROM lab.runs WHERE run_id=r.run_id) OR
 (g.worker_pid,g.worker_start_ticks,g.worker_boot_id,g.worker_unit,g.worker_invocation_id,g.worker_cgroup)
 IS DISTINCT FROM (r.owner_pid,r.owner_start_ticks,r.owner_boot_id,r.owner_unit,r.owner_invocation_id,r.owner_cgroup)
 THEN RAISE EXCEPTION 'stop proposal owner is not the current generation'; END IF;
 IF envelope->'expected_owner' IS DISTINCT FROM jsonb_build_object(
 'generation',g.generation,'invocation_id',g.worker_invocation_id,
 'execution_sha256',g.execution_sha256) THEN RAISE EXCEPTION
 'attempted stop expected owner changed'; END IF;
 PERFORM lab.assert_attempted_stop_window(r.run_id,p_stop);
 v:=p_proposal::jsonb; h:=encode(sha256(convert_to(p_proposal,'UTF8')),'hex');
 SELECT * INTO e FROM lab.experiments WHERE run_id=r.run_id AND experiment_id=v->>'experiment_id';
 IF e.experiment_id IS NULL OR v->>'schema' IS DISTINCT FROM 'director-proposal-checkpoint.v1' OR
 v->>'run_id' IS DISTINCT FROM r.run_id::text OR (v->>'experiment_number')::integer IS DISTINCT FROM e.experiment_number OR
 v->>'candidate_sha256' IS DISTINCT FROM e.candidate_sha256 OR v->>'candidate_blob_sha256' IS DISTINCT FROM e.candidate_blob_sha256 OR
 v->>'inputs_sha256' IS DISTINCT FROM e.inputs_sha256 OR NOT (e.proposal_json @> (v->'proposal')) OR v->>'calibration_sha256' IS DISTINCT FROM e.calibration_sha256 OR
 v->>'calibration_sha256' IS DISTINCT FROM (SELECT calibration_sha256 FROM lab.baseline_calibrations WHERE run_id=r.run_id) OR
 v->>'harness_sha256' IS DISTINCT FROM x.execution_json->>'harness_sha256' OR
 v->>'image_sha256' IS DISTINCT FROM x.execution_json->>'image_sha256' OR NOT EXISTS(
 SELECT 1 FROM lab.run_events WHERE run_id=r.run_id AND event_type='director.checkpoint'
 AND event_json->>'key'='proposal:'||e.experiment_number::text AND event_json->>'phase'='proposal_registered'
 AND event_json->>'payload_sha256'=h AND event_json->>'blob_sha256'=h) THEN
 RAISE EXCEPTION 'stopped proposal differs from original immutable checkpoint'; END IF;
 IF e.status='abandoned' AND NOT EXISTS(SELECT 1 FROM lab.director_stopped_proposals
 WHERE recovery_id=p_stop AND experiment_id=e.experiment_id) THEN
 RAISE EXCEPTION 'abandoned proposal has no existing exact stop closure'; END IF;
 PERFORM lab.assert_attempted_proposal_shape(r.run_id,e.experiment_id,g.generation,g.execution_sha256);
 SELECT event_json->>'payload_sha256' INTO reservation FROM lab.run_events WHERE run_id=r.run_id
 AND event_type='director.checkpoint' AND event_json->>'key'='proposal-budget-reservation:'||r.run_id::text||':'||e.experiment_number::text;
 SELECT event_json->>'payload_sha256' INTO reconciled FROM lab.run_events WHERE run_id=r.run_id
 AND event_type='director.checkpoint' AND event_json->>'key'='proposal-budget-reconciled:'||r.run_id::text||':'||e.experiment_number::text;
 IF reservation IS NULL THEN RAISE EXCEPTION 'stopped proposal budget evidence missing'; END IF;
 INSERT INTO lab.director_stop_closures(recovery_id,run_id,expected_generation,execution_sha256,owner_json)
 VALUES(p_stop,r.run_id,g.generation,g.execution_sha256,jsonb_build_object(
 'payload_sha256',(SELECT payload_sha256 FROM lab.runs WHERE run_id=r.run_id),
 'worker_pid',r.owner_pid,'worker_start_ticks',r.owner_start_ticks,'worker_boot_id',r.owner_boot_id,
 'worker_unit',r.owner_unit,'worker_invocation_id',r.owner_invocation_id,'worker_cgroup',r.owner_cgroup)) ON CONFLICT DO NOTHING;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF (a.run_id,a.expected_generation,a.execution_sha256) IS DISTINCT FROM (r.run_id,g.generation,g.execution_sha256)
 THEN RAISE EXCEPTION 'stop proposal closure identity changed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop) AND
 (EXISTS(SELECT 1 FROM lab.experiment_records WHERE experiment_id=e.experiment_id) OR
 EXISTS(SELECT 1 FROM lab.trajectory_records WHERE experiment_id=e.experiment_id) OR
 EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=r.run_id AND experiment_id=e.experiment_id
 AND state NOT IN ('queued','running','completed','failed'))) THEN RAISE EXCEPTION
 'attempted admission already has terminal document or failed job'; END IF;
 IF inventory::jsonb IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(j) ORDER BY j.job_id)
 FROM scorer.score_jobs j WHERE j.run_id=r.run_id AND j.experiment_id=e.experiment_id)
 AND NOT EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop
 AND stop_mode='primary_admitted' AND admitted_inventory_text=inventory) THEN
 RAISE EXCEPTION 'attempted inventory differs from exact admitted jobs'; END IF;

 INSERT INTO lab.director_stopped_proposals(recovery_id,experiment_id,proposal_json,
 proposal_sha256,reservation_sha256,reconciled_sha256,calibration_sha256,stop_mode,
 admitted_inventory_text,admitted_inventory,admitted_inventory_sha256,
 baseline_inventory_sha256,stop_envelope_sha256)
 VALUES(p_stop,e.experiment_id,v,h,reservation,reconciled,e.calibration_sha256,'primary_admitted',
 inventory,inventory::jsonb,encode(sha256(convert_to(inventory,'UTF8')),'hex'),
 lab.attempted_retained_inventory_sha256(r.run_id),
 encode(sha256(convert_to(envelope_raw,'UTF8')),'hex')) ON CONFLICT DO NOTHING;
 SELECT * INTO existing FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF (existing.experiment_id,existing.proposal_json,existing.proposal_sha256,existing.reservation_sha256,existing.reconciled_sha256,existing.calibration_sha256)
 IS DISTINCT FROM (e.experiment_id,v,h,reservation,reconciled,e.calibration_sha256) THEN RAISE EXCEPTION 'stopped proposal receipt changed'; END IF;
 IF existing.stop_mode IS DISTINCT FROM 'primary_admitted' OR
 existing.admitted_inventory_text IS DISTINCT FROM inventory OR
 existing.baseline_inventory_sha256 IS DISTINCT FROM
 lab.attempted_retained_inventory_sha256(r.run_id) OR existing.stop_envelope_sha256
 IS DISTINCT FROM encode(sha256(convert_to(envelope_raw,'UTF8')),'hex') THEN
 RAISE EXCEPTION 'attempted stop immutable inventory changed'; END IF;
 RETURN lab.assert_attempted_stop_window(r.run_id,p_stop);
END $$;
""")
    op.execute(r"""
CREATE FUNCTION lab.assert_attempted_proposal_document(p_experiment text,p_document jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE; expected jsonb;
BEGIN
 IF session_user<>'swapp_lab_director' THEN RAISE EXCEPTION 'Director stop document identity required'; END IF;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE experiment_id=p_experiment;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p.recovery_id;
 IF p.experiment_id IS NULL OR a.state IS DISTINCT FROM 'drained' OR
 a.created_at+interval '120 seconds'<=clock_timestamp() OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id) WHERE r.run_id=a.run_id
 AND r.state='stop_requested' AND r.stop_requested AND c.mode='active' AND c.current_generation=a.expected_generation)
 THEN RAISE EXCEPTION 'stopped proposal document authority expired'; END IF;
 PERFORM lab.assert_director_generation_identity(a.run_id,a.expected_generation,
 a.owner_json->>'worker_invocation_id',a.execution_sha256);
 PERFORM lab.assert_attempted_proposal_shape(a.run_id,p_experiment,a.expected_generation,a.execution_sha256);
 PERFORM lab.assert_attempted_stop_window(a.run_id,a.recovery_id);
 PERFORM lab.assert_attempted_terminal_inventory(a.recovery_id);
 IF p.stop_mode IS DISTINCT FROM 'primary_admitted' OR p.terminal_inventory_sha256 IS NULL
 OR p.terminal_inventory_text IS DISTINCT FROM lab.attempted_terminal_inventory_text(p.recovery_id)
 OR p.terminal_inventory_sha256 IS DISTINCT FROM
 encode(sha256(convert_to(p.terminal_inventory_text,'UTF8')),'hex')
 THEN RAISE EXCEPTION 'attempted stop terminal receipt missing'; END IF;
 expected:=jsonb_build_object('reason','stopped_during_primary_evaluation','recovery_id',a.recovery_id,
 'run_id',a.run_id,'experiment_id',p_experiment,'generation',a.expected_generation,
 'execution_sha256',a.execution_sha256,'proposal_sha256',p.proposal_sha256,
 'reservation_sha256',p.reservation_sha256,'reconciled_sha256',p.reconciled_sha256,'calibration_sha256',p.calibration_sha256,
 'admitted_inventory_sha256',p.admitted_inventory_sha256,
 'terminal_inventory_sha256',p.terminal_inventory_sha256);
 IF p_document->'infrastructure_stop' IS DISTINCT FROM expected OR p_document->>'status' IS DISTINCT FROM 'abandoned' OR
 p_document->'decision' IS DISTINCT FROM 'null'::jsonb OR p_document->'wall_seconds' IS DISTINCT FROM 'null'::jsonb OR
 p_document->'fit_seconds' IS DISTINCT FROM 'null'::jsonb OR p_document->'score_seconds' IS DISTINCT FROM 'null'::jsonb OR
 p_document->'suite_score' IS DISTINCT FROM 'null'::jsonb OR p_document->'per_task' IS DISTINCT FROM '[]'::jsonb OR
 p_document->>'candidate_sha256' IS DISTINCT FROM p.proposal_json->>'candidate_sha256' OR
 p_document->>'inputs_sha256' IS DISTINCT FROM p.proposal_json->>'inputs_sha256' OR
 p_document->>'parent_experiment_id' IS DISTINCT FROM p.proposal_json->>'parent_experiment_id' OR
 p_document->>'parent_tree' IS DISTINCT FROM p.proposal_json->>'parent_tree_sha256' OR
 p_document->>'harness_sha256' IS DISTINCT FROM p.proposal_json->>'harness_sha256' OR
 p_document->>'image_sha256' IS DISTINCT FROM p.proposal_json->>'image_sha256' OR
 p_document->'llm_input_tokens' IS DISTINCT FROM p.proposal_json->'input_tokens' OR
 p_document->'llm_output_tokens' IS DISTINCT FROM p.proposal_json->'output_tokens' OR
 EXISTS(SELECT 1 FROM jsonb_each_text(p_document->'guards') WHERE value<>'not_run') THEN
 RAISE EXCEPTION 'stopped proposal documents cannot invent scoring or timing'; END IF;
END $$;
""")
    op.execute(r"""
CREATE FUNCTION lab.attempted_proposal_job_receipt(p_stop uuid,p_job uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE;
 result jsonb; original jsonb;
BEGIN
 IF session_user<>'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_mode IS DISTINCT FROM 'primary_admitted' OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
 WHERE r.run_id=a.run_id AND r.state='stop_requested' AND r.stop_requested
 AND c.mode='active' AND c.current_generation=a.expected_generation) THEN
 RAISE EXCEPTION 'attempted child has no active exact stop authority'; END IF;
 PERFORM lab.assert_attempted_stop_window(a.run_id,p_stop);
 PERFORM lab.assert_attempted_proposal_shape(a.run_id,p.experiment_id,a.expected_generation,a.execution_sha256);
 IF p.baseline_inventory_sha256 IS DISTINCT FROM lab.attempted_retained_inventory_sha256(a.run_id)
 THEN RAISE EXCEPTION 'attempted retained measurements changed'; END IF;
 SELECT to_jsonb(j) INTO result FROM scorer.score_jobs j WHERE j.job_id=p_job AND j.run_id=a.run_id;
 IF result IS NULL THEN RAISE EXCEPTION 'attempted job outside exact run'; END IF;
 IF result->>'experiment_id'=p.experiment_id THEN
 SELECT value INTO original FROM jsonb_array_elements(p.admitted_inventory)
 WHERE value->>'job_id'=p_job::text;
 IF original IS NULL THEN RAISE EXCEPTION 'attempted current job outside frozen inventory'; END IF;
 IF NOT lab.valid_attempted_worker_terminal(p_job,original,a.expected_generation,a.execution_sha256) AND
 ((result-ARRAY['state','error_code','updated_at','claimed_by','lease_until'])
 IS DISTINCT FROM (original-ARRAY['state','error_code','updated_at','claimed_by','lease_until'])
 OR (result->>'state' IN ('queued','running') AND result IS DISTINCT FROM original)) THEN
 RAISE EXCEPTION 'attempted current job differs from frozen original'; END IF;
 END IF;
 IF result->>'state'='completed' THEN
 SELECT result||jsonb_build_object('claim_invocation_id',s.worker_invocation_id,
 'claim_unit','swapp-ai-scientist-scorer-'||replace(p_job::text,'-','')||'.service') INTO result
 FROM scorer.task_scores s WHERE s.score_job_id=p_job;
 END IF;
 IF result->>'state'='failed' AND lab.valid_attempted_worker_terminal(p_job,
 coalesce(original,result),a.expected_generation,a.execution_sha256) THEN
 SELECT result||jsonb_build_object('claim_invocation_id',o.worker_invocation_id,
 'claim_unit','swapp-ai-scientist-scorer-'||replace(p_job::text,'-','')||'.service') INTO result
 FROM scorer.task_terminal_outcomes o WHERE o.score_job_id=p_job AND o.recovery_invocation_id IS NULL;
 END IF;
 RETURN result;
END $$;

CREATE FUNCTION lab.finish_attempted_proposal_children(p_stop uuid,p_recovery text) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE;
 terminal_text text; terminal_hash text;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
 THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 PERFORM lab.lock_run_plan(a.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=a.run_id AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'attempted finish run is no longer stopped request'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop FOR UPDATE;
 PERFORM 1 FROM lab.director_execution_control WHERE run_id=a.run_id AND mode='active'
 AND current_generation=a.expected_generation FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'attempted finish generation changed'; END IF;
 IF p.stop_mode IS DISTINCT FROM 'primary_admitted' OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id)
 WHERE r.run_id=a.run_id AND r.state='stop_requested' AND r.stop_requested
 AND c.mode='active' AND c.current_generation=a.expected_generation) OR
 a.state NOT IN ('pending','drained') THEN RAISE EXCEPTION
 'attempted finish authority changed'; END IF;
 PERFORM lab.assert_attempted_stop_window(a.run_id,p_stop);
 PERFORM lab.assert_attempted_proposal_shape(a.run_id,p.experiment_id,a.expected_generation,a.execution_sha256);
 PERFORM lab.assert_attempted_terminal_inventory(p_stop);
 IF EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=a.run_id AND state IN ('queued','running'))
 THEN RAISE EXCEPTION 'attempted jobs remain active'; END IF;
 terminal_text:=lab.attempted_terminal_inventory_text(p_stop);
 terminal_hash:=encode(sha256(convert_to(terminal_text,'UTF8')),'hex');
 IF a.state='drained' THEN
 IF a.recovery_invocation IS DISTINCT FROM p_recovery OR p.terminal_inventory_text IS DISTINCT FROM terminal_text
 OR p.terminal_inventory_sha256 IS DISTINCT FROM terminal_hash THEN RAISE EXCEPTION
 'attempted finish replay identity changed'; END IF;
 RETURN 'drained'; END IF;
 IF EXISTS(SELECT 1 FROM lab.director_stop_job_drains WHERE recovery_id=p_stop
 AND recovery_invocation<>p_recovery) THEN RAISE EXCEPTION
 'attempted child proof belongs to another recovery invocation'; END IF;
 UPDATE lab.director_stop_closures SET state='drained',recovery_invocation=p_recovery WHERE recovery_id=p_stop;
 UPDATE lab.director_stopped_proposals SET terminal_inventory_text=terminal_text,
 terminal_inventory_sha256=terminal_hash WHERE recovery_id=p_stop;
 RETURN 'drained';
END $$;

CREATE FUNCTION lab.guard_attempted_stop_marker() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'stop marker is immutable'; END IF;
 IF TG_OP='INSERT' THEN
 IF session_user<>'swapp_lab_director' THEN RAISE EXCEPTION 'Director stop marker identity required'; END IF;
 RETURN NEW; END IF;
 IF OLD.stop_mode<>'primary_admitted' OR session_user<>'swapp_lab_scorer' OR
 (to_jsonb(NEW)-ARRAY['terminal_inventory_text','terminal_inventory_sha256']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['terminal_inventory_text','terminal_inventory_sha256']) OR
 OLD.terminal_inventory_text IS NOT NULL OR OLD.terminal_inventory_sha256 IS NOT NULL OR
 NEW.terminal_inventory_text IS DISTINCT FROM lab.attempted_terminal_inventory_text(NEW.recovery_id) OR
 NEW.terminal_inventory_sha256 IS DISTINCT FROM encode(sha256(convert_to(NEW.terminal_inventory_text,'UTF8')),'hex') OR
 NOT EXISTS(SELECT 1 FROM lab.director_stop_closures WHERE recovery_id=NEW.recovery_id AND state='drained') THEN
 RAISE EXCEPTION 'stop marker only permits exact first terminal receipt'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER attempted_stop_marker_immutable BEFORE INSERT OR UPDATE OR DELETE
 ON lab.director_stopped_proposals FOR EACH ROW EXECUTE FUNCTION lab.guard_attempted_stop_marker();

-- Clone existing trusted implementations byte-for-byte, with ONLY their names changed.
-- Private clones retain session_user checks and SECURITY DEFINER fixed search_path.
DO $$ DECLARE definition text; pair text[]; BEGIN
 FOREACH pair SLICE 1 IN ARRAY ARRAY[
 ['begin_stopped_proposal_closure','begin_unattempted_proposal_closure'],
 ['stopped_proposal_job_receipt','unattempted_proposal_job_receipt'],
 ['finish_stopped_proposal_children','finish_unattempted_proposal_children'],
 ['assert_stopped_proposal_document','assert_unattempted_proposal_document'],
 ['reconcile_stopped_score_job','reconcile_unextended_stopped_score_job']]
 LOOP
 SELECT pg_get_functiondef(p.oid) INTO definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='lab' AND p.proname=pair[1];
 IF definition IS NULL THEN RAISE EXCEPTION 'original stopped RPC missing'; END IF;
 IF position('FUNCTION lab.'||pair[1]||'(' in definition)=0 THEN
 RAISE EXCEPTION 'original stopped RPC definition shape changed'; END IF;
 definition:=replace(definition,'FUNCTION lab.'||pair[1]||'(', 'FUNCTION lab.'||pair[2]||'(');
 EXECUTE definition;
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION lab.begin_stopped_proposal_closure(p_stop uuid,p_proposal text)
 RETURNS double precision LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF session_user<>'swapp_lab_director' OR p_proposal IS NULL OR octet_length(p_proposal)>4194304
 THEN RAISE EXCEPTION 'Director bounded stop proposal identity required'; END IF;
 IF p_proposal::jsonb->>'schema'='attempted-proposal-stop.v1' THEN
 RETURN lab.begin_attempted_proposal_stop(p_stop,p_proposal); END IF;
 RETURN lab.begin_unattempted_proposal_closure(p_stop,p_proposal);
END $$;
CREATE OR REPLACE FUNCTION lab.stopped_proposal_job_receipt(p_stop uuid,p_job uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop AND stop_mode='primary_admitted')
 THEN RETURN lab.attempted_proposal_job_receipt(p_stop,p_job); END IF;
 RETURN lab.unattempted_proposal_job_receipt(p_stop,p_job);
END $$;
CREATE OR REPLACE FUNCTION lab.finish_stopped_proposal_children(p_stop uuid,p_recovery text) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop AND stop_mode='primary_admitted')
 THEN RETURN lab.finish_attempted_proposal_children(p_stop,p_recovery); END IF;
 RETURN lab.finish_unattempted_proposal_children(p_stop,p_recovery);
END $$;
CREATE OR REPLACE FUNCTION lab.assert_stopped_proposal_document(p_experiment text,p_document jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE experiment_id=p_experiment AND stop_mode='primary_admitted')
 THEN PERFORM lab.assert_attempted_proposal_document(p_experiment,p_document); RETURN; END IF;
 PERFORM lab.assert_unattempted_proposal_document(p_experiment,p_document);
END $$;
CREATE OR REPLACE FUNCTION lab.reconcile_stopped_score_job(p_stop uuid,p_job uuid,p_expected text,p_recovery text)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE; original jsonb;
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop AND stop_mode='primary_admitted') THEN
 IF session_user<>'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 PERFORM lab.lock_run_plan(a.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=a.run_id AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'attempted reconcile run is not stopped request'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop FOR UPDATE;
 PERFORM 1 FROM lab.director_execution_control WHERE run_id=a.run_id AND mode='active'
 AND current_generation=a.expected_generation FOR UPDATE;
 IF NOT FOUND OR a.state NOT IN ('pending','drained') THEN RAISE EXCEPTION
 'attempted reconcile control generation changed'; END IF;
 PERFORM lab.assert_attempted_stop_window(a.run_id,p_stop);
 SELECT value INTO original FROM jsonb_array_elements(p.admitted_inventory)
 WHERE value->>'job_id'=p_job::text;
 IF original IS NULL OR original->>'run_id' IS DISTINCT FROM a.run_id::text OR
 original->>'experiment_id' IS DISTINCT FROM p.experiment_id OR original->>'evaluation_kind' IS DISTINCT FROM 'primary'
 OR original->>'seed' IS DISTINCT FROM '0' OR original->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text
 OR original->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 THEN RAISE EXCEPTION
 'attempted reconcile job outside immutable admitted inventory'; END IF;
 -- Original 0036 checks exact current row, generation, execution and old cleanup window atomically.
 END IF;
 RETURN lab.reconcile_unextended_stopped_score_job(p_stop,p_job,p_expected,p_recovery);
END $$;

""")
    op.execute(r"""
REVOKE ALL ON FUNCTION lab.assert_attempted_stop_window(uuid,uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.attempted_retained_inventory_sha256(uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.valid_attempted_worker_terminal(uuid,jsonb,integer,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.assert_attempted_proposal_shape(uuid,text,integer,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.assert_attempted_terminal_inventory(uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.attempted_terminal_inventory_text(uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.begin_attempted_proposal_stop(uuid,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.assert_attempted_proposal_document(text,jsonb) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.attempted_proposal_job_receipt(uuid,uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.finish_attempted_proposal_children(uuid,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.guard_attempted_stop_marker() FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.begin_unattempted_proposal_closure(uuid,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.unattempted_proposal_job_receipt(uuid,uuid) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.finish_unattempted_proposal_children(uuid,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.assert_unattempted_proposal_document(text,jsonb) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.reconcile_unextended_stopped_score_job(uuid,uuid,text,text) FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
""")


def downgrade() -> None:
    """Never discard immutable admitted stop evidence."""
    raise RuntimeError("attempted proposal stop receipts cannot be downgraded")
