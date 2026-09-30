"""Docker runner boundary checks; live tests need the pinned local image."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from harness.contracts import FitContext
from lab.sandbox.docker_runner import (
    DEFAULT_SANDBOX_IMAGE,
    LocalDockerRunner,
    SandboxProfile,
    SandboxRunError,
)
from lab.sandbox.evaluation import check_causality, check_determinism, run_candidate_fit_score

IMAGE_ID = DEFAULT_SANDBOX_IMAGE


def _live_runner(tmp_path: Path, **kwargs: object) -> LocalDockerRunner:
    work_root = Path.cwd() / "data/sandbox-live-tests" / tmp_path.name
    return LocalDockerRunner(image=IMAGE_ID, work_root=work_root, **kwargs)


def test_sandbox_image_and_resource_limits_are_pinned() -> None:
    with pytest.raises(ValueError, match="pinned"):
        LocalDockerRunner(image="python:3.12", work_root=Path("/tmp/runner"))
    with pytest.raises(ValueError, match="4 GiB"):
        SandboxProfile(memory_bytes=5 * 1024**3)


@pytest.mark.live
def test_docker_phase_is_nonroot_without_gpu_or_writable_root(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = b"""import glob,json,os,sys\nfrom pathlib import Path\nrootfs_write=False\ntry: Path("/opt/sandbox-write-test").write_text("bad"); rootfs_write=True\nexcept OSError: pass\nPath("/output/probe.json").write_text(json.dumps({"uid":os.getuid(),"rootfs_write":rootfs_write,"nvidia_devices":glob.glob("/dev/nvidia*"),"input":sys.stdin.buffer.read().decode()}))\n"""  # noqa: E501
    result = runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"fixture-arrow")
    assert result.exit_code == 0
    observed = json.loads(result.artifacts[0].content)
    assert observed == {
        "uid": 10001,
        "rootfs_write": False,
        "nvidia_devices": [],
        "input": "fixture-arrow",
    }


@pytest.mark.live
def test_arrow_ipc_input_is_readable_without_label_columns(tmp_path: Path) -> None:
    table = pa.table({"sensor_a": [1.0, 2.5], "sensor_b": [3.0, 4.5]})
    buffer = BytesIO()
    with ipc.new_stream(buffer, table.schema) as writer:
        writer.write_table(table)
    source = b"""import json,sys\nfrom pathlib import Path\nimport pyarrow.ipc as ipc\ntable=ipc.open_stream(sys.stdin.buffer).read_all()\nPath("/output/observed.json").write_text(json.dumps({"columns":table.column_names,"rows":table.to_pylist()}))\n"""  # noqa: E501
    result = _live_runner(tmp_path).run_phase(
        phase="fit", candidate_source=source, arrow_input=buffer.getvalue()
    )
    observed = json.loads(result.artifacts[0].content)
    assert observed == {
        "columns": ["sensor_a", "sensor_b"],
        "rows": [{"sensor_a": 1.0, "sensor_b": 3.0}, {"sensor_a": 2.5, "sensor_b": 4.5}],
    }


@pytest.mark.live
def test_candidate_cannot_inspect_host_process_or_memory(tmp_path: Path) -> None:
    host_pid = os.getpid()
    source = f"""import json,os\nfrom pathlib import Path\nhost_pid={host_pid}\nvisible=Path(f"/proc/{{host_pid}}").exists()\ntry:\n open(f"/proc/{{host_pid}}/mem","rb").read(1); memory_read=True\nexcept OSError:\n memory_read=False\nPath("/output/isolation.json").write_text(json.dumps({{"host_pid_visible":visible,"host_memory_read":memory_read}}))\n""".encode()  # noqa: E501
    result = _live_runner(tmp_path).run_phase(phase="fit", candidate_source=source, arrow_input=b"")
    observed = json.loads(result.artifacts[0].content)
    assert observed == {"host_pid_visible": False, "host_memory_read": False}


@pytest.mark.live
def test_p1_admission_prevents_two_concurrent_sandboxes(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = (
        b"import time\n"
        b"time.sleep(1.5)\n"
        b"from pathlib import Path\n"
        b'Path("/output/done").write_text("ok")\n'
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            runner.run_phase,
            phase="fit",
            candidate_source=source,
            arrow_input=b"",
        )
        deadline = time.monotonic() + 8
        active = False
        while time.monotonic() < deadline:
            names = subprocess.run(
                ["docker", "ps", "--filter", "name=swapp-lab-fit-", "--format", "{{.Names}}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if names:
                active = True
                break
            time.sleep(0.05)
        assert active, "first candidate container did not become active"
        with pytest.raises(SandboxRunError, match="capacity busy"):
            _live_runner(tmp_path / "second").run_phase(
                phase="fit", candidate_source=b"pass\n", arrow_input=b""
            )
        result = first.result(timeout=10)
        assert result.exit_code == 0


@pytest.mark.live
def test_fit_and_score_use_separate_containers_and_ro_artifact(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = b"""import json,sys\nfrom pathlib import Path\nphase=sys.argv[1]\nif phase == "fit": Path("/output/model.bin").write_bytes(sys.stdin.buffer.read())\nelse:\n artifact=Path("/fit-artifact/model.bin")\n try: artifact.write_bytes(b"tampered"); writable=True\n except OSError: writable=False\n Path("/output/result.json").write_text(json.dumps({"model":artifact.read_bytes().decode(),"phase":phase,"artifact_writable":writable}))\n"""  # noqa: E501
    fit = runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"opaque-model")
    model = next(item.content for item in fit.artifacts if item.name == "model.bin")
    scored = runner.run_phase(
        phase="score",
        candidate_source=source,
        arrow_input=b"arrow-eval-only",
        fit_artifact=model,
    )
    assert fit.container_name != scored.container_name
    result = json.loads(scored.artifacts[0].content)
    assert result == {
        "model": "opaque-model",
        "phase": "score",
        "artifact_writable": False,
    }


@pytest.mark.live
def test_candidate_symlink_output_is_rejected(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = b"""from pathlib import Path\nPath("/output/escape").symlink_to("/etc/passwd")\n"""
    with pytest.raises(SandboxRunError, match="candidate phase failed"):
        runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"x")


@pytest.mark.live
def test_candidate_logs_are_discarded_and_output_tmpfs_is_bounded(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path, profile=SandboxProfile(output_bytes=16 * 1024))
    source = b"""import os\nfrom pathlib import Path\nos.write(1,b"x"*1000000)\nPath("/output/large").write_bytes(b"x"*1000000)\n"""  # noqa: E501
    with pytest.raises(SandboxRunError, match="candidate phase failed"):
        runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"")


@pytest.mark.live
def test_candidate_cannot_leave_background_process_to_mutate_output(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = b"""import os,time\nfrom pathlib import Path\npid=os.fork()\nif pid == 0:\n os.setsid(); time.sleep(1); Path("/output/result").write_text("tampered"); os._exit(0)\nPath("/output/result").write_text("safe")\n"""  # noqa: E501
    result = runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"")
    assert result.artifacts[0].content == b"safe"


@pytest.mark.live
def test_candidate_timeout_stops_only_its_container(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path, profile=SandboxProfile(timeout_seconds=1))
    sentinel = f"swapp-sandbox-sentinel-{uuid.uuid4().hex}"
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--pull=never",
            "--name",
            sentinel,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--user=10001:10001",
            "--entrypoint",
            "python",
            IMAGE_ID,
            "-c",
            "import time; time.sleep(30)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    source = b"""import time\ntime.sleep(10)\n"""
    try:
        with pytest.raises(SandboxRunError, match="timed out"):
            runner.run_phase(phase="fit", candidate_source=source, arrow_input=b"")
        state = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", sentinel],
            check=True,
            capture_output=True,
            text=True,
        )
        assert state.stdout.strip() == "true"
        remaining = subprocess.run(
            ["docker", "ps", "--all", "--filter", "name=swapp-lab-fit-", "--format", "{{.Names}}"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert not remaining.stdout.strip()
    finally:
        subprocess.run(["docker", "container", "rm", "--force", sentinel], check=False)


@pytest.mark.live
def test_typed_candidate_fit_score_and_full_instance_guards(tmp_path: Path) -> None:
    runner = _live_runner(tmp_path)
    source = b'''import numpy as np
from harness.contracts import AlarmPolicy, FitContext

class Detector:
    def fit(self, train, ctx):
        self.center = float(train["sensor"].mean())

    def score(self, data):
        return np.abs(data["sensor"].to_numpy(dtype=float) - self.center)

    def alarm_policy(self, train_scores):
        return AlarmPolicy(threshold=1.0, release=0.5, dwell=1)

def build_candidate():
    return Detector()
'''
    train = pd.DataFrame({"sensor": [0.0] * 32})
    evaluation = pd.DataFrame({"sensor": [0.0] * 8 + [2.0] * 8})
    context = FitContext(
        seed=0,
        signals=("sensor",),
        regime_signals=("sensor",),
        sampling_s=60,
        time_budget_s=90.0,
    )
    evaluated = run_candidate_fit_score(
        runner,
        candidate_source=source,
        train=train,
        evaluation=evaluation,
        context=context,
    )
    assert evaluated.fit_container_name != evaluated.score_container_name
    assert evaluated.scores == (0.0,) * 8 + (2.0,) * 8
    assert evaluated.policy.threshold == 1.0
    assert len(evaluated.fit_artifact_sha256) == 64
    assert check_determinism(
        runner,
        candidate_source=source,
        train=train,
        evaluation=evaluation,
        context=context,
    ).passed
    assert check_causality(
        runner,
        candidate_source=source,
        train=train,
        evaluation=evaluation,
        context=context,
        cuts=(0.5, 0.75),
    ).passed
