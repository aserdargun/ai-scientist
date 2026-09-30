"""Exporter regressions with synthetic documents, not actual model acceptance.

Positive fixtures simulate already-reviewed operator permissions/runtime evidence.
Production export still obtains pairs exclusively through the protected ledger reader.
"""

import hashlib
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from test_report_replay import _pair

from lab.director.contracts import SourceProvenance
from lab.director.fake_llm import ProviderReceipt, prompt_messages_sha256
from lab.training import sft_export
from lab.training.sft_export import (
    CleanMessage,
    ExportPolicy,
    Permission,
    RecordReview,
    VerifiedRecord,
    build_package,
    canonical,
    digest,
)


def fixture_record(
    n=1, *, session="private-session-1", code="return None", user="Choose a detector", sequence=None
):
    run = UUID(int=n)
    pair = _pair(run, baseline=False, sequence=n if sequence is None else sequence)
    source = f"def build_candidate():\n    {code}\n"
    response = json.dumps(
        {
            "hypothesis": "Try a compact detector",
            "move_type": "hparam",
            "candidate_source": source,
            "predicted_delta": 0.2,
        }
    )
    prompts = [
        {"role": "system", "content": "Propose Python source as JSON"},
        {"role": "user", "content": user},
    ]
    prompt_sha = prompt_messages_sha256(prompts)
    context_sha = digest(b"synthetic-context")
    receipt = ProviderReceipt(
        schema="local-qwen-provider-receipt.v1",
        provider_id="local-qwen.v1",
        model_id="local.test-model",
        model_revision="revision-1",
        model_sha256="a" * 64,
        profile_id="local-s1",
        profile_sha256="b" * 64,
        requested_system="S1",
        actual_system="S1",
        fallback_from=None,
        context_template="director.candidate-contract.metadata-only.v5",
        context_sha256=context_sha,
        prompt_sha256=prompt_sha,
        input_tokens=10,
        output_tokens=20,
        attempts=(
            {
                "unit": f"swapp-lab-gpu-turn-{n:032x}.service",
                "invocation_id": "c" * 32,
                "control_group": "/user.slice/private-control-group",
                "main_pid": 1234,
                "main_start_ticks": 500,
                "profile_id": "local-s1",
                "attempt_kind": "proposal",
                "prompt_sha256": prompt_sha,
                "response_schema_sha256": "d" * 64,
                "response_sha256": digest(response.encode()),
                "prompt_utf8_bytes": 60,
                "output_token_limit": 1024,
                "outcome": "completed",
                "wall_seconds": 1.0,
                "inference_seconds": 0.5,
                "peak_gpu_memory_mib": 100,
            },
        ),
        attempted_profile_ids=("local-s1",),
        provider_config_sha256="e" * 64,
        provider_registry_entry_sha256="f" * 64,
        sampling_temperature=0.0,
        sampling_top_p=1.0,
        enable_thinking=False,
        thinking_token_budget=None,
    )
    provider = receipt.model_dump(mode="json")
    transcript = {
        "messages": [prompts[0]["content"], prompts[1]["content"], response],
        "provider_attempts": [
            {
                "attempt_kind": "proposal",
                "profile_id": "local-s1",
                "prompt_messages": prompts,
                "prompt_sha256": prompt_sha,
                "response_schema_sha256": "d" * 64,
                "response_sha256": digest(response.encode()),
                "response_text": response,
                "preflight_prompt_tokens": 10,
            }
        ],
    }
    provenance = SourceProvenance(
        dataset_id="private-dataset",
        split_id="development",
        session_id=session,
        source_manifest_sha256="1" * 64,
        source_revision="source-revision",
        license_id="MIT",
        attribution="Synthetic regression fixture",
        access_terms="test only",
        usage_profile="noncommercial_research",
    )
    exp = pair.document.model_copy(
        update={
            "candidate_sha256": digest(source.encode()),
            "candidate_blob_sha256": digest(source.encode()),
            "inputs_sha256": context_sha,
            "hypothesis": "Try a compact detector",
            "predicted_delta": 0.2,
            "provider_receipt_sha256": digest(canonical(provider)),
            "llm_input_tokens": 10,
            "llm_output_tokens": 20,
        }
    )
    trajectory = pair.trajectory.model_copy(
        update={
            "provider_receipt": provider,
            "model_id": "local.test-model",
            "adapter": "vllm-local",
            "source_provenance": (provenance,),
            "inputs_sha256": context_sha,
            "messages_blob_sha256": digest(canonical(transcript)),
        }
    )
    return VerifiedRecord(
        experiment=exp,
        trajectory=trajectory,
        trajectory_sha256=digest(canonical(trajectory.model_dump(mode="json"))),
        transcript=transcript,
    )


