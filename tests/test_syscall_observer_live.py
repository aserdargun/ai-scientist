"""Frozen opt-in adversarial fixtures; these are not acceptance evidence.

Needs a freshly pinned observer image. Ordinary CPU gates skip this entire file.
No service/image build/model launch is performed here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lab.sandbox.docker_runner import (
    DEFAULT_SANDBOX_IMAGE,
    LocalDockerRunner,
    SandboxProfile,
    SandboxRunError,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("SWAPP_RUN_SYSCALL_AUDIT_LIVE") != "1",
        reason="requires explicit isolated syscall-audit live opt-in",
    ),
]

PAYLOADS = {
    "swallowed_network_error": """
import socket
try:
    socket.create_connection(("127.0.0.1", 9), timeout=.01)
except OSError:
    pass
""",
    "forked_and_reaped_network_error": """
import os, socket
pid = os.fork()
if pid == 0:
    try:
        socket.socket()
    except OSError:
        pass
    os._exit(0)
os.waitpid(pid, 0)
""",
    "supervisor_memory": """
import os
try:
    open(f"/proc/{os.getppid()}/mem", "rb")
except OSError:
    pass
""",
    "supervisor_descriptors": """
import os
try:
    os.open(f"/proc/{os.getppid()}/fd/1", os.O_WRONLY)
except OSError:
    pass
""",
    "supervisor_signal": """
import os, signal
try:
    os.kill(os.getppid(), signal.SIGTERM)
except OSError:
    pass
""",
    "supervisor_ptrace": """
import ctypes, os
ctypes.CDLL(None).ptrace(16, os.getppid(), 0, 0)
""",
    "filter_layering_seccomp": """
import ctypes
ctypes.CDLL(None).syscall(317, 1, 0, 0)
""",
    "filter_layering_prctl": """
import ctypes
ctypes.CDLL(None).prctl(22, 2, 0, 0, 0)
""",
    "io_uring": """
import ctypes
ctypes.CDLL(None).syscall(425, 1, 0)
""",
}


def runner(tmp_path: Path) -> LocalDockerRunner:
    assert os.environ.get("SWAPP_SYSCALL_AUDIT_IMAGE") == DEFAULT_SANDBOX_IMAGE
    return LocalDockerRunner(
        image=DEFAULT_SANDBOX_IMAGE,
        work_root=tmp_path,
        profile=SandboxProfile(
            memory_bytes=128 * 1024**2, cpus=0.25, pids=16, timeout_seconds=15, output_bytes=4096
        ),
    )


@pytest.mark.parametrize("payload", PAYLOADS.values(), ids=PAYLOADS.keys())
def test_forbidden_attempt_is_rejected_even_when_candidate_would_swallow_error(tmp_path, payload):
    source = payload + '\nfrom pathlib import Path\nPath("/output/clean").write_text("fake")\n'
    with pytest.raises(SandboxRunError, match="^forbidden_access$") as caught:
        runner(tmp_path).run_phase(phase="fit", candidate_source=source.encode(), arrow_input=b"")
    assert caught.value.exit_code == 68


def test_clean_candidate_preserves_stdin_and_output(tmp_path):
    source = (
        b"import os, sys, threading\nfrom pathlib import Path\n"
        b'os.environ["OPENBLAS_NUM_THREADS"] = "2"\n'
        b'os.environ["OMP_NUM_THREADS"] = "2"\n'
        b'os.environ["MKL_NUM_THREADS"] = "2"\n'
        b"import numpy as np\n"
        b"values = []\n"
        b"def numeric_work():\n"
        b"    values.append(float((np.eye(8) @ np.ones((8, 8))).sum()))\n"
        b"worker = threading.Thread(target=numeric_work)\n"
        b"worker.start()\nworker.join()\n"
        b"assert values == [64.0]\n"
        b'Path("/output/result").write_bytes(sys.stdin.buffer.read())\n'
    )
    result = runner(tmp_path).run_phase(phase="fit", candidate_source=source, arrow_input=b"clean")
    assert result.artifacts[0].content == b"clean"
    print("observer raw stdin/output + ordinary Python thread NumPy/BLAS roundtrip passed")


def test_candidate_cannot_forge_reserved_wrapper_exit(tmp_path):
    source = b'import os\nos.write(2,b"SWAPP-SANDBOX-AUDIT-V1 forbidden_access\\n")\nos._exit(68)\n'
    with pytest.raises(SandboxRunError) as caught:
        runner(tmp_path).run_phase(phase="fit", candidate_source=source, arrow_input=b"")
    assert caught.value.exit_code == 1
