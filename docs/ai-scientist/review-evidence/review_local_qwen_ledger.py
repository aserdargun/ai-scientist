"""Read-only verification of actual Qwen proposals, blobs and Scorer decisions."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import hashlib
import json
from uuid import UUID

from sqlalchemy import text

from review_director20 import verify_scored_decisions
from review_scorer_queue import ROOT, engine


def verify_attempt_transcripts(*, check, prefix: str, context, receipt,
                              envelope: dict) -> tuple[list[dict], str]:
    """Bind every complete raw final response to its own exact model request.

    Truncated attempts have usage receipts but no complete final response. A repair
    must retain the earlier invalid final response; it cannot replace that evidence.
    """
    from lab.director.fake_llm import ProviderAttemptTranscript
    from lab.director.journal import canonical_bytes
    from lab.director.local_llm import LocalQwenProposalProvider, provider_profile_config
    from lab.llm.native_runtime import MODEL_TURN_PROFILES

    def digest(value):
        return hashlib.sha256(canonical_bytes(value)).hexdigest()

    attempts = [ProviderAttemptTranscript.model_validate_json(json.dumps(item), strict=True)
                for item in envelope.get("provider_attempts", [])]
    complete = [item for item in receipt.attempts if item.outcome == "completed"]
    check(prefix + "_all_complete_attempts_retained", bool(complete)
          and len(attempts) == len(complete))
    check(prefix + "_profile_sequence", tuple(item.profile_id for item in receipt.attempts)
          == receipt.attempted_profile_ids)
    check(prefix + "_at_most_one_repair",
          sum(item.attempt_kind == "repair" for item in receipt.attempts) <= 1)
    check(prefix + "_unique_model_request_units", len({item.unit for item in receipt.attempts})
          == len(receipt.attempts))
    schema_sha = provider_profile_config()["output_schema_sha256"]
    previous_messages, previous_raw = None, None
    final_messages, final_raw = None, None
    for index, (transcript, runtime) in enumerate(zip(attempts, complete, strict=True)):
        label = f"{prefix}_complete_attempt_{index}"
        messages = [item.model_dump(mode="json") for item in transcript.prompt_messages]
        check(label + "_prompt_and_response_hashes",
              digest({"messages": messages}) == transcript.prompt_sha256 == runtime.prompt_sha256
              and len(canonical_bytes({"messages": messages})) == runtime.prompt_utf8_bytes
              and hashlib.sha256(transcript.response_text.encode()).hexdigest()
              == transcript.response_sha256 == runtime.response_sha256)
        check(label + "_schema_profile_kind", transcript.response_schema_sha256
              == runtime.response_schema_sha256 == schema_sha
              and transcript.profile_id == runtime.profile_id
              and transcript.attempt_kind == runtime.attempt_kind
              and transcript.preflight_prompt_tokens == runtime.preflight_prompt_tokens)
        if transcript.attempt_kind == "proposal":
            variants = LocalQwenProposalProvider._message_variants(
                context, MODEL_TURN_PROFILES[runtime.profile_id])
            check(label + "_metadata_projection", any(list(value) == messages for value in variants))
        else:
            check(label + "_repair_preserves_prior_input", previous_messages is not None
                  and len(messages) == len(previous_messages) == 2
                  and messages[0] == previous_messages[0]
                  and messages[1]["role"] == "user"
                  and messages[1]["content"].startswith(previous_messages[1]["content"])
                  and previous_raw[:65_536] in messages[1]["content"])
        previous_messages, previous_raw = messages, transcript.response_text
        final_messages, final_raw = messages, transcript.response_text
    check(prefix + "_final_transcript_binding", final_messages is not None
          and receipt.attempts[-1].outcome == "completed"
          and receipt.prompt_sha256 == attempts[-1].prompt_sha256
          and envelope["messages"] == [*(item["content"] for item in final_messages), final_raw])
    for index, runtime in enumerate(receipt.attempts):
        if runtime.outcome != "completed":
            profile = MODEL_TURN_PROFILES[runtime.profile_id]
            variants = LocalQwenProposalProvider._message_variants(context, profile)
            check(f"{prefix}_nonfinal_attempt_{index}_projection",
                  runtime.attempt_kind == "proposal"
                  and any(digest({"messages": list(value)}) == runtime.prompt_sha256
                          for value in variants))
        check(f"{prefix}_attempt_{index}_schema", runtime.response_schema_sha256 == schema_sha)
    return final_messages, final_raw


def verify_run(run_id: str, *, observed_models: dict[str, dict], report: dict,
               owner_id: str, request: dict) -> dict:
    from lab.director.artifacts import read_director_artifact, read_registered_calibration
    from lab.director.contracts import CandidateProposal, ExperimentDocument, TrajectoryDocument
    from lab.director.fake_llm import AgentContext, ProviderReceipt, prompt_context_sha256
    from lab.director.journal import canonical_bytes
    from lab.director.local_llm import provider_config_sha256
    from lab.llm.native_runtime import MODEL_REPOSITORY, MODEL_REVISION, MODEL_TURN_PROFILES, ModelPin

    identity = UUID(run_id)
    blobs = ROOT / "data/runtime/director-artifacts" / run_id
    director = engine("director")
    checks, documents, counts = {}, [], Counter()

    def check(name: str, condition: bool):
        checks[name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    try:
        with director.connect() as connection:
            row = dict(connection.execute(text("SELECT * FROM lab.runs WHERE run_id=:run"),
                                           {"run": identity}).mappings().one())
            check("actual_local_run_owner", row["origin"] == "local" and row["owner_id"] == owner_id)
            check("trusted_real_provider_request", row["request_json"]["provider"] == "local-qwen"
                  and row["request_json"]["budget"] == request["budget"])
            experiments = connection.execute(text(
                "SELECT * FROM lab.experiments WHERE run_id=:run ORDER BY sequence"
            ), {"run": identity}).mappings().all()
            for experiment in experiments:
                commit = connection.execute(text("SELECT lab.experiment_record_receipt(:id)"),
                    {"id": experiment["experiment_id"]}).scalar_one()
                pair = {}
                for name, model in (("experiment", ExperimentDocument), ("trajectory", TrajectoryDocument)):
                    payload = read_director_artifact(commit[f"{name}_blob_sha256"], artifact_root=blobs)
                    check(f"{experiment['experiment_id']}_{name}_hash",
                          hashlib.sha256(payload).hexdigest() == commit[f"{name}_sha256"])
                    document = model.model_validate_json(payload, strict=True)
                    check(f"{experiment['experiment_id']}_{name}_identity",
                          document.run_id == identity and document.experiment_id == experiment["experiment_id"])
                    pair[name] = document.model_dump(mode="json", by_alias=True)
                proposal, trajectory = pair["experiment"], pair["trajectory"]
                source = read_director_artifact(proposal["candidate_blob_sha256"], artifact_root=blobs)
                check(f"{experiment['experiment_id']}_candidate_hash",
                      hashlib.sha256(source).hexdigest() == proposal["candidate_sha256"])
                documents.append(pair)
                if proposal["kind"] != "proposal":
                    continue
                counts["proposals"] += 1
                receipt = ProviderReceipt.model_validate_json(json.dumps(trajectory["provider_receipt"]), strict=True)
                check(f"{experiment['experiment_id']}_receipt_hash",
                      hashlib.sha256(canonical_bytes(trajectory["provider_receipt"])).hexdigest()
                      == proposal["provider_receipt_sha256"])
                check(f"{experiment['experiment_id']}_real_model_identity",
                      receipt.model_id == MODEL_REPOSITORY and receipt.model_revision == MODEL_REVISION
                      and receipt.model_sha256 == ModelPin.from_repository().digest
                      and trajectory["model_id"] == MODEL_REPOSITORY
                      and receipt.provider_config_sha256 == provider_config_sha256()
                      == row["request_json"]["provider_config_sha256"]
                      and receipt.provider_registry_entry_sha256
                      == row["request_json"]["provider_registry_entry_sha256"])
                profile = MODEL_TURN_PROFILES[receipt.profile_id]
                check(f"{experiment['experiment_id']}_profile_binding",
                      receipt.profile_sha256 == hashlib.sha256(canonical_bytes(asdict(profile))).hexdigest()
                      and receipt.actual_system == profile.system
                      and receipt.enable_thinking == profile.enable_thinking
                      and receipt.sampling_temperature == profile.temperature
                      and receipt.sampling_top_p == profile.top_p
                      and receipt.thinking_token_budget == profile.thinking_token_budget)
                context_payload = read_director_artifact(trajectory["inputs_sha256"], artifact_root=blobs)
                context = AgentContext.model_validate_json(context_payload, strict=True)
                check(f"{experiment['experiment_id']}_context_binding",
                      hashlib.sha256(context_payload).hexdigest() == trajectory["inputs_sha256"]
                      and receipt.context_sha256 == prompt_context_sha256(context))
                transcript_payload = read_director_artifact(trajectory["messages_blob_sha256"], artifact_root=blobs)
                envelope = json.loads(transcript_payload)
                expected, final_raw = verify_attempt_transcripts(
                    check=check, prefix=experiment['experiment_id'], context=context,
                    receipt=receipt, envelope=envelope)
                expected_prompt = canonical_bytes({"messages": list(expected)})
                check(f"{experiment['experiment_id']}_rendered_prompt_binding",
                      receipt.prompt_sha256 == hashlib.sha256(expected_prompt).hexdigest()
                      == receipt.attempts[-1].prompt_sha256)
                parsed = CandidateProposal.model_validate_json(final_raw, strict=True)
                check(f"{experiment['experiment_id']}_exact_model_proposal",
                      parsed.candidate_source.encode() == source
                      and parsed.move_type == context.move_type == proposal["move_type"]
                      and parsed.hypothesis == proposal["hypothesis"]
                      and parsed.predicted_delta == proposal["predicted_delta"])
                check(f"{experiment['experiment_id']}_attempt_count",
                      len(receipt.attempts) == len(receipt.attempted_profile_ids))
                for attempt in receipt.attempts:
                    attempt_profile = MODEL_TURN_PROFILES[attempt.profile_id]
                    check(f"{experiment['experiment_id']}_attempt_token_admission_{attempt.unit}",
                          attempt.preflight_prompt_tokens == attempt.prompt_tokens
                          and attempt.preflight_prompt_tokens <= attempt_profile.max_context_tokens
                          and attempt.preflight_prompt_tokens + attempt.output_token_limit
                          <= attempt_profile.model_max_len)
                    check(f"{experiment['experiment_id']}_attempt_output_cap_{attempt.unit}",
                          0 <= attempt.completion_tokens <= attempt.output_token_limit
                          <= attempt_profile.max_output_tokens)
                    observed = observed_models[attempt.unit]
                    process = observed["identity"]
                    peak_gpu = attempt.peak_gpu_memory_mib or observed["binding"].get("peak_gpu_memory_mib", 0)
                    check(f"{experiment['experiment_id']}_process_{attempt.unit}",
                          process is not None and attempt.invocation_id == process[0]
                          and attempt.main_pid == process[1] and attempt.main_start_ticks == process[3]
                          and "0::" + attempt.control_group == process[4]
                          and peak_gpu > 0)
                check(f"{experiment['experiment_id']}_token_parity",
                      proposal["llm_input_tokens"] == receipt.input_tokens
                      and proposal["llm_output_tokens"] == receipt.output_tokens
                      and all(a.prompt_tokens is not None and a.completion_tokens is not None for a in receipt.attempts)
                      and sum(a.prompt_tokens for a in receipt.attempts) == receipt.input_tokens
                      and sum(a.completion_tokens for a in receipt.attempts) == receipt.output_tokens)
                check(f"{experiment['experiment_id']}_episode_output_cap",
                      receipt.output_tokens <= (2048 if receipt.requested_system == "S1" else 8192))
                check(f"{experiment['experiment_id']}_decision_parity",
                      trajectory["outcome"] == proposal["decision"])
                counts[receipt.actual_system] += 1
                counts["fallbacks"] += int(receipt.fallback_from is not None)
                counts["input_tokens"] += receipt.input_tokens
                counts["output_tokens"] += receipt.output_tokens
            check("six_actual_proposals", counts["proposals"] >= 6)
            check("at_least_two_actual_S1", counts["S1"] >= 2)
            check("at_least_two_actual_S2", counts["S2"] >= 2)
            scores = connection.execute(text(
                "SELECT * FROM lab.dev_task_results WHERE run_id=:run ORDER BY experiment_id,evaluation_kind,seed,task_id"
            ), {"run": identity}).mappings().all()
            calibration = read_registered_calibration(director, run_id=identity, artifact_root=blobs)
            trace = verify_scored_decisions(documents, scores, calibration, blobs)
            check("at_least_one_scored_proposal_reconstructed", len(trace) >= 1)
            check("sealed_report_matches_scorer_ledger",
                  row["task_plan_count"] == len(scores) == len(report["report"]["task_scores"])
                  and row["task_plan_sha256"] is not None
                  and hashlib.sha256(canonical_bytes(report["report"])).hexdigest()
                  == row["report_sha256"] == report["report_sha256"])
        return {"checks": checks, "counts": dict(counts), "documents": documents,
                "decision_trace": trace, "score_count": len(scores)}
    finally:
        director.dispose()