def permission(identity):
    return Permission(
        identity_sha256=identity,
        license_id="MIT",
        training_allowed=True,
        usage_profile="noncommercial_research",
        permission_evidence_sha256="2" * 64,
    )


def reviewed_policy(records, *, redactions=(), fraction=0.2):
    reviews = []
    for record in records:
        final = record.transcript["provider_attempts"][-1]
        messages = [
            *final["prompt_messages"],
            {"role": "assistant", "content": final["response_text"]},
        ]
        for value in sorted(redactions, key=len, reverse=True):
            messages = [
                {**m, "content": m["content"].replace(value, "[REDACTED]")} for m in messages
            ]
        reviews.append(
            RecordReview(
                trajectory_sha256=record.trajectory_sha256,
                messages_blob_sha256=record.trajectory.messages_blob_sha256,
                runtime_evidence_sha256="3" * 64,
                cleaning_evidence_sha256="4" * 64,
                origin="real_local_llm",
                secrets_scrubbed=True,
                people_scrubbed=True,
                raw_values_scrubbed=True,
                revoked_or_rolled_back=False,
                redactions=tuple(redactions),
                clean_messages=tuple(CleanMessage(**m) for m in messages),
            )
        )
    return ExportPolicy(
        schema="training-export-policy.v1",
        target="local_noncommercial_sft",
        split_secret="5" * 64,
        eval_fraction=fraction,
        reviews=tuple(reviews),
        data_permissions=(permission("1" * 64),),
        model_permissions=(permission("a" * 64),),
        code_permissions=tuple(
            permission(key) for key in sorted({r.experiment.candidate_sha256 for r in records})
        ),
    )


def manifest(records, policy):
    return json.loads(build_package(tuple(records), policy)["manifest.json"])


def test_reviewed_local_shape_exports_sft_without_mutating_ledger_scrub_flags():
    record = fixture_record()
    policy = reviewed_policy([record])
    files = build_package((record,), policy)
    result = json.loads(files["manifest.json"])
    assert result["included_records"] == 1 and not result["publication_authorized"]
    assert not result["training_executed"]
    assert not record.trajectory.secrets_scrubbed
    chat = json.loads(files["train.jsonl"] or files["eval.jsonl"])
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant"]
    combined = b"".join(files.values())
    for private in [
        str(record.experiment.run_id),
        record.experiment.experiment_id,
        "private-dataset",
        "private-session-1",
        "private-control-group",
        "1234",
    ]:
        assert private.encode() not in combined


@pytest.mark.parametrize(
    "field,reason",
    [
        ("data_permissions", "dataset_training_permission_missing"),
        ("model_permissions", "model_training_permission_missing"),
        ("code_permissions", "code_training_permission_missing"),
        ("reviews", "missing_cleaning_and_runtime_review"),
    ],
)
def test_missing_permission_is_explicit_exclusion(field, reason):
    record = fixture_record()
    policy = reviewed_policy([record]).model_copy(update={field: ()})
    result = manifest([record], policy)
    assert result["included_records"] == 0 and result["exclusion_counts"] == {reason: 1}


@pytest.mark.parametrize("origin", ["fixture", "fake", "unknown"])
def test_fixture_origin_never_exports(origin):
    record = fixture_record()
    policy = reviewed_policy([record])
    policy = policy.model_copy(
        update={"reviews": (policy.reviews[0].model_copy(update={"origin": origin}),)}
    )
    assert manifest([record], policy)["exclusion_counts"] == {"fixture_or_unbound_review": 1}


def test_grid_or_fake_provider_never_exports_with_review():
    record = fixture_record()
    record = record.model_copy(
        update={"trajectory": record.trajectory.model_copy(update={"adapter": "parameter-grid"})}
    )
    assert manifest([record], reviewed_policy([record]))["exclusion_counts"] == {"not_local_llm": 1}


