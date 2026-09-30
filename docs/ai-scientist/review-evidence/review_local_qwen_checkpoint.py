"""Exercise local-provider preregistration and recovery with real PG checkpoints.

Model runtime, token counts and final ledger insertion are explicit fixtures. The production
provider, Director boundary, owned PG leases/checkpoints and physical blobs run.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, insert, select

from lab.db.schema import runs
from lab.director.artifacts import read_director_artifact
from lab.director.journal import DirectorRunLease, canonical_bytes
from lab.director.local_llm import LocalQwenProposalProvider
from lab.director.runner import register_proposal_before_execution
from review_scorer_queue import ROOT, engine


def source_hashes():
    paths = [ROOT / f"lab/director/{name}.py" for name in (
        "runner", "journal", "fake_llm", "local_llm", "artifacts", "ledger")]
    paths.extend((ROOT / "lab/llm/native_runtime.py",
                  ROOT / "tests/test_local_qwen_provider.py", Path(__file__)))
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("refusing to overwrite evidence")
    fixture = runpy.run_path(str(ROOT / "tests/test_local_qwen_provider.py"))
    runtime = fixture["_FakeRuntime"]
    migrator, director = engine("migrator"), engine("director")
    run_id = uuid4()
    record = {
        "schema": "local-qwen-checkpoint-review.v1",
        "checked_at": datetime.now(UTC).isoformat(), "run_id": str(run_id),
        "scope": "Production local provider and preregistration, actual owned PostgreSQL "
                 "leases/checkpoints and physical blobs. Runtime and token counts are explicitly mocked; "
                 "final register_experiment is a fault-injection spy. No model, GPU, "
                 "Docker, Scorer, full SQL calibration admission or AOS acceptance.",
        "source_sha256": source_hashes(), "checks": {}, "scenarios": [],
    }

    def check(name, value):
        record["checks"][name] = bool(value)
        if not value:
            raise AssertionError(name)

    try:
        with migrator.begin() as connection:
            connection.execute(insert(runs).values(
                run_id=run_id, origin="local", owner_id="local-qwen-checkpoint-review",
                idempotency_key=f"local-qwen-checkpoint-{run_id}",
                payload_sha256=hashlib.sha256(str(run_id).encode()).hexdigest(),
                request_json={"review": "local Qwen checkpoint with fixture runtime"},
                state="running",
            ))
        with tempfile.TemporaryDirectory(prefix="local-qwen-checkpoint-", dir=ROOT / "data/runtime") as temporary:
            artifact_root = Path(temporary)
            for ordinal, (system, move, fallback) in enumerate(
                (("S1", "hparam", False), ("S2", "features", True)), start=1
            ):
                label = f"{system}{'_fallback' if fallback else ''}"
                runtime.calls = []
                runtime.pins = []
                runtime.truncate_first = fallback
                runtime.response_text = fixture["_proposal_json"](move)
                provider = LocalQwenProposalProvider(
                    run_id=run_id, owner="lab", principal_resolver=object(),
                    runtime_database=artifact_root / "unused-fixture-runtime.sqlite3",
                    registry_entry_sha256="c" * 64,
                )
                context = fixture["_context"](system=system, move_type=move).model_copy(
                    update={"experiment_number": ordinal})
                captured = []

                def ledger_boundary(*_args, **kwargs):
                    captured.append(kwargs)
                    if len(captured) == 1:
                        raise RuntimeError("injected-ledger-boundary-failure")
                    return {"status": "proposed"}

                arguments = dict(
                    run_id=run_id, ordinal=ordinal, parent_experiment_id="exp_" + "b" * 32,
                    parent_tree_sha256="c" * 40, suite_id="synthetic.local-qwen-checkpoint.v1",
                    suite_version=1, calibration_sha256="d" * 64,
                    harness_sha256="e" * 64, image_sha256="f" * 64,
                    system=system, context=context, provider=provider,
                    artifact_root=artifact_root, remaining_wall_seconds=720.0 if fallback else 180.0,
                )
                with patch("lab.director.local_llm.OwnedVllmRuntime", runtime), \
                     patch.object(LocalQwenProposalProvider, "_count_prompt_variants",
                         lambda _self, variants, selected_profile, **_kwargs:
                         tuple(90 if selected_profile.system == "S2" else 100 for _ in variants)), \
                     patch("lab.director.runner.register_experiment", ledger_boundary):
                    with DirectorRunLease(director, run_id) as first:
                        try:
                            register_proposal_before_execution(director, lease=first, **arguments)
                        except RuntimeError as error:
                            check(f"{label}_reached_ledger_fault", str(error) == "injected-ledger-boundary-failure")
                        else:
                            check(f"{label}_reached_ledger_fault", False)
                        check(f"{label}_durable_checkpoint", first.read_checkpoint(
                            key=f"proposal:{ordinal}", artifact_root=artifact_root) is not None)
                    with DirectorRunLease(director, run_id) as replacement:
                        resumed = register_proposal_before_execution(director, lease=replacement, **arguments)
                        repeated = register_proposal_before_execution(director, lease=replacement, **arguments)
                        check(f"{label}_exact_resume", resumed == repeated)
                        check(f"{label}_no_duplicate_model_call", len(runtime.calls) == (2 if fallback else 1))
                        check(f"{label}_same_immutable_ledger_arguments",
                              len(captured) == 3 and captured[0] == captured[1] == captured[2])
                        receipt = resumed.provider_receipt
                        check(f"{label}_receipt_survives_checkpoint", receipt is not None
                              and receipt.requested_system == system and receipt.actual_system == "S1"
                              and receipt.fallback_from == ("S2" if fallback else None)
                              and receipt.model_sha256 == provider.model_pin.digest
                              and all(pin is provider.model_pin for pin in runtime.pins))
                        transcript = json.loads(read_director_artifact(
                            resumed.messages_blob_sha256, artifact_root=artifact_root))["messages"]
                        request_messages = [{"role": "system", "content": transcript[0]},
                                            {"role": "user", "content": transcript[1]}]
                        check(f"{label}_prompt_transcript_binding", hashlib.sha256(
                            canonical_bytes({"messages": request_messages})).hexdigest() == receipt.prompt_sha256)
                        check(f"{label}_token_totals_preserved",
                              resumed.input_tokens == receipt.input_tokens == (190 if fallback else 100)
                              and resumed.output_tokens == receipt.output_tokens == (2098 if fallback else 50))
                        check(f"{label}_receipt_hash_bound_to_ledger", hashlib.sha256(
                            canonical_bytes(receipt.model_dump(mode="json"))).hexdigest()
                              == captured[-1]["proposal"]["provider_receipt_sha256"])
                        for name, attribute, changed in (
                            ("configuration", "configuration_sha256", "8" * 64),
                            ("registry_entry", "registry_entry_sha256", "9" * 64),
                        ):
                            original = getattr(provider, attribute)
                            setattr(provider, attribute, changed)
                            try:
                                register_proposal_before_execution(director, lease=replacement, **arguments)
                            except RuntimeError as error:
                                check(f"{label}_changed_{name}_rejected", "identity changed" in str(error)
                                      and len(captured) == 3)
                            else:
                                check(f"{label}_changed_{name}_rejected", False)
                            finally:
                                setattr(provider, attribute, original)
                        record["scenarios"].append({"requested_system": system,
                            "receipt": receipt.model_dump(mode="json"), "registration_count": len(captured)})
    except Exception as error:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        with migrator.begin() as connection:
            connection.execute(delete(runs).where(runs.c.run_id == run_id))
            record["checks"]["owned_run_removed"] = connection.execute(
                select(runs.c.run_id).where(runs.c.run_id == run_id)).first() is None
        migrator.dispose()
        director.dispose()
        record["source_after_sha256"] = source_hashes()
        record["checks"]["source_unchanged"] = record["source_sha256"] == record["source_after_sha256"]
        code = 0 if not record.get("error") and len(record["checks"]) == 24 and all(record["checks"].values()) else 1
        record["overall_exit_code"] = code
        with args.output.open("x") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        print(json.dumps({"output": str(args.output), "checks": record["checks"],
                          "error": record.get("error"), "overall_exit_code": code}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
