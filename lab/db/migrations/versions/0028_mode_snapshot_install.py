"""Scorer-only atomic installation of bounded synthetic operating-mode snapshots."""

from alembic import op

revision = "0028_mode_snapshot_install"
down_revision = "0027_terminal_finalization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(r"""
CREATE FUNCTION scorer.install_mode_snapshot(p_bundle text,p_sha256 text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,scorer AS $$
DECLARE b jsonb; keys text[]; k text; v jsonb; rendered text; canonical text;
 semantics_text text; profile_text text; times_text text; masks_text text;
 n integer; window_size integer; sid text; family text; idx integer;
 current_time_value timestamptz; prior_time_value timestamptz;
 profile scorer.dataset_profiles%ROWTYPE; semantics scorer.dataset_task_semantics%ROWTYPE;
 labels_json jsonb; label_count integer; existing_profile boolean; existing_semantics boolean;
 expected_profile jsonb; expected_semantics jsonb;
BEGIN
 IF session_user IS DISTINCT FROM 'swapp_lab_scorer' THEN
  RAISE EXCEPTION 'synthetic snapshot installation requires Scorer'; END IF;
 IF p_bundle IS NULL OR octet_length(p_bundle) NOT BETWEEN 1 AND 262144 OR
    p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$' OR
    encode(sha256(convert_to(p_bundle,'UTF8')),'hex') IS DISTINCT FROM p_sha256 THEN
  RAISE EXCEPTION 'synthetic snapshot bundle hash or bound is invalid'; END IF;
 b:=p_bundle::jsonb;
 IF jsonb_typeof(b) IS DISTINCT FROM 'object' THEN
  RAISE EXCEPTION 'synthetic snapshot bundle must be an object'; END IF;
 SELECT array_agg(key ORDER BY key COLLATE "C") INTO keys FROM jsonb_object_keys(b) key;
 IF keys IS DISTINCT FROM ARRAY['dataset_id','embargo_seconds','evaluation_times',
  'failure_windows','labels','masked_samples','profile_sha256','sample_count','sampling_s',
  'schema','semantics_sha256','session_id','sliding_window','split_id','task_family','visibility']
 OR b->>'schema' IS DISTINCT FROM 'mode-snapshot-registration.v1'
 OR b->>'dataset_id' IS DISTINCT FROM 'synthetic-operating-modes'
 OR b->>'split_id' IS DISTINCT FROM 'train-window-embargo.v1'
 OR b->>'visibility' IS DISTINCT FROM 'dev'
 OR jsonb_typeof(b->'session_id') IS DISTINCT FROM 'string'
 OR (b->>'session_id') !~ '^[0-9a-f]{64}$'
 OR jsonb_typeof(b->'profile_sha256') IS DISTINCT FROM 'string'
 OR (b->>'profile_sha256') !~ '^[0-9a-f]{64}$'
 OR jsonb_typeof(b->'semantics_sha256') IS DISTINCT FROM 'string'
 OR (b->>'semantics_sha256') !~ '^[0-9a-f]{64}$'
 OR b->>'task_family' IS NULL OR b->>'task_family' NOT IN ('EVT','NRM') THEN
  RAISE EXCEPTION 'synthetic snapshot namespace or fields are invalid'; END IF;
 FOREACH k IN ARRAY ARRAY['sample_count','sliding_window','embargo_seconds','sampling_s'] LOOP
  IF jsonb_typeof(b->k) IS DISTINCT FROM 'number' OR (b->>k) !~ '^[0-9]{1,4}$' THEN
   RAISE EXCEPTION 'synthetic snapshot integer field is invalid'; END IF;
 END LOOP;
 n:=(b->>'sample_count')::integer;window_size:=(b->>'sliding_window')::integer;
 sid:=b->>'session_id';family:=b->>'task_family';
 IF n NOT BETWEEN 1 AND 1024 OR window_size NOT BETWEEN 1 AND n OR
  (b->>'embargo_seconds')::integer IS DISTINCT FROM window_size OR
  (b->>'sampling_s')::integer IS DISTINCT FROM 1 THEN
  RAISE EXCEPTION 'synthetic snapshot sample/window/cadence is invalid'; END IF;
 FOREACH k IN ARRAY ARRAY['evaluation_times','labels','masked_samples','failure_windows'] LOOP
  IF jsonb_typeof(b->k) IS DISTINCT FROM 'array' THEN
   RAISE EXCEPTION 'synthetic snapshot axes must be arrays'; END IF;
 END LOOP;
 IF jsonb_array_length(b->'evaluation_times') IS DISTINCT FROM n OR
  jsonb_array_length(b->'labels') IS DISTINCT FROM n OR
  jsonb_array_length(b->'masked_samples') IS DISTINCT FROM n OR
  b->'failure_windows' IS DISTINCT FROM '[]'::jsonb THEN
  RAISE EXCEPTION 'synthetic snapshot axes are misaligned'; END IF;
 FOR idx IN 0..n-1 LOOP
  IF jsonb_typeof(b->'labels'->idx) IS DISTINCT FROM 'boolean' OR
   jsonb_typeof(b->'masked_samples'->idx) IS DISTINCT FROM 'boolean' OR
   jsonb_typeof(b->'evaluation_times'->idx) IS DISTINCT FROM 'string' OR
   (b->'evaluation_times'->>idx) !~
    '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$' THEN
   RAISE EXCEPTION 'synthetic snapshot axis element type is invalid'; END IF;
  current_time_value:=(b->'evaluation_times'->>idx)::timestamptz;
  IF prior_time_value IS NOT NULL AND
   current_time_value-prior_time_value IS DISTINCT FROM interval '1 second' THEN
   RAISE EXCEPTION 'synthetic snapshot time axis must have exact one-second cadence'; END IF;
  prior_time_value:=current_time_value;
 END LOOP;
 IF (family='NRM' AND b->'labels' @> '[true]'::jsonb) OR
    (family='EVT' AND NOT b->'labels' @> '[true]'::jsonb) THEN
  RAISE EXCEPTION 'synthetic snapshot labels differ from task family'; END IF;
 -- Every permitted value is now an ASCII scalar or a bounded array of scalars.
 -- Reconstruct Python's sorted compact JSON; rejects duplicate keys/whitespace/escapes.
 canonical:='{';
 FOREACH k IN ARRAY keys LOOP
  v:=b->k;
  IF jsonb_typeof(v)='array' THEN
   SELECT '['||coalesce(string_agg(value::text,',' ORDER BY ordinal),'')||']'
    INTO rendered FROM jsonb_array_elements(v) WITH ORDINALITY AS x(value,ordinal);
  ELSE rendered:=v::text; END IF;
  canonical:=canonical||CASE WHEN canonical='{' THEN '' ELSE ',' END||
   to_jsonb(k)::text||':'||rendered;
  IF k='evaluation_times' THEN times_text:=rendered; END IF;
  IF k='masked_samples' THEN masks_text:=rendered; END IF;
 END LOOP;
 canonical:=canonical||'}';
 IF canonical IS DISTINCT FROM p_bundle THEN
  RAISE EXCEPTION 'synthetic snapshot bundle is not canonical'; END IF;
 semantics_text:='{"evaluation_times":'||times_text||',"failure_windows":[],'||
  '"masked_samples":'||masks_text||',"sampling_s":'||1::text||
  ',"schema":"public-task-semantics.v1",'||
  '"task_family":'||to_jsonb(family)::text||'}';
 IF encode(sha256(convert_to(semantics_text,'UTF8')),'hex') IS DISTINCT FROM
   b->>'semantics_sha256' THEN
  RAISE EXCEPTION 'synthetic snapshot semantics hash differs'; END IF;
 profile_text:='{"embargo_samples":'||window_size::text||
  ',"schema":"mode-snapshot-profile.v1","semantics_sha256":'||
  to_jsonb(b->>'semantics_sha256')::text||',"sliding_window":'||window_size::text||
  ',"snapshot_sha256":'||to_jsonb(sid)::text||'}';
 IF encode(sha256(convert_to(profile_text,'UTF8')),'hex') IS DISTINCT FROM
   b->>'profile_sha256' THEN
  RAISE EXCEPTION 'synthetic snapshot profile hash differs'; END IF;
 PERFORM pg_advisory_xact_lock(('x'||substr(encode(sha256(convert_to(
  'mode-snapshot:synthetic-operating-modes:train-window-embargo.v1:'||sid,'UTF8')),
  'hex'),1,16))::bit(64)::bigint);
 expected_profile:=jsonb_build_object('dataset_id','synthetic-operating-modes',
  'split_id','train-window-embargo.v1','session_id',sid,'sample_count',n,
  'sliding_window',window_size,'profile_sha256',b->>'profile_sha256',
  'task_family',family,'visibility','dev');
 expected_semantics:=jsonb_build_object('dataset_id','synthetic-operating-modes',
  'split_id','train-window-embargo.v1','session_id',sid,'task_family',family,'sampling_s',1,
  'evaluation_times_json',b->'evaluation_times','masked_samples_json',b->'masked_samples',
  'failure_windows_json','[]'::jsonb,'semantics_sha256',b->>'semantics_sha256');
 SELECT * INTO profile FROM scorer.dataset_profiles WHERE
  dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' AND session_id=sid;
 existing_profile:=FOUND;
 SELECT * INTO semantics FROM scorer.dataset_task_semantics WHERE
  dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' AND session_id=sid;
 existing_semantics:=FOUND;
 SELECT count(*),jsonb_agg(jsonb_build_array(sample_index,is_anomaly) ORDER BY sample_index)
  INTO label_count,labels_json FROM scorer.dataset_labels WHERE
  dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' AND session_id=sid;
 IF existing_profile OR existing_semantics OR label_count<>0 THEN
  IF NOT existing_profile OR NOT existing_semantics OR label_count<>n OR
   to_jsonb(profile) IS DISTINCT FROM expected_profile OR
   to_jsonb(semantics) IS DISTINCT FROM expected_semantics OR
   labels_json IS DISTINCT FROM (SELECT jsonb_agg(jsonb_build_array(ordinal-1,value)
    ORDER BY ordinal) FROM jsonb_array_elements(b->'labels') WITH ORDINALITY x(value,ordinal)) THEN
   RAISE EXCEPTION 'synthetic snapshot existing registration conflicts'; END IF;
 ELSE
  INSERT INTO scorer.dataset_profiles(dataset_id,split_id,session_id,sample_count,
   sliding_window,profile_sha256,task_family,visibility)
  VALUES('synthetic-operating-modes','train-window-embargo.v1',sid,n,window_size,
   b->>'profile_sha256',family,'dev');
  INSERT INTO scorer.dataset_labels(dataset_id,split_id,session_id,sample_index,is_anomaly)
  SELECT 'synthetic-operating-modes','train-window-embargo.v1',sid,ordinal-1,(value::text)::boolean
  FROM jsonb_array_elements(b->'labels') WITH ORDINALITY x(value,ordinal);
  INSERT INTO scorer.dataset_task_semantics(dataset_id,split_id,session_id,task_family,
   sampling_s,evaluation_times_json,masked_samples_json,failure_windows_json,semantics_sha256)
  VALUES('synthetic-operating-modes','train-window-embargo.v1',sid,family,1,
   b->'evaluation_times',b->'masked_samples','[]'::jsonb,b->>'semantics_sha256');
 END IF;
 -- Read persisted identity so the receipt never reports uncommitted caller-only values.
 SELECT * INTO STRICT profile FROM scorer.dataset_profiles WHERE
  dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' AND session_id=sid;
 SELECT * INTO STRICT semantics FROM scorer.dataset_task_semantics WHERE
  dataset_id='synthetic-operating-modes' AND split_id='train-window-embargo.v1' AND session_id=sid;
 RETURN jsonb_build_object('schema','mode-snapshot-registration-receipt.v1',
  'registration_sha256',p_sha256,'snapshot_sha256',profile.session_id,
  'profile_sha256',profile.profile_sha256,'semantics_sha256',semantics.semantics_sha256,
  'sample_count',profile.sample_count,'sliding_window',profile.sliding_window);
END $$;
REVOKE ALL ON FUNCTION scorer.install_mode_snapshot(text,text) FROM PUBLIC,
 swapp_lab_director,swapp_lab_planner;
GRANT EXECUTE ON FUNCTION scorer.install_mode_snapshot(text,text) TO swapp_lab_scorer;
""")


def downgrade() -> None:
    op.execute("DROP FUNCTION scorer.install_mode_snapshot(text,text)")