@pytest.mark.parametrize("change", ["runtime", "receipt_hash", "transcript", "revoked", "scrubbed"])
def test_runtime_hash_transcript_and_review_fail_closed(change):
    record = fixture_record()
    policy = reviewed_policy([record])
    if change == "runtime":
        provider = dict(record.trajectory.provider_receipt)
        provider["attempts"] = [{**provider["attempts"][0], "unit": None}]
        record = record.model_copy(
            update={
                "trajectory": record.trajectory.model_copy(update={"provider_receipt": provider}),
                "experiment": record.experiment.model_copy(
                    update={"provider_receipt_sha256": digest(canonical(provider))}
                ),
            }
        )
    elif change == "receipt_hash":
        record = record.model_copy(
            update={
                "experiment": record.experiment.model_copy(
                    update={"provider_receipt_sha256": "0" * 64}
                )
            }
        )
    elif change == "transcript":
        record = record.model_copy(update={"transcript": {"provider_attempts": []}})
    else:
        key = "revoked_or_rolled_back" if change == "revoked" else "raw_values_scrubbed"
        value = change == "revoked"
        policy = policy.model_copy(
            update={"reviews": (policy.reviews[0].model_copy(update={key: value}),)}
        )
    assert manifest([record], policy)["included_records"] == 0


def test_clean_messages_cannot_invent_a_replacement_response():
    record = fixture_record()
    policy = reviewed_policy([record])
    messages = list(policy.reviews[0].clean_messages)
    messages[1] = CleanMessage(role="user", content="Invented training context")
    policy = policy.model_copy(
        update={
            "reviews": (policy.reviews[0].model_copy(update={"clean_messages": tuple(messages)}),)
        }
    )
    assert manifest([record], policy)["exclusion_counts"] == {
        "clean_messages_not_observed_redaction": 1
    }


def test_reviewed_redactions_remove_person_and_secret_from_observed_turn():
    private = "person@example.org"
    record = fixture_record(user=f"Choose a detector for {private}")
    assert manifest([record], reviewed_policy([record]))["exclusion_counts"] == {
        "unclean_export_content": 1
    }
    files = build_package((record,), reviewed_policy([record], redactions=(private,)))
    assert json.loads(files["manifest.json"])["included_records"] == 1
    assert private.encode() not in b"".join(files.values())


def test_protected_split_is_not_relabelled_for_training():
    record = fixture_record()
    source = record.trajectory.source_provenance[0].model_copy(update={"split_id": "holdout"})
    record = record.model_copy(
        update={"trajectory": record.trajectory.model_copy(update={"source_provenance": (source,)})}
    )
    assert manifest([record], reviewed_policy([record]))["exclusion_counts"] == {
        "protected_split": 1
    }


def test_session_connected_runs_stay_in_one_split():
    records = [fixture_record(1, code="return None"), fixture_record(2, code="return 2")]
    result = manifest(records, reviewed_policy(records, fraction=0.5))
    assert result["included_records"] == 2
    assert len({row["group_id"] for row in result["records"]}) == 1
    assert len({row["split"] for row in result["records"]}) == 1


def test_normalized_code_dedup_connects_other_examples_before_split():
    records = [
        fixture_record(1, session="session-one"),
        fixture_record(2, session="session-two"),
        fixture_record(2, session="session-two", code="return 7", sequence=3),
    ]
    result = manifest(records, reviewed_policy(records, fraction=0.5))
    assert result["included_records"] == 2
    assert result["exclusion_counts"] == {"duplicate_normalized_candidate": 1}
    assert len({row["split"] for row in result["records"]}) == 1


def test_duplicate_policy_identity_is_not_silently_overwritten():
    record = fixture_record()
    policy = reviewed_policy([record])
    policy = policy.model_copy(update={"data_permissions": policy.data_permissions * 2})
    with pytest.raises(ValueError, match="duplicate policy"):
        build_package((record,), policy)


def test_repeat_export_is_byte_identical_and_files_hash_correct():
    record = fixture_record()
    policy = reviewed_policy([record])
    files = build_package((record,), policy)
    assert files == build_package((record,), policy)
    for name, entry in json.loads(files["manifest.json"])["files"].items():
        assert hashlib.sha256(files[name]).hexdigest() == entry["sha256"]


