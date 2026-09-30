"""Measure user-systemd IP filtering and private network namespace capability.

Only owned loopback/Unix sockets and bounded CPU units are used. No external
network destination, model, CUDA call or host interface change is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import uuid

ROOT = Path(__file__).resolve().parents[3]
PYTHON = ROOT / ".venv/bin/python"


def connect_tcp(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def measured_unit(name: str, properties: list[str], command: list[str]) -> dict:
    argv = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
        f"--unit={name}", "--service-type=exec", "--property=MemoryMax=128M",
        "--property=MemorySwapMax=0", "--property=CPUQuota=25%", "--property=TasksMax=16",
        "--property=RuntimeMaxSec=15", "--property=TimeoutStopSec=2",
        "--property=KillMode=control-group", "--property=NoNewPrivileges=yes",
        *properties, *command,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    state = subprocess.run([
        "systemctl", "--user", "show", "--property=LoadState", "--property=ActiveState",
        "--property=MainPID", name,
    ], capture_output=True, text=True, timeout=3, check=False)
    return {
        "command": argv, "exit_code": result.returncode, "stdout": result.stdout,
        "stderr": result.stderr, "terminal_state_exit_code": state.returncode,
        "terminal_state": dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line),
    }


def main(output: Path) -> int:
    if output.exists():
        raise RuntimeError("refusing to overwrite network capability evidence")
    task_id = uuid.uuid4().hex
    runtime = Path(f"/run/user/{os.getuid()}") / f"swapp-net-review-{task_id}"
    runtime.mkdir(mode=0o700)
    unix_path = runtime / "probe.sock"
    stop = threading.Event()
    listeners = []
    threads = []
    record = {
        "schema": "native-network-capability-review.v1",
        "scope": "Host capability only; own loopback/Unix sockets, two short CPU units; no model/CUDA or runtime acceptance.",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": {},
    }

    def serve(server: socket.socket) -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                client, _ = server.accept()
            except TimeoutError:
                continue
            with client:
                client.settimeout(1)
                try:
                    client.sendall(b"owned-probe")
                except OSError:
                    pass

    try:
        tcp = socket.socket()
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(8)
        listeners.append(tcp)
        unix = socket.socket(socket.AF_UNIX)
        unix.bind(str(unix_path))
        unix.listen(8)
        os.chmod(unix_path, 0o600)
        listeners.append(unix)
        for listener in listeners:
            thread = threading.Thread(target=serve, args=(listener,), daemon=True)
            thread.start()
            threads.append(thread)
        port = tcp.getsockname()[1]
        record["checks"]["owned_host_loopback_reachable_before"] = connect_tcp(port)
        parent_namespace = os.readlink("/proc/self/ns/net")
        deny_program = (
            "import json,socket; s=socket.socket(); s.settimeout(1); "
            f"code=s.connect_ex(('127.0.0.1',{port})); "
            "print(json.dumps({'connect_errno':code,'blocked':code!=0})); s.close()"
        )
        deny = measured_unit(
            f"swapp-review-network-deny-{task_id}.service",
            ["--property=IPAddressDeny=any"],
            [str(PYTHON), "-c", deny_program],
        )
        record["systemd_ip_filter"] = deny
        deny_observation = json.loads(deny["stdout"]) if deny["exit_code"] == 0 else {}
        record["ip_filter_effective"] = deny_observation.get("blocked") is True

        namespace_program = f'''
import json,os,socket,subprocess
before={parent_namespace!r}
current=os.readlink('/proc/self/ns/net')
if current==before: raise RuntimeError('refusing interface change in host namespace')
subprocess.run(['/usr/bin/ip','link','set','dev','lo','up'],check=True,timeout=3)
with socket.socket() as probe:
    probe.settimeout(1)
    host_errno=probe.connect_ex(('127.0.0.1',{port}))
with socket.socket() as server:
    server.bind(('127.0.0.1',0)); server.listen(1)
    with socket.socket() as local:
        local.settimeout(1); local.connect(server.getsockname())
        accepted,_=server.accept(); accepted.close()
        own_loopback=True
with socket.socket(socket.AF_UNIX) as pipe:
    pipe.settimeout(1); pipe.connect({str(unix_path)!r})
    uds_ok=pipe.recv(64)==b'owned-probe'
routes=open('/proc/net/route').read().splitlines()[1:]
default_route=any(line.split()[1]=='00000000' for line in routes)
print(json.dumps({{'namespace':current,'namespace_differs':current!=before,'host_tcp_errno':host_errno,'host_tcp_blocked':host_errno!=0,'private_loopback_works':own_loopback,'private_unix_socket_works':uds_ok,'no_default_route':not default_route}}))
'''
        private = measured_unit(
            f"swapp-review-network-namespace-{task_id}.service", [],
            ["/usr/bin/unshare", "--user", "--map-root-user", "--net", str(PYTHON), "-c", namespace_program],
        )
        record["private_network_namespace"] = private
        observations = json.loads(private["stdout"]) if private["exit_code"] == 0 else {}
        record["checks"]["private_network_namespace_available"] = private["exit_code"] == 0
        for name in ("namespace_differs", "host_tcp_blocked", "private_loopback_works", "private_unix_socket_works", "no_default_route"):
            record["checks"][name] = observations.get(name) is True
        record["checks"]["owned_host_loopback_reachable_after"] = connect_tcp(port)
        record["checks"]["host_namespace_unchanged"] = os.readlink("/proc/self/ns/net") == parent_namespace
        record["checks"]["owned_units_terminal"] = all(
            item["terminal_state_exit_code"] == 0 and item["terminal_state"].get("MainPID") == "0"
            and item["terminal_state"].get("ActiveState") in {"inactive", "failed"}
            for item in (deny, private)
        )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        for listener in listeners:
            listener.close()
        if unix_path.exists():
            unix_path.unlink()
        runtime.rmdir()
    record["private_namespace_checks_passed"] = all(record["checks"].values())
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "ip_filter_effective": record["ip_filter_effective"], "checks": record["checks"], "private_namespace_checks_passed": record["private_namespace_checks_passed"]}))
    return 0 if record["private_namespace_checks_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.output))
