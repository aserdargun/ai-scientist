"""Complete original primary plans and retain genuine stop recovery ancestry."""

# Fixed, bounded role RPC SQL; old implementations remain the exact fallback.
# ruff: noqa: E501
from alembic import op

revision = "0038_attempted_stop_recovery"
down_revision = "0037_attempted_proposal_stop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Keep one original stop window, immutable plan, and append-only actor proofs."""
    op.execute(r"""
ALTER TABLE lab.director_stopped_proposals
 ADD COLUMN stop_protocol_version integer NOT NULL DEFAULT 1 CHECK(stop_protocol_version IN (1,2)),
 ADD COLUMN original_primary_plan_text text, ADD COLUMN original_primary_plan_json jsonb,
 ADD COLUMN original_primary_plan_sha256 text, ADD COLUMN child_inventory_text text,
 ADD COLUMN child_inventory_sha256 text, ADD COLUMN missing_primary_inventory_text text,
 ADD COLUMN missing_primary_inventory_sha256 text,
 ADD COLUMN recovery_roster_json jsonb,
 ADD CONSTRAINT attempted_stop_v2_shape CHECK(
 (stop_protocol_version=1 AND original_primary_plan_text IS NULL AND original_primary_plan_json IS NULL
 AND original_primary_plan_sha256 IS NULL AND child_inventory_text IS NULL AND child_inventory_sha256 IS NULL
 AND missing_primary_inventory_text IS NULL AND missing_primary_inventory_sha256 IS NULL AND recovery_roster_json IS NULL) OR
 (stop_protocol_version=2 AND stop_mode='primary_admitted' AND original_primary_plan_text IS NOT NULL
 AND octet_length(original_primary_plan_text)<=2097152 AND original_primary_plan_json IS NOT NULL
 AND jsonb_typeof(original_primary_plan_json)='array' AND original_primary_plan_json=original_primary_plan_text::jsonb
 AND original_primary_plan_sha256 IS NOT NULL AND original_primary_plan_sha256 ~ '^[0-9a-f]{64}$'
 AND recovery_roster_json IS NOT NULL AND jsonb_typeof(recovery_roster_json)='object'
 AND ((child_inventory_text IS NULL AND child_inventory_sha256 IS NULL) OR
 (child_inventory_text IS NOT NULL AND child_inventory_sha256 IS NOT NULL AND child_inventory_sha256 ~ '^[0-9a-f]{64}$'))
 AND ((missing_primary_inventory_text IS NULL AND missing_primary_inventory_sha256 IS NULL) OR
 (missing_primary_inventory_text IS NOT NULL AND missing_primary_inventory_sha256 IS NOT NULL AND missing_primary_inventory_sha256 ~ '^[0-9a-f]{64}$'))));
CREATE TABLE lab.attempted_stop_recovery_attempts(
 recovery_id uuid NOT NULL REFERENCES lab.director_stop_closures(recovery_id),
 attempt_ordinal integer NOT NULL CHECK(attempt_ordinal BETWEEN 1 AND 32),
 worker_identity jsonb NOT NULL CHECK(jsonb_typeof(worker_identity)='object'),
 recovery_invocation_id text NOT NULL CHECK(recovery_invocation_id ~ '^[0-9a-f]{32}$'),
 expected_generation integer NOT NULL CHECK(expected_generation>0),
 execution_sha256 text NOT NULL CHECK(execution_sha256 ~ '^[0-9a-f]{64}$'),
 original_primary_plan_sha256 text NOT NULL CHECK(original_primary_plan_sha256 ~ '^[0-9a-f]{64}$'),
 registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(recovery_id,attempt_ordinal),UNIQUE(recovery_id,recovery_invocation_id));
CREATE TABLE lab.attempted_stop_recovery_retirements(
 recovery_id uuid NOT NULL,retired_ordinal integer NOT NULL,
 identity_sha256 text NOT NULL CHECK(identity_sha256 ~ '^[0-9a-f]{64}$'),
 observation_text text NOT NULL CHECK(octet_length(observation_text)<=32768),
 observation_sha256 text NOT NULL CHECK(observation_sha256 ~ '^[0-9a-f]{64}$'),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(recovery_id,retired_ordinal),FOREIGN KEY(recovery_id,retired_ordinal)
 REFERENCES lab.attempted_stop_recovery_attempts(recovery_id,attempt_ordinal));
REVOKE ALL ON lab.attempted_stop_recovery_attempts,lab.attempted_stop_recovery_retirements
 FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;

DO $$ DECLARE definition text; pair text[]; BEGIN
 FOREACH pair SLICE 1 IN ARRAY ARRAY[
 ['begin_stopped_proposal_closure','begin_proposal_stop_v37'],
 ['stopped_proposal_job_receipt','proposal_stop_job_receipt_v37'],
 ['finish_stopped_proposal_children','finish_proposal_stop_children_v37'],
 ['reconcile_stopped_score_job','reconcile_stop_job_v37'],
 ['close_stopped_unattempted_tasks','close_stopped_missing_v37'],
 ['assert_attempted_terminal_inventory','assert_attempted_terminal_inventory_v37'],
 ['attempted_terminal_inventory_text','attempted_terminal_inventory_text_v37']]
 LOOP
 SELECT pg_get_functiondef(p.oid) INTO definition FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname='lab' AND p.proname=pair[1];
 IF definition IS NULL OR position('FUNCTION lab.'||pair[1]||'(' in definition)=0 THEN
 RAISE EXCEPTION 'stop v37 function shape changed'; END IF;
 EXECUTE replace(definition,'FUNCTION lab.'||pair[1]||'(', 'FUNCTION lab.'||pair[2]||'(');
 END LOOP;
END $$;

CREATE FUNCTION lab.stop_v2_roster(p_stop uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('attempts',coalesce((SELECT jsonb_agg(worker_identity||jsonb_build_object(
 'attempt_ordinal',attempt_ordinal,'identity_text',worker_identity::text,
 'identity_sha256',encode(sha256(convert_to(worker_identity::text,'UTF8')),'hex'),'expected_generation',expected_generation,'execution_sha256',execution_sha256,
 'original_primary_plan_sha256',original_primary_plan_sha256) ORDER BY attempt_ordinal)
 FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop),'[]'::jsonb),
 'retirements',coalesce((SELECT jsonb_agg(jsonb_build_object('retired_ordinal',retired_ordinal,
 'identity_sha256',identity_sha256,'observation_sha256',observation_sha256) ORDER BY retired_ordinal)
 FROM lab.attempted_stop_recovery_retirements WHERE recovery_id=p_stop),'[]'::jsonb))
 $$;
CREATE FUNCTION lab.assert_stop_v2_context(p_stop uuid,p_owner jsonb DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE; g lab.director_owner_generations%ROWTYPE;
BEGIN
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF a.run_id IS NULL THEN RAISE EXCEPTION 'stop v2 closure missing'; END IF;
 PERFORM lab.lock_run_plan(a.run_id);
 PERFORM 1 FROM lab.runs WHERE run_id=a.run_id AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'stop v2 run is no longer requested'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop FOR UPDATE;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop FOR UPDATE;
 PERFORM 1 FROM lab.director_execution_control WHERE run_id=a.run_id AND mode='active'
 AND current_generation=a.expected_generation FOR UPDATE;
 IF NOT FOUND OR p.stop_protocol_version IS DISTINCT FROM 2 OR p.stop_mode IS DISTINCT FROM 'primary_admitted'
 THEN RAISE EXCEPTION 'stop v2 current generation changed'; END IF;
 SELECT * INTO g FROM lab.director_owner_generations WHERE run_id=a.run_id AND generation=a.expected_generation;
 IF g.execution_sha256 IS DISTINCT FROM a.execution_sha256 OR
 jsonb_build_object('payload_sha256',(SELECT payload_sha256 FROM lab.runs WHERE run_id=a.run_id),
 'worker_pid',g.worker_pid,'worker_start_ticks',g.worker_start_ticks,'worker_boot_id',g.worker_boot_id,
 'worker_unit',g.worker_unit,'worker_invocation_id',g.worker_invocation_id,'worker_cgroup',g.worker_cgroup)
 IS DISTINCT FROM a.owner_json OR (p_owner IS NOT NULL AND p_owner IS DISTINCT FROM
 jsonb_build_object('generation',g.generation,'invocation_id',g.worker_invocation_id,'execution_sha256',g.execution_sha256))
 THEN RAISE EXCEPTION 'stop v2 original owner changed'; END IF;
 PERFORM lab.assert_attempted_stop_window(a.run_id,p_stop);
 IF p.original_primary_plan_json IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(t) ORDER BY task_id,seed)
 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=p.experiment_id) OR
 p.original_primary_plan_sha256 IS DISTINCT FROM encode(sha256(convert_to(p.original_primary_plan_text,'UTF8')),'hex') OR
 p.recovery_roster_json IS DISTINCT FROM lab.stop_v2_roster(p_stop) OR
 p.baseline_inventory_sha256 IS DISTINCT FROM lab.attempted_retained_inventory_sha256(a.run_id)
 THEN RAISE EXCEPTION 'stop v2 immutable original plan or retained inventory changed'; END IF;
END $$;
CREATE FUNCTION lab.assert_stop_v2_worker_identity(p_identity jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF jsonb_typeof(p_identity) IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_identity) key) IS DISTINCT FROM
 ARRAY['worker_boot_id','worker_cgroup','worker_invocation_id','worker_pid','worker_start_ticks','worker_unit']::text[] OR
 jsonb_typeof(p_identity->'worker_boot_id') IS DISTINCT FROM 'string' OR
 jsonb_typeof(p_identity->'worker_cgroup') IS DISTINCT FROM 'string' OR
 jsonb_typeof(p_identity->'worker_invocation_id') IS DISTINCT FROM 'string' OR
 jsonb_typeof(p_identity->'worker_unit') IS DISTINCT FROM 'string' OR
 jsonb_typeof(p_identity->'worker_pid') IS DISTINCT FROM 'number' OR
 jsonb_typeof(p_identity->'worker_start_ticks') IS DISTINCT FROM 'number' OR
 (p_identity->>'worker_pid')::integer<=1 OR (p_identity->>'worker_start_ticks')::bigint<=0 OR
 coalesce(p_identity->>'worker_boot_id','') !~ '^[0-9a-f-]{36}$' OR
 coalesce(p_identity->>'worker_invocation_id','') !~ '^[0-9a-f]{32}$' OR
 coalesce(p_identity->>'worker_unit','') !~ '^swapp-ai-scientist-scorer-[0-9a-f]{32}\.service$' OR
 p_identity->>'worker_cgroup' NOT LIKE '%/swapp-ai-scientist-scorer.slice/'||(p_identity->>'worker_unit')
 THEN RAISE EXCEPTION 'stop v2 actual Scorer identity malformed'; END IF;
END $$;
CREATE FUNCTION lab.guard_stop_v2_audit() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF TG_OP<>'INSERT' OR (TG_TABLE_NAME='attempted_stop_recovery_attempts' AND session_user<>'swapp_lab_scorer') OR
 (TG_TABLE_NAME='attempted_stop_recovery_retirements' AND session_user<>'swapp_lab_director') THEN
 RAISE EXCEPTION 'stop recovery audit is append only by its exact trusted producer'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER stop_v2_attempt_append_only BEFORE INSERT OR UPDATE OR DELETE ON lab.attempted_stop_recovery_attempts
 FOR EACH ROW EXECUTE FUNCTION lab.guard_stop_v2_audit();
CREATE TRIGGER stop_v2_retirement_append_only BEFORE INSERT OR UPDATE OR DELETE ON lab.attempted_stop_recovery_retirements
 FOR EACH ROW EXECUTE FUNCTION lab.guard_stop_v2_audit();
""")
    op.execute(r"""
CREATE FUNCTION lab.stop_v2_child_text(p_stop uuid) RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('schema','attempted-stop-children.v2',
 'original_primary_plan_sha256',p.original_primary_plan_sha256,'original_primary_plan',p.original_primary_plan_json,
 'admitted_inventory_sha256',p.admitted_inventory_sha256,
 'admitted_terminal',lab.attempted_terminal_inventory_text_v37(p_stop)::jsonb,
 'recovery_roster_json',lab.stop_v2_roster(p_stop))::text
 FROM lab.director_stopped_proposals p WHERE recovery_id=p_stop
 $$;
CREATE FUNCTION lab.stop_v2_missing_text(p_stop uuid) RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('schema','attempted-stop-missing.v2',
 'original_primary_plan_sha256',p.original_primary_plan_sha256,
 'outcomes',coalesce((SELECT jsonb_agg(to_jsonb(o) ORDER BY task_id,seed)
 FROM scorer.task_terminal_outcomes o WHERE o.run_id=a.run_id AND o.experiment_id=p.experiment_id
 AND o.outcome_code='infrastructure_unattempted'),'[]'::jsonb))::text
 FROM lab.director_stop_closures a JOIN lab.director_stopped_proposals p USING(recovery_id)
 WHERE a.recovery_id=p_stop
 $$;
CREATE FUNCTION lab.assert_stop_v2_marker_transition(p_new jsonb,p_old jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE stop uuid; base_fields text[]; first_fields text[];
BEGIN
 stop:=(p_new->>'recovery_id')::uuid;
 base_fields:=ARRAY['recovery_roster_json','child_inventory_text','child_inventory_sha256',
 'missing_primary_inventory_text','missing_primary_inventory_sha256','terminal_inventory_text','terminal_inventory_sha256'];
 IF p_old->>'stop_protocol_version'='1' THEN
 first_fields:=base_fields||ARRAY['stop_protocol_version','original_primary_plan_text','original_primary_plan_json',
 'original_primary_plan_sha256','stop_envelope_sha256'];
 IF session_user<>'swapp_lab_director' OR p_new->>'stop_protocol_version'<>'2' OR
 (p_new-first_fields) IS DISTINCT FROM (p_old-first_fields) OR
 p_old->>'terminal_inventory_text' IS NOT NULL OR
 p_new->'recovery_roster_json' IS DISTINCT FROM jsonb_build_object('attempts','[]'::jsonb,'retirements','[]'::jsonb) OR
 p_new->'original_primary_plan_json' IS DISTINCT FROM (SELECT jsonb_agg(to_jsonb(t) ORDER BY task_id,seed)
 FROM scorer.run_tasks t WHERE t.run_id=(p_new->'proposal_json'->>'run_id')::uuid
 AND t.experiment_id=p_new->>'experiment_id') OR
 p_new->>'original_primary_plan_sha256' IS DISTINCT FROM encode(sha256(convert_to(p_new->>'original_primary_plan_text','UTF8')),'hex')
 THEN RAISE EXCEPTION 'stop v2 initial plan transition invalid'; END IF;
 RETURN; END IF;
 IF p_old->>'stop_protocol_version'<>'2' OR p_new->>'stop_protocol_version'<>'2' OR
 (p_new-base_fields) IS DISTINCT FROM (p_old-base_fields) OR
 p_new->'recovery_roster_json' IS DISTINCT FROM lab.stop_v2_roster(stop)
 THEN RAISE EXCEPTION 'stop v2 marker immutable identity changed'; END IF;
 IF p_new=p_old THEN RETURN; END IF;
 IF (p_new-ARRAY['recovery_roster_json'])=(p_old-ARRAY['recovery_roster_json']) THEN
 IF session_user NOT IN ('swapp_lab_director','swapp_lab_scorer') OR
 NOT ((p_new->'recovery_roster_json'->'attempts') @> (p_old->'recovery_roster_json'->'attempts')) OR
 NOT ((p_new->'recovery_roster_json'->'retirements') @> (p_old->'recovery_roster_json'->'retirements')) THEN
 RAISE EXCEPTION 'stop v2 roster must append exact audit identities'; END IF;
 RETURN; END IF;
 IF (p_new-ARRAY['child_inventory_text','child_inventory_sha256'])=
 (p_old-ARRAY['child_inventory_text','child_inventory_sha256']) AND session_user='swapp_lab_scorer'
 AND p_old->>'child_inventory_text' IS NULL AND p_old->>'child_inventory_sha256' IS NULL
 AND p_new->>'child_inventory_text'=lab.stop_v2_child_text(stop)
 AND p_new->>'child_inventory_sha256'=encode(sha256(convert_to(p_new->>'child_inventory_text','UTF8')),'hex')
 AND EXISTS(SELECT 1 FROM lab.director_stop_closures WHERE recovery_id=stop AND state='pending')
 THEN RETURN; END IF;
 IF (p_new-ARRAY['missing_primary_inventory_text','missing_primary_inventory_sha256','terminal_inventory_text','terminal_inventory_sha256'])=
 (p_old-ARRAY['missing_primary_inventory_text','missing_primary_inventory_sha256','terminal_inventory_text','terminal_inventory_sha256'])
 AND session_user='swapp_lab_planner' AND p_old->>'missing_primary_inventory_text' IS NULL
 AND p_old->>'terminal_inventory_text' IS NULL AND p_new->>'missing_primary_inventory_text'=lab.stop_v2_missing_text(stop)
 AND p_new->>'missing_primary_inventory_sha256'=encode(sha256(convert_to(p_new->>'missing_primary_inventory_text','UTF8')),'hex')
 AND p_new->>'terminal_inventory_text'=lab.attempted_terminal_inventory_text(stop)
 AND p_new->>'terminal_inventory_sha256'=encode(sha256(convert_to(p_new->>'terminal_inventory_text','UTF8')),'hex')
 AND EXISTS(SELECT 1 FROM lab.director_stop_closures WHERE recovery_id=stop AND state='drained')
 THEN RETURN; END IF;
 RAISE EXCEPTION 'stop v2 marker transition outside exact lifecycle';
END $$;
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('lab.guard_attempted_stop_marker()'::regprocedure) INTO definition;
 IF position('BEGIN' in definition)=0 THEN RAISE EXCEPTION 'stop marker trigger shape changed'; END IF;
 definition:=overlay(definition placing $branch$BEGIN
 IF TG_OP='UPDATE' AND (NEW.stop_protocol_version=2 OR OLD.stop_protocol_version=2) THEN
 PERFORM lab.assert_stop_v2_marker_transition(to_jsonb(NEW),to_jsonb(OLD)); RETURN NEW; END IF;
 $branch$ from position('BEGIN' in definition) for 5);
 EXECUTE definition;
END $$;

CREATE OR REPLACE FUNCTION lab.begin_stopped_proposal_closure(p_stop uuid,p_proposal text)
 RETURNS double precision LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE envelope jsonb; p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
 attempt lab.attempted_stop_recovery_attempts%ROWTYPE; identity jsonb; observation jsonb; properties jsonb;
 original_run uuid; current_owner jsonb; seconds double precision; plan text; v1 jsonb;
BEGIN
 IF session_user<>'swapp_lab_director' OR p_proposal IS NULL OR octet_length(p_proposal)>6291456
 THEN RAISE EXCEPTION 'Director bounded stop v2 identity required'; END IF;
 envelope:=p_proposal::jsonb;
 IF coalesce(envelope->>'schema','') NOT IN ('attempted-proposal-stop.v2','attempted-stop-retirement.v2') THEN
 RETURN lab.begin_proposal_stop_v37(p_stop,p_proposal); END IF;
 IF jsonb_typeof(envelope->'expected_owner') IS DISTINCT FROM 'object' THEN
 RAISE EXCEPTION 'stop v2 captured expected owner required'; END IF;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF envelope->>'schema'='attempted-stop-retirement.v2' THEN
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(envelope) key) IS DISTINCT FROM
 ARRAY['action','expected_owner','observation_text','schema','worker_identity']::text[] OR envelope->>'action' IS DISTINCT FROM 'retire'
 THEN RAISE EXCEPTION 'stop retirement envelope malformed'; END IF;
 PERFORM lab.assert_stop_v2_context(p_stop,envelope->'expected_owner');
 identity:=envelope->'worker_identity'; PERFORM lab.assert_stop_v2_worker_identity(identity);
 SELECT * INTO attempt FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop
 AND recovery_invocation_id=identity->>'worker_invocation_id';
 IF attempt.worker_identity IS DISTINCT FROM identity OR attempt.recovery_id IS NULL THEN
 RAISE EXCEPTION 'retirement does not bind exact registered worker'; END IF;
 observation:=(envelope->>'observation_text')::jsonb; properties:=observation->'unit_properties';
 IF jsonb_typeof(envelope->'observation_text') IS DISTINCT FROM 'string' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(observation) key) IS DISTINCT FROM
 ARRAY['cgroup_empty','observed_at','observed_boot_id','process_retired','schema','unit_properties','worker_identity']::text[] OR
 jsonb_typeof(observation->'observed_at') IS DISTINCT FROM 'string' OR
 jsonb_typeof(observation->'observed_boot_id') IS DISTINCT FROM 'string' OR
 jsonb_typeof(properties) IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(properties) key) IS DISTINCT FROM
 ARRAY['ActiveState','ControlGroup','InvocationID','LoadState','MainPID']::text[] OR
 EXISTS(SELECT 1 FROM jsonb_each(properties) item WHERE jsonb_typeof(item.value)<>'string') OR
 octet_length(envelope->>'observation_text')>32768 OR
 observation->>'schema' IS DISTINCT FROM 'attempted-stop-retirement-observation.v2' OR
 observation->'worker_identity' IS DISTINCT FROM identity OR
 observation->'process_retired' IS DISTINCT FROM 'true'::jsonb OR observation->'cgroup_empty' IS DISTINCT FROM 'true'::jsonb OR
 coalesce(observation->>'observed_boot_id','') !~ '^[0-9a-f-]{36}$' OR
 (observation->>'observed_at')::timestamptz>clock_timestamp()+interval '5 seconds' OR
 (observation->>'observed_at')::timestamptz<attempt.registered_at-interval '5 seconds' OR
 properties->>'MainPID' IS DISTINCT FROM '0' OR properties->>'ControlGroup' NOT IN ('',identity->>'worker_cgroup') OR
 NOT (properties->>'LoadState'='not-found' AND properties->>'ActiveState'='inactive' AND properties->>'InvocationID'='' OR
 properties->>'LoadState'='loaded' AND properties->>'ActiveState' IN ('inactive','failed')
 AND properties->>'InvocationID'=identity->>'worker_invocation_id') THEN
 RAISE EXCEPTION 'registered recovery worker retirement proof malformed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM lab.attempted_stop_recovery_retirements WHERE recovery_id=p_stop AND retired_ordinal=attempt.attempt_ordinal) THEN
 INSERT INTO lab.attempted_stop_recovery_retirements(recovery_id,retired_ordinal,identity_sha256,observation_text,observation_sha256)
 VALUES(p_stop,attempt.attempt_ordinal,encode(sha256(convert_to(identity::text,'UTF8')),'hex'),
 envelope->>'observation_text',encode(sha256(convert_to(envelope->>'observation_text','UTF8')),'hex'));
 UPDATE lab.director_stopped_proposals SET recovery_roster_json=lab.stop_v2_roster(p_stop) WHERE recovery_id=p_stop;
 END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 RETURN lab.assert_attempted_stop_window(a.run_id,p_stop);
 END IF;
 IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(envelope) key) IS DISTINCT FROM
 ARRAY['admitted_inventory_text','expected_owner','original_primary_plan_text','proposal_text','schema']::text[] THEN
 RAISE EXCEPTION 'original plan stop envelope malformed'; END IF;
 IF jsonb_typeof(envelope->'proposal_text') IS DISTINCT FROM 'string' OR
 jsonb_typeof(envelope->'admitted_inventory_text') IS DISTINCT FROM 'string' OR
 jsonb_typeof(envelope->'original_primary_plan_text') IS DISTINCT FROM 'string' THEN
 RAISE EXCEPTION 'stop v2 raw canonical text fields required'; END IF;
 plan:=envelope->>'original_primary_plan_text';
 IF plan IS NULL OR octet_length(plan)>2097152 OR jsonb_typeof(plan::jsonb) IS DISTINCT FROM 'array'
 OR jsonb_array_length(plan::jsonb) NOT BETWEEN 1 AND 1024 THEN RAISE EXCEPTION 'original primary plan invalid'; END IF;
 IF p.recovery_id IS NOT NULL THEN
 PERFORM lab.assert_stop_v2_context(p_stop,envelope->'expected_owner');
 IF p.original_primary_plan_text IS DISTINCT FROM plan OR p.admitted_inventory_text IS DISTINCT FROM envelope->>'admitted_inventory_text'
 OR p.proposal_json IS DISTINCT FROM (envelope->>'proposal_text')::jsonb OR
 p.proposal_sha256 IS DISTINCT FROM encode(sha256(convert_to(envelope->>'proposal_text','UTF8')),'hex') OR
 p.stop_envelope_sha256 IS DISTINCT FROM encode(sha256(convert_to(p_proposal,'UTF8')),'hex') THEN
 RAISE EXCEPTION 'original plan stop replay changed immutable envelope'; END IF;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 RETURN lab.assert_attempted_stop_window(a.run_id,p_stop); END IF;
 SELECT run_id INTO original_run FROM lab.director_recoveries WHERE recovery_id=p_stop;
 PERFORM lab.lock_run_plan(original_run);
 PERFORM 1 FROM lab.runs WHERE run_id=original_run AND state='stop_requested' AND stop_requested FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'original plan stop run is not requested'; END IF;
 SELECT jsonb_build_object('generation',g.generation,'invocation_id',g.worker_invocation_id,'execution_sha256',g.execution_sha256)
 INTO current_owner FROM lab.director_execution_control c JOIN lab.director_owner_generations g
 ON g.run_id=c.run_id AND g.generation=c.current_generation WHERE c.run_id=original_run AND c.mode='active';
 IF current_owner IS DISTINCT FROM envelope->'expected_owner' OR plan::jsonb IS DISTINCT FROM
 (SELECT jsonb_agg(to_jsonb(t) ORDER BY task_id,seed) FROM scorer.run_tasks t
 WHERE t.run_id=original_run AND t.experiment_id=(envelope->>'proposal_text')::jsonb->>'experiment_id') THEN
 RAISE EXCEPTION 'original primary plan or expected owner changed before admission'; END IF;
 v1:=jsonb_set(envelope-'original_primary_plan_text','{schema}','"attempted-proposal-stop.v1"'::jsonb);
 seconds:=lab.begin_proposal_stop_v37(p_stop,v1::text);
 UPDATE lab.director_stopped_proposals SET stop_protocol_version=2,original_primary_plan_text=plan,
 original_primary_plan_json=plan::jsonb,original_primary_plan_sha256=encode(sha256(convert_to(plan,'UTF8')),'hex'),
 recovery_roster_json=lab.stop_v2_roster(p_stop),stop_envelope_sha256=encode(sha256(convert_to(p_proposal,'UTF8')),'hex') WHERE recovery_id=p_stop;
 RETURN seconds;
END $$;
""")
    op.execute(r"""
CREATE FUNCTION lab.stop_v2_admitted_text(p_stop uuid) RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('jobs',(SELECT jsonb_agg(to_jsonb(j) ORDER BY job_id)
 FROM scorer.score_jobs j WHERE j.run_id=a.run_id AND j.experiment_id=p.experiment_id),
 'drains',(SELECT jsonb_agg(to_jsonb(d) ORDER BY job_id) FROM lab.director_stop_job_drains d WHERE recovery_id=p_stop),
 'outcomes',(SELECT jsonb_agg(to_jsonb(o) ORDER BY score_job_id) FROM scorer.task_terminal_outcomes o
 WHERE o.run_id=a.run_id AND o.experiment_id=p.experiment_id AND o.score_job_id IS NOT NULL),
 'completions_count',(SELECT count(*) FROM scorer.task_completions c JOIN scorer.score_jobs j
 USING(run_id,experiment_id,evaluation_kind,task_id,seed) WHERE j.run_id=a.run_id AND j.experiment_id=p.experiment_id),
 'completions_sha256',(SELECT encode(sha256(convert_to(coalesce(jsonb_agg(to_jsonb(c) ORDER BY c.task_id,c.seed),'[]'::jsonb)::text,'UTF8')),'hex')
 FROM scorer.task_completions c JOIN scorer.score_jobs j USING(run_id,experiment_id,evaluation_kind,task_id,seed)
 WHERE j.run_id=a.run_id AND j.experiment_id=p.experiment_id))::text
 FROM lab.director_stop_closures a JOIN lab.director_stopped_proposals p USING(recovery_id) WHERE a.recovery_id=p_stop
 $$;
CREATE OR REPLACE FUNCTION lab.stop_v2_child_text(p_stop uuid) RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT jsonb_build_object('schema','attempted-stop-children.v2',
 'original_primary_plan_sha256',p.original_primary_plan_sha256,'original_primary_plan',p.original_primary_plan_json,'admitted_inventory_sha256',p.admitted_inventory_sha256,
 'admitted_terminal',lab.stop_v2_admitted_text(p_stop)::jsonb,'recovery_roster_json',lab.stop_v2_roster(p_stop))::text
 FROM lab.director_stopped_proposals p WHERE recovery_id=p_stop
 $$;
CREATE FUNCTION lab.assert_stop_v2_ancestry(p_stop uuid,p_current text DEFAULT NULL) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE latest lab.attempted_stop_recovery_attempts%ROWTYPE;
BEGIN
 SELECT * INTO latest FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop ORDER BY attempt_ordinal DESC LIMIT 1;
 IF latest.recovery_id IS NULL OR (p_current IS NOT NULL AND latest.recovery_invocation_id IS DISTINCT FROM p_current) OR
 EXISTS(SELECT 1 FROM lab.attempted_stop_recovery_attempts w WHERE recovery_id=p_stop
 AND (p_current IS NULL OR w.attempt_ordinal<latest.attempt_ordinal) AND NOT EXISTS(
 SELECT 1 FROM lab.attempted_stop_recovery_retirements r WHERE r.recovery_id=p_stop AND r.retired_ordinal=w.attempt_ordinal
 AND r.identity_sha256=encode(sha256(convert_to(w.worker_identity::text,'UTF8')),'hex')
 AND r.observation_sha256=encode(sha256(convert_to(r.observation_text,'UTF8')),'hex')
 AND r.observation_text::jsonb->'worker_identity'=w.worker_identity
 AND r.observation_text::jsonb->>'schema'='attempted-stop-retirement-observation.v2'
 AND r.observation_text::jsonb->'process_retired'='true'::jsonb
 AND r.observation_text::jsonb->'cgroup_empty'='true'::jsonb)) OR
 EXISTS(SELECT 1 FROM lab.director_stop_job_drains d WHERE d.recovery_id=p_stop AND NOT EXISTS(
 SELECT 1 FROM lab.attempted_stop_recovery_attempts w WHERE w.recovery_id=p_stop AND w.recovery_invocation_id=d.recovery_invocation))
 THEN RAISE EXCEPTION 'stop v2 recovery ancestry incomplete or foreign'; END IF;
END $$;
CREATE OR REPLACE FUNCTION lab.stopped_proposal_job_receipt(p_stop uuid,p_job uuid) RETURNS jsonb
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
BEGIN
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_protocol_version IS DISTINCT FROM 2 THEN RETURN lab.proposal_stop_job_receipt_v37(p_stop,p_job); END IF;
 IF session_user<>'swapp_lab_scorer' THEN RAISE EXCEPTION 'Scorer stop context identity required'; END IF;
 PERFORM lab.assert_stop_v2_context(p_stop);
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF p_job IS NOT NULL THEN RETURN lab.proposal_stop_job_receipt_v37(p_stop,p_job); END IF;
 RETURN jsonb_build_object('schema','attempted-stop-context.v2','run_id',a.run_id,'experiment_id',p.experiment_id,
 'stop_protocol_version',2,'expected_owner',jsonb_build_object('generation',a.expected_generation,
 'invocation_id',a.owner_json->>'worker_invocation_id','execution_sha256',a.execution_sha256),
 'original_primary_plan_sha256',p.original_primary_plan_sha256,'original_primary_plan',p.original_primary_plan_json,'admitted_inventory_sha256',p.admitted_inventory_sha256,
 'child_inventory_sha256',p.child_inventory_sha256,'terminal_inventory_sha256',p.terminal_inventory_sha256,
 'closure_state',a.state,'recovery_roster_json',p.recovery_roster_json);
END $$;
CREATE OR REPLACE FUNCTION lab.reconcile_stopped_score_job(p_stop uuid,p_job uuid,p_expected text,p_recovery text)
 RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
BEGIN
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_protocol_version=2 THEN
 IF session_user<>'swapp_lab_scorer' OR p_recovery IS NULL OR p_recovery !~ '^[0-9a-f]{32}$' THEN
 RAISE EXCEPTION 'stop v2 registered Scorer required'; END IF;
 PERFORM lab.assert_stop_v2_context(p_stop);
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF a.state<>'pending' OR p.child_inventory_text IS NOT NULL THEN RAISE EXCEPTION 'stop v2 child proof already sealed'; END IF;
 PERFORM lab.assert_stop_v2_ancestry(p_stop,p_recovery);
 END IF;
 RETURN lab.reconcile_stop_job_v37(p_stop,p_job,p_expected,p_recovery);
END $$;
CREATE OR REPLACE FUNCTION lab.finish_stopped_proposal_children(p_stop uuid,p_recovery text) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
 envelope jsonb; identity jsonb; latest lab.attempted_stop_recovery_attempts%ROWTYPE; child text;
BEGIN
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_protocol_version IS DISTINCT FROM 2 THEN RETURN lab.finish_proposal_stop_children_v37(p_stop,p_recovery); END IF;
 IF session_user<>'swapp_lab_scorer' OR p_recovery IS NULL OR octet_length(p_recovery)>16384
 THEN RAISE EXCEPTION 'stop v2 actual worker envelope required'; END IF;
 envelope:=p_recovery::jsonb;
 IF envelope->>'schema' IS DISTINCT FROM 'attempted-stop-worker.v2' OR
 jsonb_typeof(envelope->'expected_owner') IS DISTINCT FROM 'object' OR
 (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(envelope) key) IS DISTINCT FROM
 ARRAY['action','expected_owner','schema','worker_identity']::text[] OR
 coalesce(envelope->>'action','') NOT IN ('register','children_drained') THEN RAISE EXCEPTION 'stop v2 worker action malformed'; END IF;
 PERFORM lab.assert_stop_v2_context(p_stop,envelope->'expected_owner');
 identity:=envelope->'worker_identity'; PERFORM lab.assert_stop_v2_worker_identity(identity);
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 SELECT * INTO latest FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop ORDER BY attempt_ordinal DESC LIMIT 1;
 IF a.state<>'pending' THEN RAISE EXCEPTION 'stop v2 final proof requires reuse not new worker'; END IF;
 IF envelope->>'action'='register' THEN
 IF p.child_inventory_text IS NOT NULL THEN RAISE EXCEPTION 'stop v2 child proof requires reuse'; END IF;
 IF latest.worker_identity=identity THEN RETURN 'registered'; END IF;
 IF EXISTS(SELECT 1 FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop
 AND recovery_invocation_id=identity->>'worker_invocation_id') OR
 (latest.recovery_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM lab.attempted_stop_recovery_retirements
 WHERE recovery_id=p_stop AND retired_ordinal=latest.attempt_ordinal
 AND identity_sha256=encode(sha256(convert_to(latest.worker_identity::text,'UTF8')),'hex'))) THEN
 RAISE EXCEPTION 'stop v2 predecessor not retired or invocation reused'; END IF;
 INSERT INTO lab.attempted_stop_recovery_attempts(recovery_id,attempt_ordinal,worker_identity,recovery_invocation_id,
 expected_generation,execution_sha256,original_primary_plan_sha256)
 VALUES(p_stop,coalesce(latest.attempt_ordinal,0)+1,identity,identity->>'worker_invocation_id',
 a.expected_generation,a.execution_sha256,p.original_primary_plan_sha256);
 UPDATE lab.director_stopped_proposals SET recovery_roster_json=lab.stop_v2_roster(p_stop) WHERE recovery_id=p_stop;
 RETURN 'registered'; END IF;
 IF latest.worker_identity IS DISTINCT FROM identity THEN RAISE EXCEPTION 'stop v2 finisher is not current registered actor'; END IF;
 PERFORM lab.assert_stop_v2_ancestry(p_stop,identity->>'worker_invocation_id');
 PERFORM lab.assert_attempted_terminal_inventory_v37(p_stop);
 IF EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=a.run_id AND state IN ('queued','running')) THEN
 RAISE EXCEPTION 'stop v2 admitted jobs remain active'; END IF;
 IF p.child_inventory_text IS NOT NULL THEN
 IF p.child_inventory_text::jsonb->'admitted_terminal' IS DISTINCT FROM lab.stop_v2_admitted_text(p_stop)::jsonb
 OR p.child_inventory_sha256 IS DISTINCT FROM encode(sha256(convert_to(p.child_inventory_text,'UTF8')),'hex') THEN
 RAISE EXCEPTION 'stop v2 persisted child receipt changed'; END IF;
 RETURN 'children_drained'; END IF;
 child:=lab.stop_v2_child_text(p_stop);
 UPDATE lab.director_stopped_proposals SET child_inventory_text=child,
 child_inventory_sha256=encode(sha256(convert_to(child,'UTF8')),'hex') WHERE recovery_id=p_stop;
 RETURN 'children_drained';
END $$;
""")
    op.execute(r"""
CREATE FUNCTION lab.assert_stop_v2_coverage(p_stop uuid) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
DECLARE p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
BEGIN
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 PERFORM lab.assert_attempted_terminal_inventory_v37(p_stop);
 PERFORM lab.assert_stop_v2_ancestry(p_stop);
 IF p.child_inventory_text IS NULL OR p.child_inventory_sha256 IS DISTINCT FROM
 encode(sha256(convert_to(p.child_inventory_text,'UTF8')),'hex') OR
 p.child_inventory_text::jsonb->>'original_primary_plan_sha256' IS DISTINCT FROM p.original_primary_plan_sha256 OR
 p.child_inventory_text::jsonb->>'admitted_inventory_sha256' IS DISTINCT FROM p.admitted_inventory_sha256 OR
 p.child_inventory_text::jsonb->'admitted_terminal' IS DISTINCT FROM lab.stop_v2_admitted_text(p_stop)::jsonb OR
 NOT ((lab.stop_v2_roster(p_stop)->'attempts') @> (p.child_inventory_text::jsonb->'recovery_roster_json'->'attempts')) OR
 (SELECT count(*) FROM scorer.task_completions WHERE run_id=a.run_id AND experiment_id=p.experiment_id)
 IS DISTINCT FROM jsonb_array_length(p.original_primary_plan_json) OR
 EXISTS(SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=p.experiment_id AND
 NOT EXISTS(SELECT 1 FROM scorer.task_completions c WHERE
 (c.run_id,c.experiment_id,c.evaluation_kind,c.task_id,c.seed)=
 (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed))) OR
 EXISTS(SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=p.experiment_id
 AND NOT EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)=
 (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed)) AND
 (EXISTS(SELECT 1 FROM scorer.task_scores s WHERE
 (s.run_id,s.experiment_id,s.evaluation_kind,s.task_id,s.seed)=
 (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed)) OR NOT EXISTS(
 SELECT 1 FROM scorer.task_terminal_outcomes o JOIN scorer.task_completions c
 USING(run_id,experiment_id,evaluation_kind,task_id,seed)
 WHERE (o.run_id,o.experiment_id,o.evaluation_kind,o.task_id,o.seed)=
 (t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed)
 AND o.candidate_sha256=t.candidate_sha256 AND o.outcome_code='infrastructure_unattempted'
 AND o.producer_role='swapp_lab_planner' AND o.score_job_id IS NULL AND o.claim_token IS NULL
 AND o.worker_invocation_id IS NULL AND o.recovery_invocation_id IS NULL
 AND o.admitted_generation=a.expected_generation AND o.execution_sha256=a.execution_sha256
 AND c.completion_kind='terminal'))) THEN
 RAISE EXCEPTION 'stop v2 original primary plan coverage incomplete or changed'; END IF;
END $$;
CREATE OR REPLACE FUNCTION lab.assert_attempted_terminal_inventory(p_stop uuid) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=p_stop AND stop_protocol_version=2) THEN
 PERFORM lab.assert_stop_v2_context(p_stop); PERFORM lab.assert_stop_v2_coverage(p_stop); RETURN; END IF;
 PERFORM lab.assert_attempted_terminal_inventory_v37(p_stop);
END $$;
CREATE OR REPLACE FUNCTION lab.attempted_terminal_inventory_text(p_stop uuid) RETURNS text LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
 SELECT CASE WHEN p.stop_protocol_version=2 THEN jsonb_build_object('schema','attempted-stop-terminal.v2',
 'original_primary_plan_sha256',p.original_primary_plan_sha256,'original_primary_plan',p.original_primary_plan_json,'child_inventory_sha256',p.child_inventory_sha256,
 'missing_primary_inventory',lab.stop_v2_missing_text(p_stop)::jsonb,
 'terminal_inventory',lab.attempted_terminal_inventory_text_v37(p_stop)::jsonb,
 'recovery_roster_json',lab.stop_v2_roster(p_stop))::text
 ELSE lab.attempted_terminal_inventory_text_v37(p_stop) END
 FROM lab.director_stopped_proposals p WHERE recovery_id=p_stop
 $$;
DO $$ DECLARE definition text; BEGIN
 SELECT pg_get_functiondef('lab.assert_stopped_missing_transition(text,text,jsonb)'::regprocedure) INTO definition;
 IF position('FUNCTION lab.assert_stopped_missing_transition(' in definition)=0 THEN RAISE EXCEPTION 'missing guard shape changed'; END IF;
 EXECUTE replace(definition,'FUNCTION lab.assert_stopped_missing_transition(', 'FUNCTION lab.assert_stopped_missing_v37(');
END $$;
CREATE FUNCTION lab.assert_stop_v2_missing_transition(p_table text,p_operation text,p_new jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE stop uuid; experiment text; a lab.director_stop_closures%ROWTYPE; p lab.director_stopped_proposals%ROWTYPE;
BEGIN
 IF session_user<>'swapp_lab_planner' OR p_table NOT IN ('task_terminal_outcomes','task_completions') OR p_operation<>'INSERT'
 THEN RAISE EXCEPTION 'Planner stop v2 missing INSERT only'; END IF;
 stop:=nullif(current_setting('lab.stop_request',true),'')::uuid; experiment:=current_setting('lab.stop_missing',true);
 PERFORM lab.assert_stop_v2_context(stop); PERFORM lab.assert_stop_v2_ancestry(stop);
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=stop;
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=stop;
 IF a.state<>'pending' OR p.experiment_id IS DISTINCT FROM experiment OR p.child_inventory_text IS NULL
 OR p.missing_primary_inventory_text IS NOT NULL OR p.terminal_inventory_text IS NOT NULL THEN
 RAISE EXCEPTION 'stop v2 missing cells have no exact unsealed child authority'; END IF;
 IF p_new IS NULL THEN RETURN; END IF;
 IF p_new->>'run_id' IS DISTINCT FROM a.run_id::text OR p_new->>'experiment_id' IS DISTINCT FROM experiment OR
 p_new->>'evaluation_kind' IS DISTINCT FROM 'primary' OR p_new->>'seed' IS DISTINCT FROM '0' OR
 NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.original_primary_plan_json) cell
 WHERE cell->>'task_id'=p_new->>'task_id' AND cell->>'seed'='0' AND cell->>'evaluation_kind'='primary') OR
 EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=a.run_id AND j.experiment_id=experiment
 AND j.evaluation_kind='primary' AND j.task_id=p_new->>'task_id' AND j.seed=0) OR
 EXISTS(SELECT 1 FROM scorer.task_scores s WHERE s.run_id=a.run_id AND s.experiment_id=experiment
 AND s.evaluation_kind='primary' AND s.task_id=p_new->>'task_id' AND s.seed=0) OR
 EXISTS(SELECT 1 FROM scorer.task_completions c WHERE c.run_id=a.run_id AND c.experiment_id=experiment
 AND c.evaluation_kind='primary' AND c.task_id=p_new->>'task_id' AND c.seed=0) THEN
 RAISE EXCEPTION 'stop v2 missing cell is not an unresolved original planned task'; END IF;
 IF p_table='task_terminal_outcomes' AND (p_new->>'outcome_code' IS DISTINCT FROM 'infrastructure_unattempted' OR
 p_new->>'producer_role' IS DISTINCT FROM 'swapp_lab_planner' OR p_new->>'score_job_id' IS NOT NULL OR
 p_new->>'claim_token' IS NOT NULL OR p_new->>'worker_invocation_id' IS NOT NULL OR p_new->>'recovery_invocation_id' IS NOT NULL OR
 p_new->>'admitted_generation' IS DISTINCT FROM a.expected_generation::text OR p_new->>'execution_sha256' IS DISTINCT FROM a.execution_sha256 OR
 NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.original_primary_plan_json) cell
 WHERE cell->>'task_id'=p_new->>'task_id' AND cell->>'candidate_sha256'=p_new->>'candidate_sha256') OR
 EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes o WHERE o.run_id=a.run_id AND o.experiment_id=experiment
 AND o.evaluation_kind='primary' AND o.task_id=p_new->>'task_id' AND o.seed=0)) THEN
 RAISE EXCEPTION 'stop v2 missing outcome malformed or duplicate'; END IF;
 IF p_table='task_completions' AND (p_new->>'completion_kind' IS DISTINCT FROM 'terminal' OR
 NOT EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes o WHERE o.run_id=a.run_id AND o.experiment_id=experiment
 AND o.evaluation_kind='primary' AND o.task_id=p_new->>'task_id' AND o.seed=0
 AND o.outcome_code='infrastructure_unattempted' AND o.producer_role='swapp_lab_planner'
 AND o.admitted_generation=a.expected_generation AND o.execution_sha256=a.execution_sha256)) THEN
 RAISE EXCEPTION 'stop v2 missing completion has no exact real outcome'; END IF;
END $$;
CREATE OR REPLACE FUNCTION lab.assert_stopped_missing_transition(p_table text,p_operation text,p_new jsonb) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM lab.director_stopped_proposals WHERE recovery_id=nullif(current_setting('lab.stop_request',true),'')::uuid
 AND stop_protocol_version=2) THEN PERFORM lab.assert_stop_v2_missing_transition(p_table,p_operation,p_new); RETURN; END IF;
 PERFORM lab.assert_stopped_missing_v37(p_table,p_operation,p_new);
END $$;
CREATE OR REPLACE FUNCTION lab.close_stopped_unattempted_tasks(p_stop uuid,p_experiment text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE p lab.director_stopped_proposals%ROWTYPE; a lab.director_stop_closures%ROWTYPE;
 latest lab.attempted_stop_recovery_attempts%ROWTYPE; cell scorer.run_tasks%ROWTYPE; missing text; terminal_text text;
BEGIN
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 IF p.stop_protocol_version IS DISTINCT FROM 2 THEN RETURN lab.close_stopped_missing_v37(p_stop,p_experiment); END IF;
 IF session_user<>'swapp_lab_planner' OR p_experiment IS DISTINCT FROM p.experiment_id THEN
 RAISE EXCEPTION 'Planner original primary missing identity required'; END IF;
 PERFORM lab.assert_stop_v2_context(p_stop); PERFORM lab.assert_stop_v2_ancestry(p_stop);
 SELECT * INTO p FROM lab.director_stopped_proposals WHERE recovery_id=p_stop;
 SELECT * INTO a FROM lab.director_stop_closures WHERE recovery_id=p_stop;
 IF a.state='drained' THEN
 PERFORM lab.assert_stop_v2_coverage(p_stop);
 IF p.missing_primary_inventory_text IS DISTINCT FROM lab.stop_v2_missing_text(p_stop) OR
 p.missing_primary_inventory_sha256 IS DISTINCT FROM encode(sha256(convert_to(p.missing_primary_inventory_text,'UTF8')),'hex') OR
 p.terminal_inventory_text IS DISTINCT FROM lab.attempted_terminal_inventory_text(p_stop) OR
 p.terminal_inventory_sha256 IS DISTINCT FROM encode(sha256(convert_to(p.terminal_inventory_text,'UTF8')),'hex') THEN
 RAISE EXCEPTION 'stop v2 final seal replay changed'; END IF;
 RETURN true; END IF;
 IF a.state<>'pending' OR p.child_inventory_text IS NULL OR p.terminal_inventory_text IS NOT NULL OR
 p.child_inventory_text::jsonb->'admitted_terminal' IS DISTINCT FROM lab.stop_v2_admitted_text(p_stop)::jsonb OR
 p.child_inventory_sha256 IS DISTINCT FROM encode(sha256(convert_to(p.child_inventory_text,'UTF8')),'hex') THEN
 RAISE EXCEPTION 'stop v2 final seal has no immutable drained child proof'; END IF;
 PERFORM lab.assert_attempted_terminal_inventory_v37(p_stop);
 PERFORM set_config('lab.stop_request',p_stop::text,true); PERFORM set_config('lab.stop_missing',p_experiment,true);
 FOR cell IN SELECT t.* FROM scorer.run_tasks t WHERE t.run_id=a.run_id AND t.experiment_id=p_experiment AND
 NOT EXISTS(SELECT 1 FROM scorer.score_jobs j WHERE
 (j.run_id,j.experiment_id,j.evaluation_kind,j.task_id,j.seed)=(t.run_id,t.experiment_id,t.evaluation_kind,t.task_id,t.seed))
 ORDER BY task_id,seed LOOP
 INSERT INTO scorer.task_terminal_outcomes(run_id,experiment_id,evaluation_kind,task_id,seed,
 candidate_sha256,outcome_code,producer_role,admitted_generation,execution_sha256)
 VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,cell.candidate_sha256,
 'infrastructure_unattempted','swapp_lab_planner',a.expected_generation,a.execution_sha256);
 INSERT INTO scorer.task_completions(run_id,experiment_id,evaluation_kind,task_id,seed,completion_kind)
 VALUES(cell.run_id,cell.experiment_id,cell.evaluation_kind,cell.task_id,cell.seed,'terminal');
 END LOOP;
 PERFORM lab.assert_stop_v2_coverage(p_stop);
 missing:=lab.stop_v2_missing_text(p_stop); terminal_text:=lab.attempted_terminal_inventory_text(p_stop);
 SELECT * INTO latest FROM lab.attempted_stop_recovery_attempts WHERE recovery_id=p_stop ORDER BY attempt_ordinal DESC LIMIT 1;
 UPDATE lab.director_stop_closures SET state='drained',recovery_invocation=latest.recovery_invocation_id WHERE recovery_id=p_stop;
 UPDATE lab.director_stopped_proposals SET missing_primary_inventory_text=missing,
 missing_primary_inventory_sha256=encode(sha256(convert_to(missing,'UTF8')),'hex'),terminal_inventory_text=terminal_text,
 terminal_inventory_sha256=encode(sha256(convert_to(terminal_text,'UTF8')),'hex') WHERE recovery_id=p_stop;
 RETURN true;
END $$;
""")
    op.execute(r"""
REVOKE ALL ON FUNCTION lab.begin_proposal_stop_v37(uuid,text),lab.proposal_stop_job_receipt_v37(uuid,uuid),
 lab.finish_proposal_stop_children_v37(uuid,text),lab.reconcile_stop_job_v37(uuid,uuid,text,text),
 lab.close_stopped_missing_v37(uuid,text),lab.assert_attempted_terminal_inventory_v37(uuid),
 lab.attempted_terminal_inventory_text_v37(uuid),lab.assert_stopped_missing_v37(text,text,jsonb),
 lab.stop_v2_roster(uuid),lab.assert_stop_v2_context(uuid,jsonb),lab.assert_stop_v2_worker_identity(jsonb),
 lab.guard_stop_v2_audit(),lab.stop_v2_child_text(uuid),lab.stop_v2_missing_text(uuid),
 lab.assert_stop_v2_marker_transition(jsonb,jsonb),lab.stop_v2_admitted_text(uuid),
 lab.assert_stop_v2_ancestry(uuid,text),lab.assert_stop_v2_coverage(uuid),
 lab.assert_stop_v2_missing_transition(text,text,jsonb)
 FROM PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
""")


def downgrade() -> None:
    """Refuse to discard immutable original plan or genuine recovery audit."""
    raise RuntimeError("attempted stop plan/recovery audits cannot be downgraded")