def test_historical_baseline_message_array_is_excluded_without_inventing_context():
    record = fixture_record()
    policy = reviewed_policy([record])
    record = record.model_copy(
        update={
            "experiment": record.experiment.model_copy(update={"kind": "baseline"}),
            "transcript": ["observed historical baseline message"],
        }
    )
    assert manifest([record], policy)["exclusion_counts"] == {"not_proposal": 1}


def test_local_model_message_array_cannot_supply_an_observed_provider_turn():
    record = fixture_record()
    policy = reviewed_policy([record])
    record = record.model_copy(update={"transcript": ["historical message array"]})
    assert manifest([record], policy)["exclusion_counts"] == {
        "unsupported_local_transcript_envelope": 1
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "different-profile"),
        ("attempt_kind", "repair"),
        ("response_schema_sha256", "0" * 64),
    ],
)
def test_final_transcript_runtime_identity_must_match(field, value):
    record = fixture_record()
    transcript = {
        **record.transcript,
        "provider_attempts": [{**record.transcript["provider_attempts"][0], field: value}],
    }
    record = record.model_copy(
        update={
            "transcript": transcript,
            "trajectory": record.trajectory.model_copy(
                update={"messages_blob_sha256": digest(canonical(transcript))}
            ),
        }
    )
    assert manifest([record], reviewed_policy([record]))["exclusion_counts"] == {
        "transcript_provider_hash_mismatch": 1
    }


def test_complete_source_stays_together_across_sessions_and_manifest_revisions():
    records = [
        fixture_record(1, session="source-earlier", code="return 1"),
        fixture_record(2, session="source-later", code="return 2"),
    ]
    second = records[1]
    source = second.trajectory.source_provenance[0].model_copy(
        update={"source_manifest_sha256": "6" * 64, "source_revision": "later-source-revision"}
    )
    records[1] = second.model_copy(
        update={
            "experiment": second.experiment.model_copy(
                update={"parent_experiment_id": "exp_" + "7" * 32}
            ),
            "trajectory": second.trajectory.model_copy(update={"source_provenance": (source,)}),
        }
    )
    policy = reviewed_policy(records)
    policy = policy.model_copy(
        update={"data_permissions": (permission("1" * 64), permission("6" * 64))}
    )
    result = manifest(records, policy)
    assert result["included_records"] == 2
    assert len({row["group_id"] for row in result["records"]}) == 1


def test_experiment_family_stays_together_across_distinct_sources():
    records = [fixture_record(1, code="return 1"), fixture_record(2, code="return 2")]
    second = records[1]
    source = second.trajectory.source_provenance[0].model_copy(
        update={"dataset_id": "different-source", "source_manifest_sha256": "6" * 64}
    )
    records[1] = second.model_copy(
        update={"trajectory": second.trajectory.model_copy(update={"source_provenance": (source,)})}
    )
    policy = reviewed_policy(records)
    policy = policy.model_copy(
        update={"data_permissions": (permission("1" * 64), permission("6" * 64))}
    )
    result = manifest(records, policy)
    assert result["included_records"] == 2
    assert len({row["group_id"] for row in result["records"]}) == 1


def evidence_policy(record):
    """Synthetic hash-bound attestations test the reader, never real LLM origin."""
    policy = reviewed_policy([record])
    blobs = {}

    def store(value):
        payload = canonical(value)
        key = digest(payload)
        blobs[key] = payload
        return key

    for field, kind in [
        ("data_permissions", "dataset_permission"),
        ("model_permissions", "model_permission"),
        ("code_permissions", "code_permission"),
    ]:
        grants = []
        for grant in getattr(policy, field):
            key = store(
                {
                    "schema": "training-reviewed-evidence.v1",
                    "kind": kind,
                    "subject_sha256": grant.identity_sha256,
                    "usage_profile": "noncommercial_research",
                    "license_id": grant.license_id,
                    "training_allowed": True,
                }
            )
            grants.append(grant.model_copy(update={"permission_evidence_sha256": key}))
        policy = policy.model_copy(update={field: tuple(grants)})
    review = policy.reviews[0]
    observer = store({"synthetic_fixture_only": True})
    runtime = store(
        {
            "schema": "training-reviewed-evidence.v1",
            "kind": "runtime",
            "subject_sha256": record.trajectory_sha256,
            "usage_profile": "noncommercial_research",
            "origin": "real_local_llm",
            "provider_receipt_sha256": record.experiment.provider_receipt_sha256,
            "observer_receipt_sha256": observer,
        }
    )
    cleaning = store(
        {
            "schema": "training-reviewed-evidence.v1",
            "kind": "cleaning",
            "subject_sha256": record.trajectory_sha256,
            "usage_profile": "noncommercial_research",
            "secrets_scrubbed": True,
            "people_scrubbed": True,
            "raw_values_scrubbed": True,
            "clean_messages_sha256": digest(
                canonical([m.model_dump() for m in review.clean_messages])
            ),
            "redactions_sha256": digest(canonical(list(review.redactions))),
        }
    )
    policy = policy.model_copy(
        update={
            "reviews": (
                review.model_copy(
                    update={
                        "runtime_evidence_sha256": runtime,
                        "cleaning_evidence_sha256": cleaning,
                    }
                ),
            )
        }
    )
    return policy, blobs


