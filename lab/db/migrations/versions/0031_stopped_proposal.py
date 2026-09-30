"""Close one calibrated proposal stopped before candidate admission, without a verdict."""

# Fixed SQL statements implement role-bound closure, never admission.
# ruff: noqa: E501
from alembic import op

revision = "0031_stopped_proposal"
down_revision = "0030_director_resume"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Bind stop documents to the original proposal and nonrenewable cleanup window."""
    op.execute(r"""
CREATE TABLE lab.director_stopped_proposals(
 recovery_id uuid PRIMARY KEY REFERENCES lab.director_stop_closures(recovery_id),
 experiment_id text NOT NULL UNIQUE REFERENCES lab.experiments(experiment_id),
 proposal_json jsonb NOT NULL,
 proposal_sha256 text NOT NULL CHECK(proposal_sha256 ~ '^[0-9a-f]{64}$'),
 reservation_sha256 text NOT NULL CHECK(reservation_sha256 ~ '^[0-9a-f]{64}$'),
 reconciled_sha256 text CHECK(reconciled_sha256 ~ '^[0-9a-f]{64}$'),
 calibration_sha256 text NOT NULL CHECK(calibration_sha256 ~ '^[0-9a-f]{64}$')
);
REVOKE ALL ON lab.director_stopped_proposals FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_stopped_proposals TO swapp_lab_director,swapp_lab_scorer;
CREATE FUNCTION lab.assert_stopped_proposal_shape(p_run uuid,p_experiment text,p_generation integer,p_execution text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE task_count integer;
BEGIN
 SELECT c.task_count INTO task_count FROM lab.baseline_calibrations c WHERE run_id=p_run;
 IF task_count IS NULL OR task_count<1 OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=p_run AND kind='baseline')<>3 OR
 EXISTS(SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run AND e.kind='baseline' AND
  (e.status<>'scored' OR NOT EXISTS(SELECT 1 FROM lab.experiment_records r WHERE r.experiment_id=e.experiment_id)
   OR NOT EXISTS(SELECT 1 FROM lab.trajectory_records t WHERE t.experiment_id=e.experiment_id))) OR
 (SELECT count(*) FROM lab.experiments WHERE run_id=p_run AND kind='proposal')<>1 OR
 NOT EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=p_run AND experiment_id=p_experiment
  AND kind='proposal' AND status IN ('proposed','abandoned')) OR
 EXISTS(SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run AND experiment_id=p_experiment) OR
 EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run AND experiment_id=p_experiment) OR
 EXISTS(SELECT 1 FROM scorer.task_scores WHERE run_id=p_run AND experiment_id=p_experiment) OR
 (SELECT count(*) FROM scorer.run_tasks WHERE run_id=p_run)<>9*task_count OR
 (SELECT count(*) FROM scorer.task_scores WHERE run_id=p_run)<>9*task_count OR
 (SELECT count(*) FROM scorer.score_jobs WHERE run_id=p_run)<>9*task_count OR
 EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=p_run AND (
  j.state<>'completed' OR j.evaluation_kind<>'baseline' OR j.execution_sha256 IS DISTINCT FROM p_execution OR
  NOT lab.valid_director_restart_ancestry(p_run,j.admitted_generation,p_generation,p_execution) OR
  NOT EXISTS(SELECT 1 FROM scorer.task_completions c WHERE c.run_id=j.run_id AND c.experiment_id=j.experiment_id
   AND c.evaluation_kind=j.evaluation_kind AND c.task_id=j.task_id AND c.seed=j.seed AND c.completion_kind='scored') OR
  NOT EXISTS(SELECT 1 FROM scorer.task_scores s WHERE s.run_id=j.run_id AND s.experiment_id=j.experiment_id
   AND s.evaluation_kind=j.evaluation_kind AND s.task_id=j.task_id AND s.seed=j.seed
   -- The verified transient claim token is erased by guard_task_score_invocation.
   AND s.score_job_id=j.job_id AND s.claim_token IS NULL
   AND s.worker_invocation_id ~ '^[0-9a-f]{32}$'))) THEN
 RAISE EXCEPTION 'stopped proposal requires complete calibration and no candidate admission'; END IF;
END $$;
REVOKE ALL ON FUNCTION lab.assert_stopped_proposal_shape(uuid,text,integer,text) FROM PUBLIC;
CREATE FUNCTION lab.begin_stopped_proposal_closure(p_stop uuid,p_proposal text)
RETURNS double precision LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.director_recoveries%ROWTYPE; g lab.director_owner_generations%ROWTYPE;
 a lab.director_stop_closures%ROWTYPE; e lab.experiments%ROWTYPE; x lab.director_execution_contracts%ROWTYPE;
 v jsonb; h text; reservation text; reconciled text; existing lab.director_stopped_proposals%ROWTYPE;
BEGIN
 IF session_user<>'swapp_lab_director' OR p_stop IS NULL OR p_proposal IS NULL OR octet_length(p_proposal)>2097152
 THEN RAISE EXCEPTION 'Director bounded stop proposal identity required'; END IF;
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
 PERFORM lab.assert_stopped_proposal_shape(r.run_id,e.experiment_id,g.generation,g.execution_sha256);
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
 INSERT INTO lab.director_stopped_proposals VALUES(p_stop,e.experiment_id,v,h,reservation,reconciled,e.calibration_sha256) ON CONFLICT DO NOTHING;
 SELECT * INTO existing FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF (existing.experiment_id,existing.proposal_json,existing.proposal_sha256,existing.reservation_sha256,existing.reconciled_sha256,existing.calibration_sha256)
 IS DISTINCT FROM (e.experiment_id,v,h,reservation,reconciled,e.calibration_sha256) THEN RAISE EXCEPTION 'stopped proposal receipt changed'; END IF;
 RETURN greatest(0,extract(epoch FROM a.created_at+interval '120 seconds'-clock_timestamp()));
