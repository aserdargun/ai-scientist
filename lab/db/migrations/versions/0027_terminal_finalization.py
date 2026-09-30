"""Bound terminal report closure to durable same-owner receipts and a fixed CPU window."""

from alembic import op

revision = "0027_terminal_finalization"
down_revision = "0026_holdout_execution_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
CREATE TABLE lab.terminal_finalizations (
 run_id uuid PRIMARY KEY REFERENCES lab.runs(run_id),
 generation integer NOT NULL CHECK(generation > 0),
 invocation_id text NOT NULL CHECK(invocation_id ~ '^[0-9a-f]{32}$'),
 execution_sha256 text NOT NULL CHECK(execution_sha256 ~ '^[0-9a-f]{64}$'),
 payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[0-9a-f]{64}$'),
 source_kind text NOT NULL CHECK(source_kind IN ('holdout','baseline')),
 source_sha256 text NOT NULL CHECK(source_sha256 ~ '^[0-9a-f]{64}$'),
 task_plan_sha256 text NOT NULL CHECK(task_plan_sha256 ~ '^[0-9a-f]{64}$'),
 task_plan_count integer NOT NULL CHECK(task_plan_count > 0),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 deadline_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '60 seconds')
);
REVOKE ALL ON lab.terminal_finalizations FROM
    PUBLIC,swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT SELECT ON lab.terminal_finalizations TO swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;