def test_permission_and_cleaning_proofs_are_read_and_hash_bound(monkeypatch, tmp_path):
    record = fixture_record()
    policy, blobs = evidence_policy(record)
    monkeypatch.setattr(sft_export, "read_director_artifact", lambda key, **_: blobs[key])
    sft_export.validate_review_evidence((record,), policy, tmp_path)
    files = build_package((record,), policy)
    result = json.loads(files["manifest.json"])
    row = result["records"][0]
    assert row["candidate_sha256"] == record.experiment.candidate_sha256
    assert (
        row["model_permission_evidence_sha256"]
        == policy.model_permissions[0].permission_evidence_sha256
    )
    assert (
        row["code_permission_evidence_sha256"]
        == policy.code_permissions[0].permission_evidence_sha256
    )
    card = json.loads(files["data-card.json"])
    assert card["usage_profile"] == "noncommercial_research" and card["licenses"] == ["MIT"]
    assert not card["training_executed"] and not card["publication_authorized"]


@pytest.mark.parametrize("kind", ["runtime", "cleaning", "dataset_permission", "observer"])
def test_missing_or_replaced_review_bytes_fail_closed(monkeypatch, tmp_path, kind):
    record = fixture_record()
    policy, blobs = evidence_policy(record)
    if kind == "observer":
        key = json.loads(blobs[policy.reviews[0].runtime_evidence_sha256])[
            "observer_receipt_sha256"
        ]
    else:
        key = next(key for key, payload in blobs.items() if json.loads(payload).get("kind") == kind)
    blobs[key] = b"{}"
    monkeypatch.setattr(sft_export, "read_director_artifact", lambda key, **_: blobs[key])
    with pytest.raises(ValueError):
        sft_export.validate_review_evidence((record,), policy, tmp_path)


def test_package_quota_includes_manifest_and_data_card(monkeypatch):
    record = fixture_record()
    policy = reviewed_policy([record])
    files = build_package((record,), policy)
    monkeypatch.setattr(
        sft_export, "MAX_PACKAGE_BYTES", len(files["train.jsonl"]) + len(files["eval.jsonl"])
    )
    with pytest.raises(ValueError, match="byte quota"):
        build_package((record,), policy)


def test_cli_failure_is_stable_and_does_not_print_private_inputs(monkeypatch, capsys, tmp_path):
    def private_input(_):
        raise ValueError("private postgres password and identity")

    monkeypatch.setattr(sft_export, "_private_file", private_input)
    code = sft_export.main(
        [
            "--dsn-file",
            str(tmp_path / "dsn"),
            "--policy-file",
            str(tmp_path / "policy"),
            "--artifact-root",
            str(tmp_path),
            "--review-root",
            str(tmp_path),
            "--output",
            str(tmp_path / "output"),
            "--run-id",
            str(UUID(int=1)),
        ]
    )
    assert code == 1
    assert capsys.readouterr().err == "training export failed closed\n"


def test_private_input_file_rejects_shared_permissions(tmp_path):
    path = tmp_path / "policy"
    path.write_bytes(b"private fixture")
    path.chmod(0o600)
    assert sft_export._private_file(path) == b"private fixture"
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(ValueError, match="owner-only"):
        sft_export._private_file(path)


