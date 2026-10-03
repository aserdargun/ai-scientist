"""Admit unscored mode streams through the existing execution and report fences."""

from alembic import op

revision = "0043_mode_stream"
down_revision = "0042_failure_stop_event"
branch_labels = None
depends_on = None


def _replace_body(signature: str, old: str, new: str) -> None:
    """Patch one asserted fragment, preserving subsequent recovery guard branches."""
    # All arguments are migration literals, never runtime or user input.
    op.execute(f"""DO $rewrite$
DECLARE definition text; old_fragment text := $old${old}$old$;
BEGIN
 SELECT pg_get_functiondef('{signature}'::regprocedure) INTO definition;
 IF (length(definition)-length(replace(definition,old_fragment,'')))
       / length(old_fragment) <> 1 THEN
  RAISE EXCEPTION 'mode stream guard source shape changed: {signature}';
 END IF;
 EXECUTE replace(definition,old_fragment,$new${new}$new$);
END $rewrite$;""")


def upgrade() -> None:
    """Keep existing owners, immutable history and the original stop deadline."""
    op.execute(r"""
CREATE FUNCTION lab.is_mode_stream_intent(p_intent jsonb)
 RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
 SELECT coalesce(p_intent @> '{"purpose": "mode-stream", "provider": "mode-stream",
 "program_version": "mode-stream.v1", "track": "mode", "proposal_limit": 0,
 "budget": {"experiments": 0, "model_tokens": 0}}'::jsonb AND
 p_intent->>'proposal_limit'='0' AND p_intent#>>'{budget,experiments}'='0' AND
 p_intent#>>'{budget,model_tokens}'='0' AND
 jsonb_typeof(p_intent#>'{budget,wall_seconds}')='number' AND
 CASE WHEN p_intent#>>'{budget,wall_seconds}' ~ '^[1-9][0-9]{0,4}$'
  THEN (p_intent#>>'{budget,wall_seconds}')::integer BETWEEN 1 AND 14400
  ELSE false END,false)
$$;
REVOKE ALL ON FUNCTION lab.is_mode_stream_intent(jsonb) FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;

CREATE OR REPLACE FUNCTION scorer.guard_task_plan_seal()
 RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
BEGIN
 IF OLD.task_plan_sha256 IS NOT NULL AND
 (NEW.task_plan_sha256 IS DISTINCT FROM OLD.task_plan_sha256 OR
 NEW.task_plan_count IS DISTINCT FROM OLD.task_plan_count) THEN
  RAISE EXCEPTION 'sealed task plan is immutable';
 END IF;
 IF (NEW.task_plan_sha256 IS NULL) <> (NEW.task_plan_count IS NULL) OR
 (NEW.task_plan_sha256 IS NOT NULL AND
 (length(NEW.task_plan_sha256) <> 64 OR NEW.task_plan_count < 0)) THEN
  RAISE EXCEPTION 'task plan seal is malformed';
 END IF;
 IF NEW.task_plan_count=0 THEN
  IF lab.is_mode_stream_intent(NEW.request_json) THEN
   IF NEW.state NOT IN ('running','stop_requested') OR NEW.task_plan_sha256 <>
    '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945' OR
    EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=NEW.run_id) OR
    EXISTS(SELECT 1 FROM scorer.run_tasks WHERE run_id=NEW.run_id) OR
    EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=NEW.run_id) OR
    EXISTS(SELECT 1 FROM lab.holdout_reservations WHERE run_id=NEW.run_id) THEN
    RAISE EXCEPTION 'mode stream requires a canonical empty score plan without research work';
   END IF;
  ELSIF NEW.state <> 'stop_requested' THEN
   RAISE EXCEPTION 'empty task plans are allowed only for stop finalization';
  END IF;
 END IF;
 RETURN NEW;
END $$;
""")
    _replace_body(
        "lab.guard_baseline_intent_mutation()",
        "OLD.request_json->>'purpose'='baseline'",
        "OLD.request_json->>'purpose' IN ('baseline','mode-stream')",
    )
    _replace_body(
        "lab.guard_baseline_intent_mutation()",
        "IF intent->>'purpose' IS DISTINCT FROM 'baseline' THEN RETURN NEW; END IF;",
        """IF intent->>'purpose'='mode-stream' THEN
            RAISE EXCEPTION 'mode stream intent rejects experiments, score tasks and holdout';
          END IF;
          IF intent->>'purpose' IS DISTINCT FROM 'baseline' THEN RETURN NEW; END IF;""",
    )
    _replace_body(
        "lab.assert_scorer_empty_baseline_stop(uuid)",
        "run_item.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR",
        """(run_item.request_json->>'purpose' IS DISTINCT FROM 'baseline' AND
                NOT lab.is_mode_stream_intent(run_item.request_json)) OR
               (run_item.request_json->>'purpose'='mode-stream' AND EXISTS(
                SELECT 1 FROM lab.run_events WHERE run_id=p_run_id
                 AND event_type='director.checkpoint')) OR""",
    )
    _replace_body(
        "lab.prepare_unstarted_baseline_stop_0024_unfenced(uuid,text)",
        "r.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR",
        """(r.request_json->>'purpose' IS DISTINCT FROM 'baseline' AND
                NOT lab.is_mode_stream_intent(r.request_json)) OR
               (r.request_json->>'purpose'='mode-stream' AND EXISTS(
                SELECT 1 FROM lab.run_events WHERE run_id=p_run_id
                 AND event_type='director.checkpoint')) OR""",
    )
    _replace_body(
        "lab.guard_scorer_owned_row()",
        """IF NEW.report_json->>'schema' IS DISTINCT FROM 'lab.baseline-report.v1' OR
                       NEW.report_json->>'run_id' IS DISTINCT FROM run_target::text OR
                       NEW.report_json->>'purpose' IS DISTINCT FROM 'baseline' OR""",
        """IF NOT EXISTS(SELECT 1 FROM lab.runs r WHERE r.run_id=run_target AND (
                        (r.request_json->>'purpose'='baseline' AND
                         NEW.report_json->>'schema'='lab.baseline-report.v1' AND
                         NEW.report_json->>'purpose'='baseline') OR
                        (lab.is_mode_stream_intent(r.request_json) AND
                         NEW.report_json->>'schema'='lab.mode-stream-report.v1' AND
                         NEW.report_json->>'purpose'='mode-stream' AND
                         NEW.report_json->>'program_version'='mode-stream.v1')))
                       OR NEW.report_json->>'run_id' IS DISTINCT FROM run_target::text OR""",
    )
    op.execute(r"""
CREATE FUNCTION lab.mode_stream_report_evidence(
 p_run uuid,p_generation integer,p_sha text,p_empty boolean
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,lab,scorer AS $$
DECLARE r lab.runs%ROWTYPE; checkpoint_count integer; checkpoints jsonb;
 first_stop timestamptz; deadline timestamptz;
 owners jsonb := '[]'::jsonb; owner jsonb := NULL;
BEGIN
 IF session_user<>'swapp_lab_scorer' OR p_run IS NULL OR p_empty IS NULL OR
 current_setting('transaction_isolation')<>'read committed' OR
 (p_empty AND (p_generation IS NOT NULL OR p_sha IS NOT NULL)) OR
 (NOT p_empty AND (p_generation IS NULL OR p_generation<1 OR
 p_sha IS NULL OR p_sha !~ '^[0-9a-f]{64}$')) THEN
  RAISE EXCEPTION 'mode stream report requires one exact Scorer execution identity';
 END IF;
 -- The same lifecycle lock as lock_run_plan, which is private to Director/Planner.
 -- Acquire it before any row lock; the original assertions then revalidate control.
 PERFORM pg_advisory_xact_lock(
  ('x'||substr(encode(sha256(uuid_send(p_run)),'hex'),1,16))::bit(64)::bigint);
 SELECT * INTO r FROM lab.runs WHERE run_id=p_run FOR UPDATE;
 IF r.run_id IS NULL OR NOT lab.is_mode_stream_intent(r.request_json) THEN
  RAISE EXCEPTION 'report evidence requires an exact mode-stream.v1 intent';
 END IF;
 IF p_empty THEN
  PERFORM lab.assert_scorer_empty_baseline_stop(p_run);
  IF r.state IS DISTINCT FROM 'stop_requested' OR r.stop_requested IS DISTINCT FROM TRUE THEN
   RAISE EXCEPTION 'unstarted mode stream evidence requires its pending stop';
  END IF;
  SELECT min(created_at) INTO first_stop FROM lab.run_events
   WHERE run_id=p_run AND event_type='run.stop_requested';
  IF first_stop IS NULL THEN
   RAISE EXCEPTION 'unstarted mode stream original deadline missing';
  END IF;
  deadline:=r.created_at+make_interval(secs=>(r.request_json#>>'{budget,wall_seconds}')::integer);
  IF least(deadline,first_stop+interval '120 seconds')<=clock_timestamp() THEN
   RAISE EXCEPTION 'unstarted mode stream original deadline expired';
  END IF;
 ELSIF r.state='stop_requested' THEN
  PERFORM lab.assert_scorer_run_stop_execution(p_run,p_generation,p_sha);
  -- NULL closure id preserves min(original deadline, first stop + 120 seconds).
  -- It creates no recovery receipt and grants no additional cleanup time.
  PERFORM lab.assert_attempted_stop_window(p_run,NULL::uuid);
 ELSE
  PERFORM lab.assert_scorer_report_execution(p_run,p_generation,p_sha);
 END IF;
 IF r.task_plan_sha256 IS DISTINCT FROM
  '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945' OR
 r.task_plan_count IS DISTINCT FROM 0 OR
 EXISTS(SELECT 1 FROM lab.experiments WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM scorer.task_scores WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM scorer.task_completions WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM lab.baseline_operations WHERE run_id=p_run) OR
 EXISTS(SELECT 1 FROM lab.baseline_calibrations WHERE run_id=p_run) THEN
  RAISE EXCEPTION 'mode stream report requires an empty sealed score plan and no research work';
 END IF;
 SELECT count(*) INTO checkpoint_count FROM (
  SELECT 1 FROM lab.run_events WHERE run_id=p_run AND event_type='director.checkpoint'
  LIMIT 1027
 ) bounded;
 IF checkpoint_count>1026 THEN
  RAISE EXCEPTION 'mode stream checkpoint count exceeds its finite plan';
 END IF;
 IF EXISTS(SELECT 1 FROM lab.run_events WHERE run_id=p_run
  AND event_type='director.checkpoint' AND (
   jsonb_typeof(event_json->'sequence') IS DISTINCT FROM 'number' OR
   (event_json->>'sequence') !~ '^(0|[1-9][0-9]{0,3})$')) THEN
  RAISE EXCEPTION 'mode stream checkpoint sequence is malformed';
 END IF;
 SELECT coalesce(jsonb_agg(event_json ORDER BY (event_json->>'sequence')::integer),
  '[]'::jsonb) INTO checkpoints FROM lab.run_events
  WHERE run_id=p_run AND event_type='director.checkpoint';
 IF NOT p_empty THEN
  SELECT coalesce(jsonb_agg(jsonb_build_object('generation',g.generation,
   'execution_sha256',g.execution_sha256) ORDER BY g.generation),'[]'::jsonb)
   INTO owners FROM lab.director_owner_generations g
   JOIN lab.director_execution_contracts x USING(run_id)
   JOIN lab.director_execution_control c USING(run_id)
   WHERE g.run_id=p_run AND g.generation<=c.current_generation
    AND c.current_generation=p_generation AND g.execution_sha256=p_sha
    AND x.execution_sha256=p_sha AND x.payload_sha256=r.payload_sha256;
  SELECT jsonb_build_object('payload_sha256',x.payload_sha256,
   'worker_pid',g.worker_pid,'worker_start_ticks',g.worker_start_ticks,
   'worker_boot_id',g.worker_boot_id,'worker_unit',g.worker_unit,
   'worker_invocation_id',g.worker_invocation_id,'worker_cgroup',g.worker_cgroup)
   INTO owner FROM lab.director_owner_generations g
   JOIN lab.director_execution_contracts x USING(run_id)
   WHERE g.run_id=p_run AND g.generation=p_generation
    AND g.execution_sha256=p_sha AND x.execution_sha256=p_sha
    AND x.payload_sha256=r.payload_sha256;
  IF owner IS NULL THEN RAISE EXCEPTION 'mode stream current owner is unavailable'; END IF;
 END IF;
 RETURN jsonb_build_object('run_id',p_run,'checkpoints',checkpoints,
  'owners',owners,'owner',owner);
END $$;
REVOKE ALL ON FUNCTION lab.mode_stream_report_evidence(uuid,integer,text,boolean) FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner,swapp_lab_scorer;
GRANT EXECUTE ON FUNCTION lab.mode_stream_report_evidence(uuid,integer,text,boolean)
 TO swapp_lab_scorer;
""")


def downgrade() -> None:
    """Reject automatic rollback that could strand immutable stream history."""
    raise RuntimeError("mode stream execution and report history require a preserving rollback")
