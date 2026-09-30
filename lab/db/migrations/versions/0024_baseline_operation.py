"""Bind independent baseline completion to a whole-matrix Scorer receipt."""

# The multiline PL/pgSQL statements intentionally exceed Python source line limits.
# ruff: noqa: E501

from __future__ import annotations

from alembic import op

revision = "0024_baseline_operation"
down_revision = "0023_care_calibration_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE lab.baseline_operations ("
        "run_id uuid PRIMARY KEY REFERENCES lab.runs(run_id) ON DELETE CASCADE,"
        "request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),"
        "suite_manifest_sha256 text NOT NULL CHECK(suite_manifest_sha256 ~ '^[0-9a-f]{64}$'),"
        "harness_sha256 text NOT NULL CHECK(harness_sha256 ~ '^[0-9a-f]{64}$'),"
        "image_sha256 text NOT NULL CHECK(image_sha256 ~ '^[0-9a-f]{64}$'),"
        "calibration_sha256 text NOT NULL CHECK(calibration_sha256 ~ '^[0-9a-f]{64}$'),"
        "task_plan_sha256 text NOT NULL CHECK(task_plan_sha256 ~ '^[0-9a-f]{64}$'),"
        "task_plan_count integer NOT NULL CHECK(task_plan_count > 0),"
        "budget_receipt jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"
    )
    op.execute(
        "REVOKE ALL ON lab.baseline_operations FROM PUBLIC, swapp_lab_director, "
        "swapp_lab_planner, swapp_lab_scorer"
    )
    op.execute("GRANT DELETE ON lab.baseline_operations TO swapp_lab_migrator")
    op.execute(
        r"""
        CREATE FUNCTION lab.guard_baseline_intent_mutation()
        RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
        DECLARE intent jsonb;
        BEGIN
          IF TG_TABLE_SCHEMA='lab' AND TG_TABLE_NAME='runs' THEN
            IF OLD.request_json->>'purpose'='baseline' AND
               (NEW.request_json IS DISTINCT FROM OLD.request_json OR
                NEW.payload_sha256 IS DISTINCT FROM OLD.payload_sha256) THEN
              RAISE EXCEPTION 'baseline request identity is immutable';
            END IF;
            RETURN NEW;
          END IF;
          SELECT request_json INTO intent FROM lab.runs WHERE run_id=NEW.run_id;
          IF intent->>'purpose' IS DISTINCT FROM 'baseline' THEN RETURN NEW; END IF;
          IF TG_TABLE_NAME='experiments' THEN
            IF NEW.kind <> 'baseline' OR NEW.baseline_name NOT IN
                 ('robust_z','iforest','ecod_train_frozen') THEN
              RAISE EXCEPTION 'baseline intent rejects proposal experiments';
            END IF;
          ELSIF TG_TABLE_NAME='run_tasks' THEN
            IF NEW.evaluation_kind <> 'baseline' OR NEW.seed NOT BETWEEN 0 AND 2 THEN
              RAISE EXCEPTION 'baseline task plan rejects non-baseline work';
            END IF;
          ELSIF TG_TABLE_NAME='holdout_reservations' THEN
            RAISE EXCEPTION 'baseline intent rejects holdout reservations';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER baseline_intent_immutable BEFORE UPDATE OF request_json,payload_sha256 "
        "ON lab.runs FOR EACH ROW EXECUTE FUNCTION lab.guard_baseline_intent_mutation()"
    )
    op.execute(
        "CREATE TRIGGER baseline_intent_experiments BEFORE INSERT ON lab.experiments "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_baseline_intent_mutation()"
    )
    op.execute(
        "CREATE TRIGGER baseline_intent_tasks BEFORE INSERT ON scorer.run_tasks "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_baseline_intent_mutation()"
    )
    op.execute(
        "CREATE TRIGGER baseline_intent_no_holdout BEFORE INSERT ON lab.holdout_reservations "
        "FOR EACH ROW EXECUTE FUNCTION lab.guard_baseline_intent_mutation()"
    )
    op.execute("REVOKE ALL ON FUNCTION lab.guard_baseline_intent_mutation() FROM PUBLIC")
    op.execute(
        r"""
        CREATE FUNCTION lab.register_baseline_operation(p_run_id uuid, p_budget jsonb)
        RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
        DECLARE r lab.runs%ROWTYPE; c lab.baseline_calibrations%ROWTYPE;
                existing lab.baseline_operations%ROWTYPE; n integer; total integer;
        BEGIN
          IF session_user <> 'swapp_lab_director' THEN
            RAISE EXCEPTION 'baseline operation registration requires Director role';
          END IF;
          SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
          IF NOT FOUND OR r.state <> 'running' OR
             r.request_json->>'purpose' IS DISTINCT FROM 'baseline' THEN
            RAISE EXCEPTION 'active baseline intent is unavailable';
          END IF;
          IF jsonb_typeof(p_budget) IS DISTINCT FROM 'object' OR
             jsonb_typeof(p_budget->'reservations') IS DISTINCT FROM 'array' OR
             r.request_json->'budget'->>'experiments' IS DISTINCT FROM '0' OR
             r.request_json->'budget'->>'model_tokens' IS DISTINCT FROM '0' OR
             COALESCE((r.request_json->'budget'->>'wall_seconds')::numeric, -1) < 1 OR
             p_budget->'proposal_count' IS DISTINCT FROM '0'::jsonb OR
             p_budget->'model_tokens' IS DISTINCT FROM '0'::jsonb OR
             p_budget->'reserved_wall_seconds' IS DISTINCT FROM '0'::jsonb OR
             p_budget->'reserved_model_tokens' IS DISTINCT FROM '0'::jsonb OR
             COALESCE((p_budget->>'wall_seconds')::numeric, -1) < 0 OR
             (p_budget->>'wall_seconds')::numeric >
                 (r.request_json->'budget'->>'wall_seconds')::numeric OR
             COALESCE((p_budget->>'elapsed_wall_seconds')::numeric, -1) < 0 OR
             (p_budget->>'elapsed_wall_seconds')::numeric >
                 (r.request_json->'budget'->>'wall_seconds')::numeric OR
             COALESCE((p_budget->>'finalizer_allowance_seconds')::integer, 0) < 1 OR
             (p_budget->>'elapsed_wall_seconds')::numeric +
                 (p_budget->>'finalizer_allowance_seconds')::integer >
                 (r.request_json->'budget'->>'wall_seconds')::numeric OR
             p_budget->'reservations' IS DISTINCT FROM '[]'::jsonb THEN
            RAISE EXCEPTION 'baseline budget receipt is invalid or unreconciled';
          END IF;
          IF r.task_plan_sha256 IS NULL OR r.task_plan_count IS NULL THEN
            RAISE EXCEPTION 'baseline task plan is not sealed';
          END IF;
          SELECT * INTO c FROM lab.baseline_calibrations WHERE run_id=p_run_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'baseline calibration is not registered'; END IF;
          IF EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND kind <> 'baseline') OR
             (SELECT count(*) FROM lab.experiments WHERE run_id=p_run_id AND kind='baseline') <> 3 OR
             (SELECT count(DISTINCT baseline_name) FROM lab.experiments
                WHERE run_id=p_run_id AND kind='baseline') <> 3 OR
             EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id) THEN
            RAISE EXCEPTION 'baseline run contains proposals or holdout work';
          END IF;
          IF EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run_id AND
              (e.status <> 'scored' OR NOT EXISTS (SELECT 1 FROM lab.experiment_records x
                WHERE x.experiment_id=e.experiment_id) OR NOT EXISTS
                (SELECT 1 FROM lab.trajectory_records t WHERE t.experiment_id=e.experiment_id) OR
                (SELECT x.experiment_json->>'llm_input_tokens' FROM lab.experiment_records x
                   WHERE x.experiment_id=e.experiment_id) <> '0' OR
                (SELECT x.experiment_json->>'llm_output_tokens' FROM lab.experiment_records x
                   WHERE x.experiment_id=e.experiment_id) <> '0' OR
                (SELECT t.trajectory_json->>'model_id' FROM lab.trajectory_records t
                   WHERE t.experiment_id=e.experiment_id) <> 'baseline/no-llm.v1')) THEN
            RAISE EXCEPTION 'baseline experiment/trajectory terminal pair is incomplete';
          END IF;
          SELECT count(*) INTO total FROM scorer.run_tasks WHERE run_id=p_run_id;
          SELECT count(DISTINCT task_id) INTO n FROM scorer.run_tasks WHERE run_id=p_run_id;
          IF n <> c.task_count OR total <> n*9 OR total <> r.task_plan_count OR
             EXISTS (SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run_id AND
                (evaluation_kind <> 'baseline' OR seed NOT BETWEEN 0 AND 2)) OR
             EXISTS (SELECT 1 FROM scorer.run_tasks a WHERE a.run_id=p_run_id
                AND NOT EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run_id
                   AND e.kind='baseline' AND e.experiment_id=a.experiment_id)) OR
             EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run_id AND e.kind='baseline'
                AND (SELECT count(*) FROM scorer.run_tasks t WHERE t.run_id=p_run_id
                     AND t.experiment_id=e.experiment_id) <> n*3) OR
             EXISTS (SELECT 1 FROM scorer.run_tasks a WHERE a.run_id=p_run_id AND EXISTS
                (SELECT 1 FROM generate_series(0,2) s(seed) WHERE NOT EXISTS
                  (SELECT 1 FROM scorer.run_tasks b WHERE b.run_id=p_run_id
                   AND b.experiment_id=a.experiment_id AND b.task_id=a.task_id AND b.seed=s.seed))) OR
             EXISTS (SELECT 1 FROM scorer.run_tasks a WHERE a.run_id=p_run_id AND a.seed=0 AND
                EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run_id AND e.kind='baseline'
                  AND NOT EXISTS (SELECT 1 FROM scorer.run_tasks b WHERE b.run_id=p_run_id
                    AND b.experiment_id=e.experiment_id AND b.task_id=a.task_id AND b.seed=0))) THEN
            RAISE EXCEPTION 'baseline task plan is not the complete algorithm x seed x task matrix';
          END IF;
          IF (SELECT count(*) FROM scorer.task_scores WHERE run_id=p_run_id) <> total OR
             EXISTS (SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run_id
                       AND state IN ('queued','running')) THEN
            RAISE EXCEPTION 'baseline scoring is incomplete or has pending work';
          END IF;
          INSERT INTO lab.baseline_operations(run_id,request_sha256,suite_manifest_sha256,
             harness_sha256,image_sha256,calibration_sha256,task_plan_sha256,task_plan_count,
             budget_receipt)
          VALUES(p_run_id,r.payload_sha256,r.request_json->>'suite_manifest_sha256',
             r.request_json->>'harness_sha256',r.request_json->>'image_sha256',
             c.calibration_sha256,r.task_plan_sha256,r.task_plan_count,p_budget)
          ON CONFLICT(run_id) DO NOTHING;
          SELECT * INTO existing FROM lab.baseline_operations WHERE run_id=p_run_id;
          IF existing.request_sha256 <> r.payload_sha256 OR
             existing.calibration_sha256 <> c.calibration_sha256 OR
             existing.task_plan_sha256 <> r.task_plan_sha256 OR
             existing.budget_receipt <> p_budget THEN
             RAISE EXCEPTION 'baseline completion receipt conflicts with immutable prior receipt';
          END IF;
          RETURN jsonb_build_object('run_id',p_run_id,'calibration_sha256',c.calibration_sha256,
             'task_count',c.task_count,'task_plan_sha256',r.task_plan_sha256,
             'task_plan_count',r.task_plan_count);
        END; $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION lab.verify_baseline_operation(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,lab AS $$
        DECLARE r lab.runs%ROWTYPE; o lab.baseline_operations%ROWTYPE;
                c lab.baseline_calibrations%ROWTYPE; total integer; n integer;
        BEGIN
          IF session_user <> 'swapp_lab_scorer' THEN
            RAISE EXCEPTION 'baseline verification requires Scorer role';
          END IF;
          SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id;
          SELECT * INTO o FROM lab.baseline_operations WHERE run_id=p_run_id;
          SELECT * INTO c FROM lab.baseline_calibrations WHERE run_id=p_run_id;
          IF NOT FOUND OR o.run_id IS NULL OR c.run_id IS NULL OR
             r.payload_sha256 <> o.request_sha256 OR
             r.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR
             r.request_json->'budget'->>'experiments' <> '0' OR
             r.request_json->'budget'->>'model_tokens' <> '0' OR
             r.task_plan_sha256 <> o.task_plan_sha256 OR
             r.task_plan_count <> o.task_plan_count OR
             o.calibration_sha256 <> c.calibration_sha256 OR
             o.suite_manifest_sha256 <> r.request_json->>'suite_manifest_sha256' OR
             o.harness_sha256 <> r.request_json->>'harness_sha256' OR
             o.image_sha256 <> r.request_json->>'image_sha256' THEN
             RAISE EXCEPTION 'baseline operation receipt does not match immutable run identity';
          END IF;
          SELECT count(*) INTO total FROM scorer.run_tasks WHERE run_id=p_run_id;
          SELECT count(DISTINCT task_id) INTO n FROM scorer.run_tasks WHERE run_id=p_run_id;
          IF n <> c.task_count OR total <> n*9 OR total <> r.task_plan_count OR
             (SELECT count(*) FROM scorer.task_scores WHERE run_id=p_run_id) <> total OR
             EXISTS (SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run_id AND
                (evaluation_kind <> 'baseline' OR seed NOT BETWEEN 0 AND 2)) OR
             EXISTS (SELECT 1 FROM scorer.run_tasks a WHERE a.run_id=p_run_id AND a.seed=0 AND
                EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=p_run_id AND e.kind='baseline'
                  AND NOT EXISTS (SELECT 1 FROM scorer.run_tasks b WHERE b.run_id=p_run_id
                    AND b.experiment_id=e.experiment_id AND b.task_id=a.task_id AND b.seed=0))) OR
             EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND kind <> 'baseline') OR
             EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id) THEN
             RAISE EXCEPTION 'baseline matrix or exclusion checks failed';
          END IF;
          RETURN jsonb_build_object('request_sha256',o.request_sha256,
             'suite_manifest_sha256',o.suite_manifest_sha256,'harness_sha256',o.harness_sha256,
             'image_sha256',o.image_sha256,'calibration_sha256',o.calibration_sha256,
             'task_plan_sha256',o.task_plan_sha256,'task_plan_count',o.task_plan_count,
             'task_count',n,'score_count',total,'budget',o.budget_receipt,
             'suite_id',c.suite_id,'suite_version',c.suite_version);
        END; $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION lab.prepare_unstarted_baseline_stop(
            p_run_id uuid, p_expected_payload_sha256 text
        ) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE r lab.runs%ROWTYPE; empty_digest text :=
          '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';
        BEGIN
          IF session_user <> 'swapp_lab_scorer' THEN
            RAISE EXCEPTION 'unstarted baseline stop requires Scorer role';
          END IF;
          PERFORM pg_advisory_xact_lock(
            ('x' || substr(encode(sha256(uuid_send(p_run_id)), 'hex'), 1, 16))
              ::bit(64)::bigint
          );
          SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id FOR UPDATE;
          IF NOT FOUND OR r.state <> 'stop_requested' OR r.stop_requested IS DISTINCT FROM TRUE OR
             r.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR
             r.request_json->>'proposal_limit' IS DISTINCT FROM '0' OR
             r.request_json->'budget'->>'experiments' IS DISTINCT FROM '0' OR
             r.request_json->'budget'->>'model_tokens' IS DISTINCT FROM '0' OR
             r.payload_sha256 IS DISTINCT FROM p_expected_payload_sha256 OR
             p_expected_payload_sha256 !~ '^[0-9a-f]{64}$' THEN
            RAISE EXCEPTION 'ownerless stopped baseline identity is invalid';
          END IF;
          IF EXISTS (SELECT 1 FROM lab.director_run_owners WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM scorer.run_tasks WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM scorer.score_jobs WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM scorer.task_scores WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM scorer.task_terminal_outcomes WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM lab.baseline_calibrations WHERE run_id=p_run_id) OR
             EXISTS (SELECT 1 FROM lab.baseline_operations WHERE run_id=p_run_id) THEN
            RAISE EXCEPTION 'unstarted baseline stop already has execution evidence';
          END IF;
          IF (r.task_plan_sha256 IS NOT NULL AND r.task_plan_sha256 <> empty_digest) OR
             (r.task_plan_count IS NOT NULL AND r.task_plan_count <> 0) THEN
            RAISE EXCEPTION 'unstarted baseline stop has a nonempty task plan';
          END IF;
          UPDATE lab.runs SET task_plan_sha256=empty_digest, task_plan_count=0
            WHERE run_id=p_run_id AND task_plan_sha256 IS NULL;
          RETURN jsonb_build_object('run_id',p_run_id,'task_plan_sha256',empty_digest,
            'task_plan_count',0,'state','stop_requested');
        END; $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION lab.next_unstarted_baseline_stop()
        RETURNS uuid LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab,scorer AS $$
        DECLARE candidate uuid;
        BEGIN
          IF session_user <> 'swapp_lab_planner' THEN
            RAISE EXCEPTION 'unstarted baseline selector requires Planner role';
          END IF;
          SELECT r.run_id INTO candidate FROM lab.runs r
           WHERE r.state='stop_requested' AND r.stop_requested IS TRUE
             AND r.request_json->>'purpose'='baseline'
             AND r.request_json->>'proposal_limit'='0'
             AND r.request_json->'budget'->>'experiments'='0'
             AND r.request_json->'budget'->>'model_tokens'='0'
             AND (r.task_plan_sha256 IS NULL OR
                  (r.task_plan_sha256='4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'
                   AND r.task_plan_count=0))
             AND NOT EXISTS (SELECT 1 FROM lab.director_run_owners o WHERE o.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM lab.experiments e WHERE e.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM scorer.run_tasks t WHERE t.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM scorer.score_jobs j WHERE j.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM scorer.task_scores s WHERE s.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM scorer.task_terminal_outcomes o WHERE o.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM lab.holdout_reservations h WHERE h.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM lab.baseline_calibrations c WHERE c.run_id=r.run_id)
             AND NOT EXISTS (SELECT 1 FROM lab.baseline_operations b WHERE b.run_id=r.run_id)
           ORDER BY r.updated_at,r.run_id LIMIT 1;
          RETURN candidate;
        END; $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION lab.baseline_terminal_document_receipts(p_run_id uuid)
        RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,lab AS $$
        DECLARE r lab.runs%ROWTYPE; receipts jsonb;
        BEGIN
          IF session_user <> 'swapp_lab_scorer' THEN
            RAISE EXCEPTION 'baseline document verification requires Scorer role';
          END IF;
          SELECT * INTO r FROM lab.runs WHERE run_id=p_run_id;
          IF NOT FOUND OR r.request_json->>'purpose' IS DISTINCT FROM 'baseline' OR
             EXISTS (SELECT 1 FROM lab.experiments WHERE run_id=p_run_id AND kind <> 'baseline') OR
             EXISTS (SELECT 1 FROM lab.holdout_reservations WHERE run_id=p_run_id) THEN
            RAISE EXCEPTION 'run is not an isolated baseline operation';
          END IF;
          IF EXISTS (
              SELECT 1 FROM lab.experiments e
              LEFT JOIN lab.experiment_records x USING(experiment_id)
              LEFT JOIN lab.trajectory_records t USING(experiment_id)
              WHERE e.run_id=p_run_id AND (x.experiment_id IS NULL OR t.experiment_id IS NULL OR
                x.experiment_json->>'kind' IS DISTINCT FROM 'baseline' OR
                x.experiment_json->>'baseline_name' IS DISTINCT FROM e.baseline_name OR
                x.experiment_json->>'status' IS DISTINCT FROM e.status OR
                x.experiment_json->>'llm_input_tokens' IS DISTINCT FROM '0' OR
                x.experiment_json->>'llm_output_tokens' IS DISTINCT FROM '0' OR
                t.trajectory_json->>'kind' IS DISTINCT FROM 'baseline' OR
                t.trajectory_json->>'model_id' IS DISTINCT FROM 'baseline/no-llm.v1' OR
                t.trajectory_json->>'tool_calls' IS DISTINCT FROM '0' OR
                t.trajectory_json->>'usage_profile' IS DISTINCT FROM 'noncommercial_research' OR
                t.messages_blob_sha256 !~ '^[0-9a-f]{64}$')
          ) THEN
            RAISE EXCEPTION 'baseline terminal pair does not prove zero model use';
          END IF;
          SELECT COALESCE(jsonb_agg(jsonb_build_object(
              'experiment_id',e.experiment_id,'baseline_name',e.baseline_name,
              'status',e.status,'experiment_sha256',x.experiment_sha256,
              'trajectory_sha256',t.trajectory_sha256) ORDER BY e.sequence),'[]'::jsonb)
            INTO receipts
            FROM lab.experiments e
            JOIN lab.experiment_records x USING(experiment_id)
            JOIN lab.trajectory_records t USING(experiment_id)
           WHERE e.run_id=p_run_id;
          RETURN receipts;
        END; $$
        """
    )
    for signature in (
        "lab.register_baseline_operation(uuid,jsonb)",
        "lab.verify_baseline_operation(uuid)",
        "lab.prepare_unstarted_baseline_stop(uuid,text)",
        "lab.next_unstarted_baseline_stop()",
        "lab.baseline_terminal_document_receipts(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.register_baseline_operation(uuid,jsonb) "
        "TO swapp_lab_director"
    )
    op.execute("GRANT EXECUTE ON FUNCTION lab.verify_baseline_operation(uuid) TO swapp_lab_scorer")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text) "
        "TO swapp_lab_scorer"
    )
    op.execute("GRANT EXECUTE ON FUNCTION lab.next_unstarted_baseline_stop() TO swapp_lab_planner")
    op.execute(
        "GRANT EXECUTE ON FUNCTION lab.baseline_terminal_document_receipts(uuid) "
        "TO swapp_lab_scorer"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER baseline_intent_no_holdout ON lab.holdout_reservations")
    op.execute("DROP TRIGGER baseline_intent_tasks ON scorer.run_tasks")
    op.execute("DROP TRIGGER baseline_intent_experiments ON lab.experiments")
    op.execute("DROP TRIGGER baseline_intent_immutable ON lab.runs")
    op.execute("DROP FUNCTION lab.guard_baseline_intent_mutation()")
    op.execute("DROP FUNCTION lab.verify_baseline_operation(uuid)")
    op.execute("DROP FUNCTION lab.next_unstarted_baseline_stop()")
    op.execute("DROP FUNCTION lab.prepare_unstarted_baseline_stop(uuid,text)")
    op.execute("DROP FUNCTION lab.baseline_terminal_document_receipts(uuid)")
    op.execute("DROP FUNCTION lab.register_baseline_operation(uuid,jsonb)")
    op.drop_table("baseline_operations", schema="lab")