END $$;
REVOKE ALL ON FUNCTION lab.begin_stopped_proposal_closure(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.begin_stopped_proposal_closure(uuid,text) TO swapp_lab_director;
""")
    op.execute(r"""
CREATE FUNCTION lab.stopped_proposal_job_receipt(p_stop uuid,p_job uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; target text; result jsonb;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_stop IS NULL OR p_job IS NULL THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 SELECT experiment_id INTO target FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF a.run_id IS NULL OR target IS NULL OR a.created_at+interval '120 seconds'<=clock_timestamp() OR NOT EXISTS(
 SELECT 1 FROM lab.runs r JOIN lab.director_execution_control c USING(run_id) WHERE r.run_id=a.run_id
 AND r.state='stop_requested' AND r.stop_requested AND c.mode='active' AND c.current_generation=a.expected_generation)
 THEN RAISE EXCEPTION 'stop proposal cleanup authority expired'; END IF;
 PERFORM lab.assert_stopped_proposal_shape(a.run_id,target,a.expected_generation,a.execution_sha256);
 SELECT to_jsonb(j)||jsonb_build_object('claim_invocation_id',s.worker_invocation_id,
 'claim_unit','swapp-ai-scientist-scorer-'||replace(j.job_id::text,'-','')||'.service') INTO result
 FROM scorer.score_jobs j JOIN scorer.task_scores s ON s.score_job_id=j.job_id
 WHERE j.job_id=p_job AND j.run_id=a.run_id;
 IF result IS NULL THEN RAISE EXCEPTION 'stop proposal job not in original inventory'; END IF;
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION lab.stopped_proposal_job_receipt(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.stopped_proposal_job_receipt(uuid,uuid) TO swapp_lab_scorer;
CREATE FUNCTION lab.finish_stopped_proposal_children(p_stop uuid,p_recovery text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; target text; first_job uuid;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$'
 THEN RAISE EXCEPTION 'Scorer stop identity required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT experiment_id INTO target FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 SELECT job_id INTO first_job FROM scorer.score_jobs WHERE run_id=a.run_id ORDER BY job_id LIMIT 1;
 PERFORM lab.stopped_proposal_job_receipt(p_stop,first_job);
 UPDATE lab.director_stop_closures SET state='drained',recovery_invocation=coalesce(recovery_invocation,p_recovery)
 WHERE recovery_id=p_stop;
 RETURN 'drained';
END $$;
REVOKE ALL ON FUNCTION lab.finish_stopped_proposal_children(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.finish_stopped_proposal_children(uuid,text) TO swapp_lab_scorer;
CREATE FUNCTION lab.assert_stopped_proposal_document(p_experiment text,p_document jsonb) RETURNS void
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
 PERFORM lab.assert_stopped_proposal_shape(a.run_id,p_experiment,a.expected_generation,a.execution_sha256);
 expected:=jsonb_build_object('reason','stopped_before_candidate_admission','recovery_id',a.recovery_id,
 'run_id',a.run_id,'experiment_id',p_experiment,'generation',a.expected_generation,
 'execution_sha256',a.execution_sha256,'proposal_sha256',p.proposal_sha256,
 'reservation_sha256',p.reservation_sha256,'reconciled_sha256',p.reconciled_sha256,'calibration_sha256',p.calibration_sha256);
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
REVOKE ALL ON FUNCTION lab.assert_stopped_proposal_document(text,jsonb) FROM PUBLIC;
""")
    # Extend only the null-verdict exception; preserve every existing identity guard.
    op.execute(r"""
DO $$ DECLARE definition text; needle text;
BEGIN
 definition:=pg_get_functiondef('lab.guard_experiment_record_kind_identity()'::regprocedure);
 needle:='IF exp.kind = ''proposal'' AND (';
 IF position(needle in definition)=0 THEN RAISE EXCEPTION 'proposal record guard shape changed'; END IF;
 definition:=replace(definition,needle,
 'IF NEW.experiment_json ? ''infrastructure_stop'' THEN
 PERFORM lab.assert_stopped_proposal_document(NEW.experiment_id,NEW.experiment_json);
 RETURN NEW;
 END IF;
 '||needle);
 EXECUTE definition;
 definition:=pg_get_functiondef('lab.guard_trajectory_kind_identity()'::regprocedure);
 needle:='RETURN NEW;';
 IF position(needle in definition)=0 THEN RAISE EXCEPTION 'trajectory guard shape changed'; END IF;
 definition:=replace(definition,needle,
 'IF rec.experiment_json ? ''infrastructure_stop'' OR NEW.trajectory_json ? ''infrastructure_stop'' THEN
 PERFORM lab.assert_stopped_proposal_document(NEW.experiment_id,rec.experiment_json);
 IF NEW.trajectory_json->''infrastructure_stop'' IS DISTINCT FROM rec.experiment_json->''infrastructure_stop'' OR
 NEW.trajectory_json->>''messages_blob_sha256'' IS DISTINCT FROM (SELECT proposal_json->>''messages_blob_sha256''
 FROM lab.director_stopped_proposals WHERE experiment_id=NEW.experiment_id)
 THEN RAISE EXCEPTION ''stop trajectory original provenance changed''; END IF;
 END IF;
 RETURN NEW;');
 EXECUTE definition;
END $$;
""")


def downgrade() -> None:
    raise RuntimeError("stopped proposal evidence is immutable; downgrade is unsupported")