CREATE FUNCTION lab.verify_terminal_finalization(p_run uuid,p_generation integer,p_execution text)
RETURNS lab.terminal_finalizations LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,lab,scorer AS $$
DECLARE f lab.terminal_finalizations%ROWTYPE; r lab.runs%ROWTYPE;
BEGIN
 IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner','swapp_lab_scorer') OR
    p_run IS NULL OR p_generation IS NULL OR p_execution IS NULL THEN
  RAISE EXCEPTION 'terminal finalization identity is malformed';
 END IF;
 PERFORM pg_advisory_xact_lock(
    ('x'||substr(encode(sha256(uuid_send(p_run)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=p_run FOR UPDATE;
 SELECT * INTO f FROM lab.terminal_finalizations WHERE run_id=p_run;
 IF f.run_id IS NULL OR r.state IS DISTINCT FROM 'running' OR r.stop_requested OR
    f.generation IS DISTINCT FROM p_generation OR f.execution_sha256 IS DISTINCT FROM p_execution OR
    f.payload_sha256 IS DISTINCT FROM r.payload_sha256 OR f.deadline_at <= clock_timestamp() OR
    NOT EXISTS (SELECT 1 FROM lab.director_execution_control c
      JOIN lab.director_execution_contracts x USING(run_id)
      JOIN lab.director_owner_generations o ON o.run_id=c.run_id AND
    o.generation=c.current_generation
      WHERE c.run_id=p_run AND c.mode='active' AND c.current_generation=f.generation
       AND x.execution_sha256=f.execution_sha256 AND x.payload_sha256=f.payload_sha256
       AND o.execution_sha256=f.execution_sha256 AND o.worker_invocation_id=f.invocation_id) OR
    EXISTS (SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run AND state IN ('queued','running')) OR
    EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run AND state IN
    ('reserved','running')) OR
    EXISTS (SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=p_run AND NOT EXISTS (
      SELECT 1 FROM scorer.task_completions c WHERE c.run_id=t.run_id
       AND c.experiment_id=t.experiment_id AND c.evaluation_kind=t.evaluation_kind
       AND c.task_id=t.task_id AND c.seed=t.seed)) OR
    EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run
      AND NOT EXISTS (SELECT 1 FROM lab.experiment_records d
        WHERE d.experiment_id=e.experiment_id)) OR
    EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run AND
      (e.status NOT IN ('scored','crashed','abandoned','rejected') OR NOT EXISTS (
       SELECT 1 FROM lab.trajectory_records t WHERE t.experiment_id=e.experiment_id))) THEN
  RAISE EXCEPTION 'terminal finalization is stale, incomplete, or outside cleanup window';
 END IF;
 RETURN f;
END; $$;

CREATE FUNCTION lab.prepare_terminal_finalization(
 p_run uuid,p_generation integer,p_invocation text,p_execution text,
 p_plan text,p_application text,p_state text,p_marker text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE a jsonb; s jsonb; m jsonb; ae jsonb; se jsonb; me jsonb;
 ah text; sh text; mh text; plan_sha text; expected_plan jsonb; item lab.runs%ROWTYPE;
 old lab.terminal_finalizations%ROWTYPE; reservation lab.holdout_reservations%ROWTYPE;
BEGIN
 PERFORM lab.assert_director_generation_identity(p_run,p_generation,p_invocation,p_execution);
 SELECT * INTO item FROM lab.runs WHERE run_id=p_run;
 IF item.state IS DISTINCT FROM 'running' OR item.stop_requested OR
    item.request_json->>'purpose'='baseline' THEN
  RAISE EXCEPTION 'research terminal seal requires an active research identity';
 END IF;
 a:=p_application::jsonb; s:=p_state::jsonb; m:=p_marker::jsonb;
 ah:=encode(sha256(convert_to(p_application,'UTF8')),'hex');
 sh:=encode(sha256(convert_to(p_state,'UTF8')),'hex');
 mh:=encode(sha256(convert_to(p_marker,'UTF8')),'hex');
 SELECT event_json INTO ae FROM lab.run_events WHERE run_id=p_run
  AND event_type='director.checkpoint' AND event_json->>'payload_sha256'=ah;
 SELECT event_json INTO se FROM lab.run_events WHERE run_id=p_run
  AND event_type='director.checkpoint' AND event_json->>'payload_sha256'=sh;
 SELECT event_json INTO me FROM lab.run_events WHERE run_id=p_run
  AND event_type='director.checkpoint' AND event_json->>'key'='holdout-state-application:run_end:1';
 IF a IS NULL OR s IS NULL OR m IS NULL OR ae IS NULL OR se IS NULL OR me IS NULL OR
    me->>'payload_sha256' IS DISTINCT FROM mh OR
    me->>'phase' IS DISTINCT FROM 'holdout_state_application' OR
    se->>'phase' IS DISTINCT FROM 'director_loop_state' OR
    ae->>'phase' NOT IN ('holdout_application','holdout_run_end_unavailable',
      'holdout_run_end_admission_failure') OR
    a->>'run_id' IS DISTINCT FROM p_run::text OR s->>'run_id' IS DISTINCT FROM p_run::text OR
    m->>'run_id' IS DISTINCT FROM p_run::text OR
    a->>'admitted_generation' IS DISTINCT FROM p_generation::text OR
    a->>'execution_sha256' IS DISTINCT FROM p_execution OR
    m->>'application_sha256' IS DISTINCT FROM ah OR m->>'state_sha256' IS DISTINCT FROM sh OR
    NOT ((ae->>'sequence')::integer < (se->>'sequence')::integer AND
         (se->>'sequence')::integer < (me->>'sequence')::integer) OR
    EXISTS (SELECT 1 FROM lab.run_events e WHERE e.run_id=p_run
      AND e.event_type='director.checkpoint' AND e.event_json->>'phase'='director_loop_state'
      AND (e.event_json->>'sequence')::integer > (se->>'sequence')::integer) THEN
  RAISE EXCEPTION 'terminal finalization lacks exact durable application/state/marker chain';
 END IF;
 IF ae->>'phase'='holdout_application' THEN
  SELECT * INTO reservation FROM lab.holdout_reservations
   WHERE reservation_id=(a->>'reservation_id')::uuid;
  IF reservation.run_id IS DISTINCT FROM p_run OR reservation.trigger_kind IS DISTINCT FROM
    'run_end' OR
     reservation.trigger_index IS DISTINCT FROM 1 OR reservation.admitted_generation IS
    DISTINCT FROM p_generation OR
     reservation.execution_sha256 IS DISTINCT FROM p_execution OR
     reservation.candidate_experiment_id IS DISTINCT FROM a->>'candidate_experiment_id' OR
     reservation.state NOT IN ('passed','reverted','failed','exhausted') OR
     reservation.state IS DISTINCT FROM a->>'state' OR
     (reservation.state IN ('passed','reverted') AND NOT EXISTS (
       SELECT 1 FROM scorer.holdout_results h WHERE h.reservation_id=reservation.reservation_id
        AND h.result_sha256=reservation.result_sha256)) OR
     to_jsonb(reservation.result_bit) IS DISTINCT FROM nullif(a->'bit','null'::jsonb) OR
     s->>'holdout_last_status' IS DISTINCT FROM
       (CASE WHEN reservation.state='exhausted' THEN 'quota_exhausted' ELSE reservation.state
    END) THEN
   RAISE EXCEPTION 'terminal holdout receipt differs from its durable Scorer result';
  END IF;
 ELSE
  IF a->>'state' IS DISTINCT FROM 'failed' OR a->'bit' IS DISTINCT FROM 'null'::jsonb OR
     s->>'holdout_last_status' IS DISTINCT FROM 'failed' OR
     ae->'holdout_closure_application'->>'expected_state_sha256' IS DISTINCT FROM sh OR
     se->'holdout_closure_state'->>'application_sha256' IS DISTINCT FROM ah OR
     me->'holdout_closure_marker'->>'state_sha256' IS DISTINCT FROM sh THEN
   RAISE EXCEPTION 'failed terminal closure lacks its verified rollback chain';
  END IF;
 END IF;
 SELECT jsonb_agg(jsonb_build_object('experiment_id',experiment_id,
   'evaluation_kind',evaluation_kind,
   'task_id',task_id,'seed',seed,'candidate_sha256',candidate_sha256,'dataset_id',dataset_id,
   'split_id',split_id,'session_id',session_id) ORDER BY experiment_id COLLATE "C",
    evaluation_kind COLLATE "C",task_id COLLATE "C",seed)
 INTO expected_plan FROM scorer.run_tasks WHERE run_id=p_run;
 IF expected_plan IS NULL OR p_plan::jsonb IS DISTINCT FROM expected_plan THEN
  RAISE EXCEPTION 'terminal seal differs from the exact durable task plan';
 END IF;
 plan_sha:=encode(sha256(convert_to(p_plan,'UTF8')),'hex');
 INSERT INTO lab.terminal_finalizations(run_id,generation,invocation_id,execution_sha256,
  payload_sha256,source_kind,source_sha256,task_plan_sha256,task_plan_count)
 VALUES(p_run,p_generation,p_invocation,p_execution,item.payload_sha256,'holdout',mh,plan_sha,
  jsonb_array_length(expected_plan)) ON CONFLICT DO NOTHING;
 SELECT * INTO old FROM lab.terminal_finalizations WHERE run_id=p_run;
 IF old.generation IS DISTINCT FROM p_generation OR old.invocation_id IS DISTINCT FROM
    p_invocation OR
    old.execution_sha256 IS DISTINCT FROM p_execution OR old.source_sha256 IS DISTINCT FROM mh OR
    old.task_plan_sha256 IS DISTINCT FROM plan_sha OR old.source_kind IS DISTINCT FROM
    'holdout' THEN
  RAISE EXCEPTION 'terminal finalization retry changed its immutable identity';
 END IF;
 PERFORM lab.verify_terminal_finalization(p_run,p_generation,p_execution);
 PERFORM set_config('lab.terminal_finalization_run_id',p_run::text,true);
END; $$;


CREATE FUNCTION lab.prepare_scorer_baseline_finalization(
 p_run uuid,p_generation integer,p_execution text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.runs%ROWTYPE; baseline jsonb;
BEGIN
 IF session_user <> 'swapp_lab_scorer' THEN
  RAISE EXCEPTION 'baseline finalization requires Scorer';
 END IF;
 PERFORM pg_advisory_xact_lock(
    ('x'||substr(encode(sha256(uuid_send(p_run)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=p_run FOR UPDATE;
 IF r.request_json->>'purpose'='baseline' AND NOT EXISTS (
   SELECT 1 FROM lab.terminal_finalizations WHERE run_id=p_run) THEN
  baseline:=lab.verify_baseline_operation(p_run);
  INSERT INTO lab.terminal_finalizations(run_id,generation,invocation_id,execution_sha256,
    payload_sha256,source_kind,source_sha256,task_plan_sha256,task_plan_count)
  SELECT p_run,p_generation,o.worker_invocation_id,p_execution,r.payload_sha256,'baseline',
    baseline->>'calibration_sha256',r.task_plan_sha256,r.task_plan_count
  FROM lab.director_owner_generations o WHERE o.run_id=p_run AND o.generation=p_generation
    AND o.execution_sha256=p_execution;
 END IF;
 PERFORM lab.verify_terminal_finalization(p_run,p_generation,p_execution);
END; $$;
REVOKE ALL ON FUNCTION lab.prepare_scorer_baseline_finalization(uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.prepare_scorer_baseline_finalization(uuid,integer,text)
 TO swapp_lab_scorer;
CREATE FUNCTION lab.assert_scorer_terminal_finalization(p_run uuid,p_generation
    integer,p_execution text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab,scorer AS $$
DECLARE f lab.terminal_finalizations%ROWTYPE; r lab.runs%ROWTYPE;
BEGIN
 IF session_user <> 'swapp_lab_scorer' THEN RAISE EXCEPTION 'terminal report requires Scorer';
    END IF;
 PERFORM pg_advisory_xact_lock(
    ('x'||substr(encode(sha256(uuid_send(p_run)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=p_run FOR UPDATE;
 f:=lab.verify_terminal_finalization(p_run,p_generation,p_execution);
 IF r.task_plan_sha256 IS DISTINCT FROM f.task_plan_sha256 OR
    r.task_plan_count IS DISTINCT FROM f.task_plan_count THEN
  RAISE EXCEPTION 'terminal report requires its exact sealed task plan';
 END IF;
 PERFORM set_config('lab.scorer_run_id',p_run::text,true);
 PERFORM set_config('lab.scorer_generation',p_generation::text,true);
 PERFORM set_config('lab.scorer_execution_sha256',p_execution,true);
 PERFORM set_config('lab.scorer_stop_closure','false',true);
 PERFORM set_config('lab.scorer_terminal_finalization',p_run::text,true);
 RETURN jsonb_build_object('run_id',p_run,'admitted_generation',p_generation,
   'execution_sha256',p_execution);
END; $$;


        CREATE OR REPLACE FUNCTION lab.guard_director_run_update_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        DECLARE target_run uuid;
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                RETURN NULL;
            END IF;
            IF session_user='swapp_lab_scorer' THEN
                BEGIN
                    target_run := nullif(current_setting('lab.scorer_run_id',true),'')::uuid;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Scorer run update context is malformed';
                END;
                IF target_run IS NULL THEN
                    BEGIN
                        target_run := nullif(
                            current_setting('lab.scorer_baseline_empty_stop_run_id',true),''
                        )::uuid;
                    EXCEPTION WHEN OTHERS THEN
                        RAISE EXCEPTION 'Scorer baseline stop context is malformed';
                    END;
                    PERFORM lab.assert_scorer_empty_baseline_stop(target_run);
                    RETURN NULL;
                END IF;
                IF current_setting('lab.scorer_terminal_finalization',true)=target_run::text THEN
                    PERFORM lab.assert_scorer_terminal_finalization(target_run,
                        nullif(current_setting('lab.scorer_generation',true),'')::integer,
                        nullif(current_setting('lab.scorer_execution_sha256',true),''));
                    RETURN NULL;
                END IF;
                IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                    PERFORM lab.assert_scorer_run_stop_execution(
                        target_run,
                        nullif(current_setting('lab.scorer_generation',true),'')::integer,
                        nullif(current_setting('lab.scorer_execution_sha256',true),'')
                    );
                ELSE
                    PERFORM lab.assert_scorer_run_execution(
                        target_run,
                        nullif(current_setting('lab.scorer_generation',true),'')::integer,
                        nullif(current_setting('lab.scorer_execution_sha256',true),'')
                    );
                END IF;
                RETURN NULL;
            END IF;
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') THEN
                RAISE EXCEPTION 'run update requires the Director or Planner role';
            END IF;
            BEGIN
                target_run := coalesce(
                    nullif(current_setting('lab.owner_run_id',true),'')::uuid,
                    nullif(current_setting('lab.initial_director_claim',true),'')::uuid,
                    nullif(current_setting('lab.closure_run_id',true),'')::uuid,
                    nullif(current_setting('lab.api_stop_run_id',true),'')::uuid
                );
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'run update has a malformed ownership target';
            END;
            IF target_run IS NULL THEN
                RAISE EXCEPTION 'run update requires an owned or authorized transition';
            END IF;
            PERFORM lab.lock_run_plan(target_run);
            RETURN NULL;
        END;
        $$
        ;


        CREATE OR REPLACE FUNCTION lab.guard_director_run_update()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab AS $$
        BEGIN
            IF session_user='swapp_lab_migrator' THEN
                RETURN NEW;
            END IF;
            IF session_user='swapp_lab_scorer' THEN
                IF current_setting('lab.scorer_baseline_empty_stop_run_id',true)=
                       NEW.run_id::text AND
                   OLD.state='stop_requested' AND NEW.state='stop_requested' AND
                   OLD.stop_requested AND NEW.stop_requested AND
                   OLD.task_plan_sha256 IS NULL AND
                   (OLD.task_plan_count IS NULL OR OLD.task_plan_count=0) AND
                   NEW.task_plan_sha256=
                       '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945' AND
                   NEW.task_plan_count=0 AND
                   (to_jsonb(NEW) - ARRAY[
                       'task_plan_sha256','task_plan_count','updated_at'
                   ]) IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY[
                       'task_plan_sha256','task_plan_count','updated_at'
                   ]) THEN
                    PERFORM lab.assert_scorer_empty_baseline_stop(NEW.run_id);
                    RETURN NEW;
                END IF;
                IF current_setting('lab.scorer_baseline_empty_stop_run_id',true)
                        IS NOT NULL AND
                   current_setting('lab.scorer_baseline_empty_stop_run_id',true)=
                       NEW.run_id::text AND
                   OLD.state='stop_requested' AND NEW.state='stopped' AND
                   OLD.stop_requested AND NOT NEW.stop_requested AND
                   NEW.report_sha256 IS NOT NULL AND
                   (to_jsonb(NEW) - ARRAY['state','stop_requested','report_sha256','updated_at'])
                       IS NOT DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY[
                       'state','stop_requested','report_sha256','updated_at'
                   ]) THEN
                    PERFORM lab.assert_scorer_empty_baseline_stop(NEW.run_id);
                    RETURN NEW;
                END IF;
                IF current_setting('lab.scorer_run_id',true) IS DISTINCT FROM NEW.run_id::text OR
                   (to_jsonb(NEW) - ARRAY['state','stop_requested','report_sha256','updated_at'])
                       IS DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['state','stop_requested','report_sha256','updated_at']) OR
                   NEW.report_sha256 IS NULL OR NEW.stop_requested THEN
                    RAISE EXCEPTION 'Scorer run publication is not bound to the captured owner';
                END IF;
                IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                    IF OLD.state IS DISTINCT FROM 'stop_requested' OR
                       NEW.state IS DISTINCT FROM 'stopped' THEN
                        RAISE EXCEPTION 'Scorer stop closure has an invalid run transition';
                    END IF;
                ELSIF OLD.state IS DISTINCT FROM 'running' OR
                      NEW.state NOT IN ('completed','failed') THEN
                    RAISE EXCEPTION 'Scorer active publication has an invalid run transition';
                END IF;
                RETURN NEW;
            END IF;
            IF session_user NOT IN ('swapp_lab_director','swapp_lab_planner') THEN
                RAISE EXCEPTION 'run update requires the Director or Planner role';
            END IF;
            IF OLD.state='queued' AND NEW.state='running' AND
               current_setting('lab.initial_director_claim',true)=NEW.run_id::text THEN
                RETURN NEW;
            END IF;
            IF OLD.state IN ('queued','running') AND NEW.state='stop_requested' AND
               NEW.stop_requested AND
               current_setting('lab.api_stop_run_id',true)=NEW.run_id::text AND
               (to_jsonb(NEW) - ARRAY['state','stop_requested','updated_at']) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['state','stop_requested','updated_at']) THEN
                RETURN NEW;
            END IF;
            IF OLD.state='running' AND NEW.state IN ('failed','stop_requested') AND
               current_setting('lab.closure_run_id',true)=NEW.run_id::text THEN
                PERFORM lab.assert_director_generation_identity(
                    NEW.run_id,
                    nullif(current_setting('lab.closure_generation',true),'')::integer,
                    nullif(current_setting('lab.closure_invocation_id',true),''),
                    nullif(current_setting('lab.closure_execution_sha256',true),'')
                );
                RETURN NEW;
            END IF;
            IF OLD.state='stop_requested' AND NEW.state='stop_requested' AND
               OLD.stop_requested AND NEW.stop_requested AND
               OLD.task_plan_sha256 IS NULL AND NEW.task_plan_sha256 IS NOT NULL AND
               OLD.task_plan_count IS NULL AND NEW.task_plan_count IS NOT NULL AND
               current_setting('lab.owner_run_id',true)=NEW.run_id::text AND
               (to_jsonb(NEW) - ARRAY[
                   'task_plan_sha256','task_plan_count','updated_at'
               ]) IS NOT DISTINCT FROM
               (to_jsonb(OLD) - ARRAY[
                   'task_plan_sha256','task_plan_count','updated_at'
               ]) THEN
                PERFORM lab.assert_director_generation_identity(
                    NEW.run_id,
                    nullif(current_setting('lab.owner_generation',true),'')::integer,
                    nullif(current_setting('lab.owner_invocation_id',true),''),
                    nullif(current_setting('lab.owner_execution_sha256',true),'')
                );
                RETURN NEW;
            END IF;
            IF current_setting('lab.terminal_finalization_run_id',true)=OLD.run_id::text
               AND OLD.state='running' AND NEW.state='running'
               AND OLD.task_plan_sha256 IS NULL AND OLD.task_plan_count IS NULL
               AND (to_jsonb(NEW)-ARRAY['task_plan_sha256','task_plan_count','updated_at'])
                 IS NOT DISTINCT FROM
                   (to_jsonb(OLD)-ARRAY['task_plan_sha256','task_plan_count','updated_at']) THEN
                PERFORM lab.assert_director_generation_identity(OLD.run_id,
                    nullif(current_setting('lab.owner_generation',true),'')::integer,
                    nullif(current_setting('lab.owner_invocation_id',true),''),
                    nullif(current_setting('lab.owner_execution_sha256',true),''));
                PERFORM lab.verify_terminal_finalization(OLD.run_id,
                    nullif(current_setting('lab.owner_generation',true),'')::integer,
                    nullif(current_setting('lab.owner_execution_sha256',true),''));
                IF NOT EXISTS (SELECT 1 FROM lab.terminal_finalizations f
                    WHERE f.run_id=NEW.run_id AND f.task_plan_sha256=NEW.task_plan_sha256
                      AND f.task_plan_count=NEW.task_plan_count) THEN
                    RAISE EXCEPTION 'terminal seal does not match its immutable authorization';
                END IF;
                RETURN NEW;
            END IF;
            PERFORM lab.assert_director_owner_context(OLD.run_id);
            RETURN NEW;
        END;
        $$
        ;


        CREATE OR REPLACE FUNCTION lab.guard_scorer_owned_statement()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, lab, scorer AS $$
        DECLARE run_target uuid; generation_target integer; execution_target text;
            director_generation integer; director_invocation text;
        BEGIN
            IF session_user IN ('swapp_lab_director','swapp_lab_planner') THEN
                BEGIN
                    run_target := nullif(current_setting('lab.owner_run_id',true),'')::uuid;
                    director_generation := nullif(
                        current_setting('lab.owner_generation',true),'')::integer;
                EXCEPTION WHEN OTHERS THEN
                    RAISE EXCEPTION 'Director-owned Scorer mutation context is malformed';
                END;
                director_invocation := nullif(
                    current_setting('lab.owner_invocation_id',true),'');
                execution_target := nullif(
                    current_setting('lab.owner_execution_sha256',true),'');
                IF run_target IS NULL OR director_generation IS NULL OR
                   director_invocation IS NULL OR execution_target IS NULL THEN
                    RAISE EXCEPTION 'Director-owned Scorer mutation has no captured owner';
                END IF;
                IF TG_TABLE_NAME IN ('score_jobs','task_terminal_outcomes',
                                     'task_completions') AND
                   (SELECT state FROM lab.runs WHERE run_id=run_target)='stop_requested' THEN
                    PERFORM lab.assert_director_generation_identity(
                        run_target,director_generation,director_invocation,execution_target
                    );
                ELSE
                    PERFORM lab.assert_director_owner_context(run_target);
                END IF;
                RETURN NULL;
            ELSIF session_user <> 'swapp_lab_scorer' THEN
                RAISE EXCEPTION 'Scorer mutation requires the Scorer role';
            END IF;
            BEGIN
                run_target := nullif(current_setting('lab.scorer_run_id',true),'')::uuid;
                generation_target := nullif(
                    current_setting('lab.scorer_generation',true),'')::integer;
                execution_target := nullif(
                    current_setting('lab.scorer_execution_sha256',true),'');
            EXCEPTION WHEN OTHERS THEN
                RAISE EXCEPTION 'Scorer execution context is malformed';
            END;
            IF run_target IS NULL THEN
                run_target := nullif(
                    current_setting('lab.scorer_baseline_empty_stop_run_id',true),''
                )::uuid;
                IF run_target IS NULL OR TG_TABLE_SCHEMA <> 'lab' OR
                   TG_TABLE_NAME <> 'reports' THEN
                    RAISE EXCEPTION 'Scorer mutation has no captured run identity';
                END IF;
                PERFORM lab.assert_scorer_empty_baseline_stop(run_target);
                RETURN NULL;
            END IF;
            IF generation_target IS NULL OR execution_target IS NULL THEN
                RAISE EXCEPTION 'Scorer mutation has no captured generation';
            END IF;
            IF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME='reports' AND
               current_setting('lab.scorer_terminal_finalization',true)=run_target::text THEN
                PERFORM lab.assert_scorer_terminal_finalization(
                    run_target,generation_target,execution_target);
                RETURN NULL;
            END IF;
            IF current_setting('lab.scorer_stop_closure',true)='true' THEN
                PERFORM lab.assert_scorer_run_stop_execution(
                    run_target,generation_target,execution_target
                );
            ELSE
                PERFORM lab.assert_scorer_run_execution(
                    run_target,generation_target,execution_target
                );
            END IF;
            RETURN NULL;
        END;
        $$
        ;

CREATE FUNCTION lab.assert_scorer_report_execution(p_run uuid,p_generation integer,p_execution text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
BEGIN
 IF EXISTS (SELECT 1 FROM lab.terminal_finalizations WHERE run_id=p_run) OR
    EXISTS (SELECT 1 FROM lab.baseline_operations WHERE run_id=p_run) THEN
  RETURN lab.assert_scorer_terminal_finalization(p_run,p_generation,p_execution);
 END IF;
 RETURN lab.assert_scorer_run_execution(p_run,p_generation,p_execution);
END; $$;
REVOKE ALL ON FUNCTION lab.verify_terminal_finalization(uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.verify_terminal_finalization(uuid,integer,text) TO
    swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.prepare_terminal_finalization(
    uuid,integer,text,text,text,text,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.prepare_terminal_finalization(
    uuid,integer,text,text,text,text,text,text) TO swapp_lab_director,swapp_lab_planner;
REVOKE ALL ON FUNCTION lab.assert_scorer_terminal_finalization(uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.assert_scorer_terminal_finalization(uuid,integer,text) TO
    swapp_lab_scorer;
REVOKE ALL ON FUNCTION lab.assert_scorer_report_execution(uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION lab.assert_scorer_report_execution(uuid,integer,text) TO swapp_lab_scorer;

    """)


def downgrade() -> None:
    raise RuntimeError("terminal finalization receipts require forward-only migration")
