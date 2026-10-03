"""Proof-bearing native stop for a registered baseline with no admitted score jobs."""

from alembic import op

revision = "0041_zero_job_baseline_stop"
down_revision = "0040_attempted_stop_context_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add one immutable native proof path; preserve all existing job authorities."""
    op.execute(r"""
CREATE TABLE lab.director_empty_baseline_stop_evidence(
 recovery_id uuid NOT NULL REFERENCES lab.director_stop_closures(recovery_id),
 phase text NOT NULL CHECK(phase IN ('register','seal','retire')),
 evidence_json jsonb NOT NULL,
 evidence_sha256 text NOT NULL CHECK(evidence_sha256=encode(sha256(
 convert_to(evidence_json::text,'UTF8')),'hex')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(recovery_id,phase)
);
REVOKE ALL ON lab.director_empty_baseline_stop_evidence FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.director_empty_baseline_stop_evidence TO
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
CREATE FUNCTION lab.guard_empty_baseline_stop_evidence() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF TG_OP<>'INSERT' OR
 (NEW.phase IN ('register','seal') AND session_user<>'swapp_lab_scorer') OR
 (NEW.phase='retire' AND session_user<>'swapp_lab_director') THEN
 RAISE EXCEPTION 'empty baseline native evidence is append only'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER empty_baseline_stop_evidence_immutable
 BEFORE INSERT OR UPDATE OR DELETE ON lab.director_empty_baseline_stop_evidence
 FOR EACH ROW EXECUTE FUNCTION lab.guard_empty_baseline_stop_evidence();

CREATE FUNCTION lab.empty_baseline_stop_context(p_stop uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; r lab.runs%ROWTYPE;
 g lab.director_owner_generations%ROWTYPE; intent lab.director_recoveries%ROWTYPE;
 plan jsonb; baselines jsonb; owner jsonb;
BEGIN
 IF session_user NOT IN ('swapp_lab_scorer','swapp_lab_director','swapp_lab_planner')
 OR p_stop IS NULL THEN RAISE EXCEPTION 'empty baseline stop role required'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF a.run_id IS NULL THEN RAISE EXCEPTION 'empty baseline stop intent missing'; END IF;
 -- Same private transaction lock as 0040; no public lock permission is expanded.
 IF session_user='swapp_lab_scorer' THEN PERFORM pg_advisory_xact_lock(
 ('x'||substr(encode(sha256(uuid_send(a.run_id)),'hex'),1,16))::bit(64)::bigint);
 ELSE PERFORM lab.lock_run_plan(a.run_id); END IF;
 SELECT * INTO r FROM lab.runs WHERE run_id=a.run_id FOR UPDATE;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 PERFORM 1 FROM lab.director_execution_control WHERE run_id=a.run_id AND mode='active'
 AND current_generation=a.expected_generation FOR UPDATE;
 IF NOT FOUND OR r.state IS DISTINCT FROM 'stop_requested' OR NOT r.stop_requested
 OR a.created_at+interval '120 seconds'<=clock_timestamp()
 OR a.state NOT IN ('pending','drained') THEN
 RAISE EXCEPTION 'empty baseline original stop authority expired'; END IF;
 SELECT * INTO g FROM lab.director_owner_generations
 WHERE run_id=a.run_id AND generation=a.expected_generation;
 SELECT * INTO intent FROM lab.director_recoveries WHERE recovery_id=p_stop;
 owner:=jsonb_build_object('payload_sha256',r.payload_sha256,'worker_pid',g.worker_pid,
 'worker_start_ticks',g.worker_start_ticks,'worker_boot_id',g.worker_boot_id,
 'worker_unit',g.worker_unit,'worker_invocation_id',g.worker_invocation_id,
 'worker_cgroup',g.worker_cgroup);
 IF g.run_id IS NULL OR g.execution_sha256 IS DISTINCT FROM a.execution_sha256
 OR owner IS DISTINCT FROM a.owner_json OR intent.run_id IS DISTINCT FROM a.run_id
 OR intent.action IS DISTINCT FROM 'stop_and_finalize' OR intent.state NOT IN ('started','pending')
 OR (intent.owner_pid,intent.owner_start_ticks,intent.owner_boot_id,intent.owner_unit,
 intent.owner_invocation_id,intent.owner_cgroup) IS DISTINCT FROM
 (g.worker_pid,g.worker_start_ticks,g.worker_boot_id,g.worker_unit,
 g.worker_invocation_id,g.worker_cgroup)
 OR g.worker_unit !~ ('^swapp-ai-scientist-director-(dispatch-'||replace(a.run_id::text,'-','')||
 '|resume-'||replace(a.run_id::text,'-','')||'-[0-9a-f]{32})[.]service$')
 OR g.worker_cgroup NOT LIKE '%/'||g.worker_unit
 OR left(g.worker_cgroup,1)<>'/' OR position('..' in g.worker_cgroup)>0
 OR NOT EXISTS(SELECT 1 FROM lab.director_execution_contracts x WHERE x.run_id=a.run_id
 AND x.execution_sha256=a.execution_sha256)
 THEN RAISE EXCEPTION 'empty baseline current historical owner changed'; END IF;
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop)
 OR EXISTS(SELECT 1 FROM lab.baseline_calibrations WHERE run_id=a.run_id)
 OR EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=a.run_id)
 OR EXISTS(SELECT 1 FROM scorer.task_scores WHERE run_id=a.run_id)
 OR EXISTS(SELECT 1 FROM lab.director_stop_job_drains WHERE recovery_id=p_stop)
 OR (SELECT count(*) FROM lab.experiments WHERE run_id=a.run_id) NOT BETWEEN 1 AND 3
 OR EXISTS(SELECT 1 FROM lab.experiments e WHERE e.run_id=a.run_id AND
 (e.kind<>'baseline' OR e.baseline_name NOT IN ('robust_z','iforest','ecod_train_frozen')
 OR e.baseline_name IS NULL OR e.status NOT IN ('proposed','primary_running','abandoned')))
 THEN RAISE EXCEPTION 'empty baseline proof requires no admitted jobs or measurements'; END IF;
 SELECT jsonb_agg(to_jsonb(t) ORDER BY experiment_id,evaluation_kind,task_id,seed) INTO plan
 FROM scorer.run_tasks t WHERE t.run_id=a.run_id;
 IF plan IS NULL OR jsonb_array_length(plan) NOT BETWEEN 3 AND 9216 OR EXISTS(
 SELECT 1 FROM scorer.run_tasks t LEFT JOIN lab.experiments e
 ON e.run_id=t.run_id AND e.experiment_id=t.experiment_id
 LEFT JOIN scorer.dataset_profiles p USING(dataset_id,split_id,session_id)
 WHERE t.run_id=a.run_id AND (e.experiment_id IS NULL OR t.evaluation_kind<>'baseline'
 OR t.seed NOT BETWEEN 0 AND 2 OR t.candidate_sha256 IS DISTINCT FROM e.candidate_sha256
 OR p.visibility IS DISTINCT FROM 'dev')) OR EXISTS(
 SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id
 GROUP BY t.experiment_id,t.task_id HAVING count(*)<>3) THEN
 RAISE EXCEPTION 'empty baseline original registered plan malformed'; END IF;
 SELECT jsonb_agg(jsonb_build_object('experiment_id',e.experiment_id,'kind',e.kind,
 'baseline_name',e.baseline_name,'candidate_sha256',e.candidate_sha256,
 'candidate_blob_sha256',e.candidate_blob_sha256,'inputs_sha256',e.inputs_sha256)
 ORDER BY e.experiment_id) INTO baselines FROM lab.experiments e WHERE e.run_id=a.run_id;
 RETURN jsonb_build_object('schema','empty-baseline-stop-context.v1','recovery_id',p_stop,
 'run_id',a.run_id,'expected_generation',a.expected_generation,'execution_sha256',a.execution_sha256,
 'owner_json',owner,'request_json',r.request_json,
 'created_at',to_char(a.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
 'original_plan',plan,'original_plan_sha256',encode(sha256(convert_to(plan::text,'UTF8')),'hex'),
 'baselines',baselines);
END $$;

CREATE FUNCTION lab.assert_empty_baseline_observation(p_context jsonb,p_observation jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE properties jsonb; owner jsonb; observed timestamptz;
BEGIN
 IF jsonb_typeof(p_observation) IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_observation) key) IS DISTINCT FROM
 ARRAY['native_children_empty','observed_at','observed_boot_id','owner_json','process_retired',
 'run_id','sandbox_container_ids','schema','unit_properties']::text[] THEN
 RAISE EXCEPTION 'empty baseline native observation malformed'; END IF;
 owner:=p_context->'owner_json'; properties:=p_observation->'unit_properties';
 observed:=(p_observation->>'observed_at')::timestamptz;
 IF p_observation->>'schema' IS DISTINCT FROM 'empty-baseline-native-observation.v1'
 OR p_observation->>'run_id' IS DISTINCT FROM p_context->>'run_id'
 OR p_observation->'owner_json' IS DISTINCT FROM owner
 OR p_observation->'process_retired' IS DISTINCT FROM 'true'::jsonb
 OR p_observation->'native_children_empty' IS DISTINCT FROM 'true'::jsonb
 OR p_observation->'sandbox_container_ids' IS DISTINCT FROM '[]'::jsonb
 OR coalesce(p_observation->>'observed_boot_id','') !~ '^[0-9a-f-]{36}$'
 OR observed IS NULL OR observed<(p_context->>'created_at')::timestamptz
 OR observed>clock_timestamp() OR jsonb_typeof(properties) IS DISTINCT FROM 'object'
 THEN RAISE EXCEPTION 'empty baseline native observation does not bind this stop'; END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(properties) key) IS DISTINCT FROM
 ARRAY['ActiveState','ControlGroup','InvocationID','LoadState','MainPID']::text[]
 OR properties->>'MainPID' IS DISTINCT FROM '0'
 OR NOT coalesce(properties->>'ControlGroup' IN ('',owner->>'worker_cgroup'),false)
 OR NOT coalesce((properties->>'LoadState'='not-found' AND
 properties->>'ActiveState'='inactive' AND properties->>'InvocationID'='') OR
 (properties->>'LoadState'='loaded' AND properties->>'ActiveState' IN ('inactive','failed')
 AND properties->>'InvocationID'=owner->>'worker_invocation_id'),false) THEN
 RAISE EXCEPTION 'empty baseline native unit or cgroup is not retired'; END IF;
END $$;

CREATE FUNCTION lab.record_empty_baseline_stop(p_stop uuid,p_phase text,p_evidence text)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE context jsonb; evidence jsonb; registered jsonb; sealed jsonb; prior jsonb;
 observation jsonb; properties jsonb; identity jsonb; observed timestamptz;
BEGIN
 IF p_phase IS NULL OR p_phase NOT IN ('register','seal','retire') OR
 (p_phase IN ('register','seal') AND session_user<>'swapp_lab_scorer') OR
 (p_phase='retire' AND session_user<>'swapp_lab_director') OR p_evidence IS NULL
 OR octet_length(p_evidence)>8388608 THEN
 RAISE EXCEPTION 'empty baseline proof role required'; END IF;
 context:=lab.empty_baseline_stop_context(p_stop); evidence:=p_evidence::jsonb;
 IF jsonb_typeof(evidence) IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(evidence) key) IS DISTINCT FROM
 ARRAY['context','observation','worker_identity']::text[] OR
 evidence->'context' IS DISTINCT FROM context THEN
 RAISE EXCEPTION 'empty baseline proof owner or original plan changed'; END IF;
 identity:=evidence->'worker_identity'; PERFORM lab.assert_stop_v2_worker_identity(identity);
 SELECT evidence_json INTO prior FROM lab.director_empty_baseline_stop_evidence
 WHERE recovery_id=p_stop AND phase=p_phase;
 IF prior IS NOT NULL THEN
 IF prior IS DISTINCT FROM evidence THEN
 RAISE EXCEPTION 'empty baseline exact replay changed'; END IF;
 RETURN CASE WHEN p_phase='register' THEN 'registered' WHEN p_phase='seal' THEN 'drained'
 ELSE 'retired' END;
 END IF;
 IF EXISTS(SELECT 1 FROM scorer.task_completions WHERE run_id=(context->>'run_id')::uuid)
 OR EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=(context->>'run_id')::uuid)
 THEN RAISE EXCEPTION 'empty baseline proof cannot overwrite terminal cells'; END IF;
 SELECT evidence_json INTO registered FROM lab.director_empty_baseline_stop_evidence
 WHERE recovery_id=p_stop AND phase='register';
 IF p_phase<>'register' AND (registered IS NULL OR
 registered->'context' IS DISTINCT FROM context OR
 registered->'worker_identity' IS DISTINCT FROM identity) THEN
 RAISE EXCEPTION 'empty baseline proof actor is not the registered native worker'; END IF;
 IF p_phase IN ('register','seal') THEN
 IF NOT EXISTS(SELECT 1 FROM lab.director_stop_closures WHERE recovery_id=p_stop
 AND state='pending' AND recovery_invocation IS NULL) THEN
 RAISE EXCEPTION 'empty baseline child proof is already sealed'; END IF;
 PERFORM lab.assert_empty_baseline_observation(context,evidence->'observation');
 IF p_phase='seal' AND (evidence->'observation'->>'observed_at')::timestamptz<
 (registered->'observation'->>'observed_at')::timestamptz THEN
 RAISE EXCEPTION 'empty baseline final native observation precedes registration'; END IF;
 ELSE
 SELECT evidence_json INTO sealed FROM lab.director_empty_baseline_stop_evidence
 WHERE recovery_id=p_stop AND phase='seal';
 IF sealed IS NULL OR sealed->'worker_identity' IS DISTINCT FROM identity OR
 sealed->'context' IS DISTINCT FROM context OR NOT EXISTS(
 SELECT 1 FROM lab.director_stop_closures WHERE recovery_id=p_stop AND state='drained'
 AND recovery_invocation=identity->>'worker_invocation_id') THEN
 RAISE EXCEPTION 'empty baseline retirement lacks sealed worker'; END IF;
 observation:=evidence->'observation'; properties:=observation->'unit_properties';
 observed:=(observation->>'observed_at')::timestamptz;
 IF jsonb_typeof(observation) IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(observation) key) IS DISTINCT FROM
 ARRAY['cgroup_empty','observed_at','observed_boot_id','process_retired','schema',
 'unit_properties','worker_identity']::text[] OR
 observation->>'schema' IS DISTINCT FROM 'attempted-stop-retirement-observation.v2'
 OR observation->'worker_identity' IS DISTINCT FROM identity
 OR observation->'process_retired' IS DISTINCT FROM 'true'::jsonb
 OR observation->'cgroup_empty' IS DISTINCT FROM 'true'::jsonb
 OR coalesce(observation->>'observed_boot_id','') !~ '^[0-9a-f-]{36}$'
 OR observed IS NULL OR observed<(sealed->'observation'->>'observed_at')::timestamptz
 OR observed>clock_timestamp() OR jsonb_typeof(properties) IS DISTINCT FROM 'object' THEN
 RAISE EXCEPTION 'empty baseline native worker retirement malformed'; END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(properties) key) IS DISTINCT FROM
 ARRAY['ActiveState','ControlGroup','InvocationID','LoadState','MainPID']::text[]
 OR properties->>'MainPID' IS DISTINCT FROM '0'
 OR NOT coalesce(properties->>'ControlGroup' IN ('',identity->>'worker_cgroup'),false)
 OR NOT coalesce((properties->>'LoadState'='not-found' AND
 properties->>'ActiveState'='inactive' AND properties->>'InvocationID'='') OR
 (properties->>'LoadState'='loaded' AND properties->>'ActiveState' IN ('inactive','failed')
 AND properties->>'InvocationID'=identity->>'worker_invocation_id'),false) THEN
 RAISE EXCEPTION 'empty baseline recovery unit or cgroup is not retired'; END IF;
 END IF;
 INSERT INTO lab.director_empty_baseline_stop_evidence(
 recovery_id,phase,evidence_json,evidence_sha256)
 VALUES(p_stop,p_phase,evidence,encode(sha256(convert_to(evidence::text,'UTF8')),'hex'));
 IF p_phase='seal' THEN UPDATE lab.director_stop_closures SET state='drained',
 recovery_invocation=identity->>'worker_invocation_id' WHERE recovery_id=p_stop; END IF;
 RETURN CASE WHEN p_phase='register' THEN 'registered' WHEN p_phase='seal' THEN 'drained'
 ELSE 'retired' END;
END $$;

-- The existing missing-cell path is available only after independent worker
-- retirement. No fake score_job or drain row supplies this new authority.
DO $copy$
DECLARE definition text;
BEGIN
 SELECT pg_get_functiondef('lab.assert_stopped_missing_transition(text,text,jsonb)'::regprocedure)
 INTO definition;
 IF position('FUNCTION lab.assert_stopped_missing_transition(' in definition)=0 THEN
 RAISE EXCEPTION 'empty baseline missing-cell guard source changed'; END IF;
 EXECUTE replace(definition,'FUNCTION lab.assert_stopped_missing_transition(',
 'FUNCTION lab.assert_stopped_missing_v40(');
END $copy$;
CREATE OR REPLACE FUNCTION lab.assert_stopped_missing_transition(
 p_table text,p_operation text,p_new jsonb)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE stop uuid; context jsonb; sealed jsonb; retired jsonb;
BEGIN
 stop:=nullif(current_setting('lab.stop_request',true),'')::uuid;
 IF EXISTS(SELECT 1 FROM lab.director_empty_baseline_stop_evidence WHERE recovery_id=stop) THEN
 context:=lab.empty_baseline_stop_context(stop);
 SELECT evidence_json INTO sealed FROM lab.director_empty_baseline_stop_evidence
 WHERE recovery_id=stop AND phase='seal';
 SELECT evidence_json INTO retired FROM lab.director_empty_baseline_stop_evidence
 WHERE recovery_id=stop AND phase='retire';
 IF sealed IS NULL OR retired IS NULL OR sealed->'context' IS DISTINCT FROM context
 OR retired->'context' IS DISTINCT FROM context OR
 sealed->'worker_identity' IS DISTINCT FROM retired->'worker_identity' THEN
 RAISE EXCEPTION 'empty baseline cells require verified native recovery retirement'; END IF;
 END IF;
 PERFORM lab.assert_stopped_missing_v40(p_table,p_operation,p_new);
END $$;
REVOKE ALL ON FUNCTION lab.guard_empty_baseline_stop_evidence(),
 lab.empty_baseline_stop_context(uuid),lab.assert_empty_baseline_observation(jsonb,jsonb),
 lab.record_empty_baseline_stop(uuid,text,text),lab.assert_stopped_missing_v40(text,text,jsonb),
 lab.assert_stopped_missing_transition(text,text,jsonb) FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT EXECUTE ON FUNCTION lab.empty_baseline_stop_context(uuid)
 TO swapp_lab_scorer,swapp_lab_director;
GRANT EXECUTE ON FUNCTION lab.record_empty_baseline_stop(uuid,text,text)
 TO swapp_lab_scorer,swapp_lab_director;
    """)


def downgrade() -> None:
    raise RuntimeError("native empty-baseline stop proof history cannot be erased")