def export_fixture(monkeypatch, tmp_path):
    record = fixture_record()
    policy, blobs = evidence_policy(record)
    blobs[record.trajectory.messages_blob_sha256] = canonical(record.transcript)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = {
        "trajectory_sha256": record.trajectory_sha256
    }
    monkeypatch.setattr(
        sft_export,
        "read_run_pairs",
        lambda *_, **__: (
            SimpleNamespace(document=record.experiment, trajectory=record.trajectory),
        ),
    )
    monkeypatch.setattr(sft_export, "read_director_artifact", lambda key, **_: blobs[key])
    return (
        engine,
        (record.experiment.run_id,),
        {
            "artifact_root": tmp_path,
            "policy": policy,
            "review_root": tmp_path,
            "output": tmp_path / "package",
        },
    )


def test_ledger_adapter_writes_only_fresh_private_package(monkeypatch, tmp_path):
    engine, run_ids, options = export_fixture(monkeypatch, tmp_path)
    files = sft_export.export_runs(engine, run_ids, **options)
    output = options["output"]
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    for name, payload in files.items():
        assert (output / name).read_bytes() == payload
        assert stat.S_IMODE((output / name).stat().st_mode) == 0o600
    execute = engine.connect.return_value.__enter__.return_value.execute
    assert all(str(call.args[0]).startswith("SELECT ") for call in execute.call_args_list)
    with pytest.raises(ValueError, match="fresh absolute"):
        sft_export.export_runs(engine, run_ids, **options)
    assert (output / "manifest.json").read_bytes() == files["manifest.json"]


def test_package_write_failure_removes_only_created_files(monkeypatch, tmp_path):
    engine, run_ids, options = export_fixture(monkeypatch, tmp_path)
    existing = tmp_path / "unrelated"
    existing.write_bytes(b"retain me")
    real_open = sft_export.os.open

    def fail_manifest(path, *args, **kwargs):
        if path.name == "manifest.json":
            raise OSError("fixture write failure")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(sft_export.os, "open", fail_manifest)
    with pytest.raises(OSError, match="fixture write failure"):
        sft_export.export_runs(engine, run_ids, **options)
    assert not options["output"].exists()
    assert existing.read_bytes() == b"retain me"


def test_explicit_per_run_layout_reads_two_separate_roots(monkeypatch, tmp_path):
    records = [fixture_record(1, code="return 1"), fixture_record(2, code="return 2")]
    policies_and_blobs = [evidence_policy(record) for record in records]
    policy = policies_and_blobs[0][0].model_copy(
        update={
            "reviews": tuple(policy.reviews[0] for policy, _ in policies_and_blobs),
            "code_permissions": tuple(
                policy.code_permissions[0] for policy, _ in policies_and_blobs
            ),
        }
    )
    blobs = {key: payload for _, bundle in policies_and_blobs for key, payload in bundle.items()}
    for record in records:
        blobs[record.trajectory.messages_blob_sha256] = canonical(record.transcript)
    observed_roots = []

    def pairs(_, run_id, *, artifact_root):
        assert artifact_root == tmp_path / str(run_id)
        observed_roots.append(artifact_root)
        record = next(r for r in records if r.experiment.run_id == run_id)
        return (SimpleNamespace(document=record.experiment, trajectory=record.trajectory),)

    monkeypatch.setattr(sft_export, "read_run_pairs", pairs)

    def artifact(key, *, artifact_root):
        expected = next((r for r in records if r.trajectory.messages_blob_sha256 == key), None)
        if expected is not None:
            assert artifact_root == tmp_path / str(expected.experiment.run_id)
        return blobs[key]

    monkeypatch.setattr(sft_export, "read_director_artifact", artifact)
    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.side_effect = [
        {"trajectory_sha256": r.trajectory_sha256} for r in records
    ]
    files = sft_export.export_runs(
        engine,
        tuple(r.experiment.run_id for r in records),
        artifact_root=tmp_path,
        artifact_layout="per-run",
        policy=policy,
        review_root=tmp_path / "reviews",
        output=tmp_path / "package",
    )
    assert json.loads(files["manifest.json"])["included_records"] == 2
    assert len(observed_roots) == 2


def test_unknown_artifact_layout_fails_before_ledger_access(monkeypatch, tmp_path):
    engine, run_ids, options = export_fixture(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="unsupported artifact layout"):
        sft_export.export_runs(engine, run_ids, artifact_layout="unknown", **options)
    engine.connect.assert_not_called()
