from __future__ import annotations

import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

DRIVER_PATH = Path(__file__).with_name("review_aos_lab_coexistence_v3.py")
SPEC = importlib.util.spec_from_file_location("aos_lab_coexistence_v3", DRIVER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot import V3 CPU contract driver")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class SystemdInspectionTests(unittest.TestCase):
    def test_nonzero_systemctl_result_is_not_treated_as_missing_unit(self) -> None:
        failure = subprocess.CompletedProcess([], 1, "", "systemd unavailable")
        with patch.object(DRIVER.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "identity query failed"):
                DRIVER._show("swapp-aos-gpu-review-027abc00000000000000000000000001.service")

    def test_real_loadstate_not_found_response_is_absent(self) -> None:
        success = subprocess.CompletedProcess(
            [], 0, "LoadState=not-found\nActiveState=inactive\n", ""
        )
        with patch.object(DRIVER.subprocess, "run", return_value=success):
            result = DRIVER._show("swapp-aos-gpu-review-027abc00000000000000000000000001.service")
        self.assertEqual(result["LoadState"], "not-found")
        self.assertEqual(result["ActiveState"], "inactive")

    def test_malformed_unit_is_rejected_before_systemctl(self) -> None:
        with patch.object(DRIVER.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "invalid systemd unit"):
                DRIVER._show("../foreign.service")
        run.assert_not_called()


class InterpreterPinTests(unittest.TestCase):
    def test_clone_python_must_follow_its_fixed_shared_venv_link(self) -> None:
        lab_root = Path(
            "/home/cachyos/ai-scientist/data/runtime/parallel-m0/public-execution-027"
        )
        DRIVER._pinned_lab_interpreter(lab_root, lab_root / ".venv/bin/python")
        with self.assertRaisesRegex(ValueError, "must be this clone"):
            DRIVER._pinned_lab_interpreter(lab_root, Path("/usr/bin/python3"))

    def test_external_venv_pins_are_exact_and_read_only_targets(self) -> None:
        DRIVER._pinned_external_interpreter(
            Path("/home/cachyos/ai-scientist/data/runtime/aos-coexistence/.venv/bin/python"),
            Path("/home/cachyos/ai-scientist/data/runtime/aos-coexistence/.venv/bin/python"),
            "AOS",
        )
        DRIVER._pinned_external_interpreter(
            Path("/home/cachyos/.venv/bin/python"),
            Path("/home/cachyos/.venv/bin/python"),
            "model",
        )
        with self.assertRaisesRegex(ValueError, "differs from the reviewed pin"):
            DRIVER._pinned_external_interpreter(
                Path("/usr/bin/python3"), Path("/home/cachyos/.venv/bin/python"), "model"
            )


class ForegroundAdmissionTests(unittest.TestCase):
    def test_baseline_does_not_consume_foreground_model_tasks(self) -> None:
        self.assertFalse(DRIVER._proposal_foreground_ready({"proposal_count": 0}, terminal=False))

    def test_foreground_turns_begin_after_first_proposal_and_stop_at_terminal(self) -> None:
        progress = {"proposal_count": 1}
        self.assertTrue(DRIVER._proposal_foreground_ready(progress, terminal=False))
        self.assertFalse(DRIVER._proposal_foreground_ready(progress, terminal=True))


if __name__ == "__main__":
    unittest.main()
