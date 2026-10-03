"""Exercise client argument routing without SSH, remote startup or a real tunnel."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops/connect-lab.sh"


@pytest.mark.parametrize(
    ("options", "port", "startup"),
    [
        (["--profile", "field-lab"], 8789, " --profile 'field-lab'"),
        (["--profile", "field-lab", "--no-start-lab"], 8789, None),
        ([], 8788, ""),
    ],
)
def test_client_starts_and_forwards_the_selected_installation(
    tmp_path: Path, options: list[str], port: int, startup: str | None
) -> None:
    log = tmp_path / "ssh.jsonl"
    ssh = tmp_path / "ssh"
    ssh.write_text(
        "#!/usr/bin/env python3\nimport os,sys,json\n"
        "with open(os.environ['SCIENTIST_TEST_SSH_LOG'],'a') as f:\n"
        " f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "sys.exit(7 if '-N' in sys.argv else 0)\n"
    )
    ssh.chmod(0o700)
    curl = tmp_path / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n")
    curl.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        local_port = listener.getsockname()[1]
    result = subprocess.run(
        ["bash", str(SCRIPT), "--no-browser", "--local-port", str(local_port), *options],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "SCIENTIST_TEST_SSH_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    # An exited fake tunnel must never be presented as a working connection.
    assert result.returncode != 0
    assert "Lab: http" not in result.stdout
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    if startup is not None:
        assert calls[0][-1] == (
            "cd '/home/cachyos/ai-scientist' && bash ops/start-lab.sh" + startup
        )
        assert len(calls) == 2
    else:
        assert len(calls) == 1
    tunnel = calls[-1]
    assert tunnel[tunnel.index("-L") + 1] == f"127.0.0.1:{local_port}:127.0.0.1:{port}"


@pytest.mark.parametrize("profile", ["field-lab;touch /tmp/unwanted", "Field-Lab", ""])
def test_invalid_profile_is_rejected_before_connecting(profile: str) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--profile", profile, "--no-browser"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 2
    assert "Invalid profile name" in result.stderr
