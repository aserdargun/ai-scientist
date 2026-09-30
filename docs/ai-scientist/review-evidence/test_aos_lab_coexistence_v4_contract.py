"""CPU-only proof tests for the V4 coexistence acceptance predicates."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

DRIVER_PATH = Path(__file__).with_name("review_aos_lab_coexistence_v4.py")
SPEC = importlib.util.spec_from_file_location("aos_lab_coexistence_v4", DRIVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot import V4 CPU contract driver")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def _generation() -> dict[str, object]:
    return {
        "owner": "lab",
        "request_id": "ticket-1",
        "unit": "swapp-lab-gpu-turn-" + "a" * 32 + ".service",
        "invocation_id": "b" * 32,
        "main_pid": 121,
        "main_start_ticks": 9001,
        "boot_id": "boot-1",
        "control_group": "/user.slice/swapp-gpu.slice/swapp-lab-gpu-turn-" + "a" * 32 + ".service",
        "nonce": "nonce-1",
    }


def _snapshot(
    state: str, at: float, *, generation: dict[str, object] | None = None
) -> dict[str, object]:
    bound = generation or _generation()
    turn = {
        "owner": "lab",
        "request_id": "ticket-1",
        "sequence": 4,
        "state": state,
        "submitted_at": 10.5,
    }
    return {
        "boottime": at,
        "boot_id": "boot-1",
        "run_id": "run-1",
        "aos_task_id": "aos-task-1",
        "aos_task_run_id": "aos-run-1",
        "foreground_status": "running",
        "ledger": {"turns": [turn], "bindings": [bound]},
    }


class CleanupTests(unittest.TestCase):
    def test_pass_requires_both_owned_generations_to_be_drained(self) -> None:
        receipts = [
            {"unit": "aos.service", "owner": "aos", "result": "drained"},
            {"unit": "lab.service", "owner": "lab", "result": "drained"},
        ]
        self.assertTrue(DRIVER._cleanup_passed("passed", receipts))
        self.assertFalse(DRIVER._cleanup_passed("passed", receipts[:1]))
        self.assertFalse(
            DRIVER._cleanup_passed(
                "passed", [*receipts[:1], {**receipts[1], "result": "drain_timeout"}]
            )
        )
        self.assertFalse(DRIVER._cleanup_passed("failed", receipts))

    def test_execute_marks_startup_failure_failed_after_draining_owned_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                phase="coexistence",
                aos_unit="swapp-aos-review-test.service",
                aos_source=root,
                lab_root=root,
                suite="fixture-suite",
                desktop_manifest=root / "desktop.json",
                decider_manifest=root / "decider.json",
            )
            for path in (args.desktop_manifest, args.decider_manifest):
                path.write_text("{}", encoding="utf-8")
            suite = {"suite_manifest_sha256": "a" * 64}
            run = DRIVER.CoexistenceRun(args, root / "receipts", suite)
            captured = _generation()
            captured.update({"owner": "aos", "unit": args.aos_unit})

            def launch_aos() -> None:
                run.aos_principal = captured
                run.aos_generation_captured_at = 2.0
                raise TimeoutError("application readiness timed out")

            def stop_aos() -> None:
                run.cleanup_receipts.append(
                    {"owner": "aos", "unit": args.aos_unit, "result": "drained"}
                )
                run.aos_principal = None

            def stop_dispatch() -> None:
                run.cleanup_receipts.append(
                    {"owner": "lab", "unit": "lab-dispatch.service", "result": "drained"}
                )

            with (
                patch.object(run, "_launch_aos", side_effect=launch_aos),
                patch.object(run, "_stop_aos", side_effect=stop_aos),
                patch.object(run, "_stop_dispatch", side_effect=stop_dispatch),
                patch.object(DRIVER, "_sha", return_value="b" * 64),
            ):
                with self.assertRaisesRegex(TimeoutError, "readiness timed out"):
                    run.execute()

            receipt = json.loads((run.base / "driver-observations.json").read_text())
            self.assertEqual(receipt["outcome"], "failed")
            self.assertEqual(receipt["failure_type"], "TimeoutError")
            self.assertTrue(DRIVER._cleanup_passed("passed", run.cleanup_receipts))

    def test_cleanup_failure_overrides_success_and_rejects_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(
                phase="coexistence",
                aos_unit="swapp-aos-review-test.service",
                aos_source=root,
                lab_root=root,
                suite="fixture-suite",
                desktop_manifest=root / "desktop.json",
                decider_manifest=root / "decider.json",
            )
            for path in (args.desktop_manifest, args.decider_manifest):
                path.write_text("{}", encoding="utf-8")
            suite = {"suite_manifest_sha256": "a" * 64}
            run = DRIVER.CoexistenceRun(args, root / "receipts", suite)
            started = {"lab_run_id": "run-1"}

            def stop_aos() -> None:
                run.cleanup_receipts.append(
                    {"owner": "aos", "unit": args.aos_unit, "result": "drain_timeout"}
                )

            def stop_dispatch() -> None:
                run.cleanup_receipts.append(
                    {"owner": "lab", "unit": "lab-dispatch.service", "result": "drained"}
                )

            with (
                patch.object(run, "_launch_aos"),
                patch.object(run, "_start_lab", return_value=started),
                patch.object(run, "_launch_dispatch"),
                patch.object(run, "_wait_coexistence", return_value={"observed": True}),
                patch.object(run, "_make_html_report", return_value={"sha256": "c" * 64}),
                patch.object(run, "_stop_aos", side_effect=stop_aos),
                patch.object(run, "_stop_dispatch", side_effect=stop_dispatch),
                patch.object(DRIVER, "_sha", return_value="b" * 64),
            ):
                with self.assertRaisesRegex(RuntimeError, "cannot pass"):
                    run.execute()

            receipt = json.loads((run.base / "driver-observations.json").read_text())
            self.assertEqual(receipt["outcome"], "failed")
            self.assertEqual(receipt["failure_type"], "owned_unit_cleanup_not_drained")


class ProgressGateTests(unittest.TestCase):
    def test_foreground_admission_requires_calibration_and_live_model_attempt(self) -> None:
        progress = {
            "baseline_scored_count": 3,
            "calibration_sha256": "c" * 64,
            "calibration_blob_sha256": "c" * 64,
            "active_provider_attempt_count": 1,
        }
        self.assertTrue(DRIVER._proposal_foreground_ready(progress, terminal=False))
        self.assertFalse(DRIVER._proposal_foreground_ready(progress, terminal=True))
        self.assertFalse(
            DRIVER._proposal_foreground_ready(
                {**progress, "active_provider_attempt_count": 0}, terminal=False
            )
        )
        self.assertFalse(
            DRIVER._proposal_foreground_ready(
                {**progress, "calibration_blob_sha256": "d" * 64}, terminal=False
            )
        )

    def test_lab_must_remain_running_across_measured_overlap_interval(self) -> None:
        self.assertTrue(
            DRIVER._lab_active_through_interval({"lab_state": "running"}, {"lab_state": "running"})
        )
        self.assertFalse(
            DRIVER._lab_active_through_interval(
                {"lab_state": "completed"}, {"lab_state": "running"}
            )
        )
        self.assertFalse(
            DRIVER._lab_active_through_interval(
                {"lab_state": "running"}, {"lab_state": "completed"}
            )
        )


class TicketTransitionTests(unittest.TestCase):
    def test_only_observed_live_to_done_transition_inside_same_run_counts(self) -> None:
        earlier = _snapshot("active", 11.0)
        later = _snapshot("done", 12.0)
        transitions = DRIVER._ticket_done_transition(
            earlier,
            later,
            owner="lab",
            interval=(10.0, 13.0),
            boot_id="boot-1",
            run_id="run-1",
            require_foreground=True,
        )
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["request_id"], "ticket-1")
        self.assertEqual(transitions[0]["aos_task_run_id"], "aos-run-1")

    def test_historical_done_ticket_does_not_count_as_a_transition(self) -> None:
        earlier = _snapshot("done", 11.0)
        later = _snapshot("done", 12.0)
        self.assertEqual(
            DRIVER._ticket_done_transition(
                earlier,
                later,
                owner="lab",
                interval=(10.0, 13.0),
                boot_id="boot-1",
                run_id="run-1",
            ),
            [],
        )

    def test_rejects_boot_or_run_spanning_observations(self) -> None:
        earlier = _snapshot("active", 11.0)
        later = _snapshot("done", 12.0)
        with self.assertRaisesRegex(ValueError, "boot"):
            DRIVER._ticket_done_transition(
                earlier,
                {**later, "boot_id": "boot-2"},
                owner="lab",
                interval=(10.0, 13.0),
                boot_id="boot-1",
                run_id="run-1",
            )
        with self.assertRaisesRegex(ValueError, "run"):
            DRIVER._ticket_done_transition(
                earlier,
                {**later, "run_id": "run-2"},
                owner="lab",
                interval=(10.0, 13.0),
                boot_id="boot-1",
                run_id="run-1",
            )

    def test_child_generation_change_is_not_a_completion_receipt(self) -> None:
        changed = {**_generation(), "invocation_id": "c" * 32}
        self.assertEqual(
            DRIVER._ticket_done_transition(
                _snapshot("active", 11.0),
                _snapshot("done", 12.0, generation=changed),
                owner="lab",
                interval=(10.0, 13.0),
                boot_id="boot-1",
                run_id="run-1",
            ),
            [],
        )


class StartScopeTests(unittest.TestCase):
    def test_human_owned_typed_start_is_explicitly_not_m0_aos_5_proof(self) -> None:
        self.assertIn("HUMAN-owned typed API", DRIVER.M0_AOS_5_LIMITATION)
        self.assertIn("not proof", DRIVER.M0_AOS_5_LIMITATION)

    def test_systemd_run_timeout_captures_and_drains_created_aos_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "aos"
            (source / "runs").mkdir(parents=True)
            args = SimpleNamespace(
                phase="coexistence",
                aos_unit="swapp-aos-review-test.service",
                aos_source=source,
                aos_python=root / "python",
                desktop_manifest=root / "desktop.json",
                decider_manifest=root / "decider.json",
                model_python=root / "model-python",
                lab_api_url="http://127.0.0.1:9999",
                lab_token_file=root / "token",
                startup_seconds=10,
                lab_root=root,
                suite="fixture-suite",
            )
            for path in (args.desktop_manifest, args.decider_manifest):
                path.write_text("{}", encoding="utf-8")
            run = DRIVER.CoexistenceRun(
                args, root / "receipts", {"suite_manifest_sha256": "a" * 64}
            )
            principal = {
                "unit": args.aos_unit,
                "owner": "aos",
                "main_pid": 123,
                "main_start_ticks": 456,
                "boot_id": "boot-1",
                "invocation_id": "b" * 32,
                "control_group": "/user.slice/swapp-gpu.slice/" + args.aos_unit,
            }

            def stop_dispatch() -> None:
                run.cleanup_receipts.append(
                    {"owner": "lab", "unit": "dispatch.service", "result": "drained"}
                )

            with (
                patch.object(DRIVER, "_show", return_value={"LoadState": "not-found"}),
                patch.object(DRIVER, "_principal_identity", return_value=principal) as identity,
                patch.object(DRIVER, "_boottime", side_effect=[5.0, 7.0]),
                patch.object(DRIVER.time, "monotonic", side_effect=[0.0, 1.0]),
                patch.object(DRIVER.time, "sleep"),
                patch.object(
                    DRIVER.subprocess,
                    "run",
                    side_effect=DRIVER.subprocess.TimeoutExpired("systemd-run", 15),
                ),
                patch.object(
                    DRIVER,
                    "_stop_owned_unit",
                    return_value={"owner": "aos", "unit": args.aos_unit, "result": "drained"},
                ) as stop_owned,
                patch.object(run, "_stop_dispatch", side_effect=stop_dispatch),
                patch.object(DRIVER, "_sha", return_value="c" * 64),
            ):
                with self.assertRaisesRegex(RuntimeError, "after an owned generation"):
                    run.execute()

            self.assertGreaterEqual(identity.call_count, 2)
            self.assertEqual(run.aos_generation_captured_at, 7.0)
            self.assertEqual(stop_owned.call_args.args[2], principal)
            receipt = json.loads((run.base / "driver-observations.json").read_text())
            self.assertEqual(receipt["outcome"], "failed")


if __name__ == "__main__":
    unittest.main()
