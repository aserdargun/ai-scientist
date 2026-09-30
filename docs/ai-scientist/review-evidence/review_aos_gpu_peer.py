"""Check actual AOS UDS peer authentication using a bounded CPU fixture service.

The socket/process/systemd identities are real. Inference and runtime-generation
fields returned by the fixture are fake. No scheduler, AOS model or GPU runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
UNIT = "swapp-lab-gpu-broker.service"
SOURCE = ROOT / "data/runtime/aos-coexistence/source-gpu/src/aos/gpu_turn.py"
PYTHON = ROOT / "data/runtime/aos-coexistence/.venv/bin/python"

SERVER = r'''
import json,os,socket,sys
from pathlib import Path
root=Path(sys.argv[1]);os.umask(0o077)
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
 listener.bind(str(root/'broker.sock'));listener.listen(1);listener.settimeout(15)
 peer,_=listener.accept()
 with peer:
  peer.settimeout(5);data=bytearray()
  while b'\n' not in data:
   data.extend(peer.recv(4096))
   if len(data)>131072: raise ValueError('fixture request bound exceeded')
  request=json.loads(bytes(data).split(b'\n',1)[0])
  response={'version':1,'request_id':request['request_id'],'profile_id':request['profile_id'],
   'deployment_digest':request['deployment_digest'],
   'generation':{'unit':'swapp-aos-gpu-turn-'+('1'*32)+'.service','invocation_id':'2'*32,
                 'main_pid':123,'control_group':'/explicit-fixture-not-model'},
   'response':{'cpu_fixture':True},'usage':{'cpu_fixture':True}}
  peer.sendall((json.dumps(response,separators=(',',':'))+'\n').encode())
 print(json.dumps({'server_pid':os.getpid(),'request_fields':sorted(request)}),flush=True)
'''

CLIENT = r'''
import asyncio,json,os,sys
from pathlib import Path
from aos.contracts import AOSFault
from aos.gpu_turn import AOSGpuTurnClient
directory=Path(sys.argv[1]);pid=int(sys.argv[2]);checks={}
client=AOSGpuTurnClient(directory/'broker.sock',timeout_seconds=8)
for name,peer_pid,peer_uid in [('foreign_pid',os.getpid(),os.getuid()),('foreign_uid',pid,os.getuid()+1)]:
 try: client._verify_broker_peer(peer_pid,peer_uid)
 except AOSFault: checks[name+'_rejected']=True
 else: checks[name+'_rejected']=False
result=asyncio.run(client.infer('aos.decider.turn.v1','a'*64,{'request':{'cpu_fixture':True}}))
checks['actual_authenticated_roundtrip']=result.response=={'cpu_fixture':True}
(directory/'client-result.json').write_text(json.dumps({'checks':checks,'scope':'CPU fixture only'}))
print(json.dumps(checks));raise SystemExit(0 if all(checks.values()) else 1)
'''


def show() -> dict[str, str]:
    result = subprocess.run([
        "/usr/bin/systemctl", "--user", "show", UNIT, "--no-pager",
        "--property=LoadState,ActiveState,MainPID,InvocationID,Description,ControlGroup",
    ], capture_output=True, text=True, timeout=3, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def run(output: Path) -> int:
    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    existing = show()
    if existing.get("LoadState") != "not-found" or existing.get("MainPID") != "0":
        raise RuntimeError("broker name already belongs to another service; left untouched")
    os.umask(0o077)
    identity = uuid4().hex
    directory = ROOT / "data/runtime/aos-gpu-peer-review" / identity
    directory.mkdir(mode=0o700, parents=True)
    server, client = directory / "server.py", directory / "client.py"
    server.write_text(SERVER);client.write_text(CLIENT)
    description = "SWAPP owned CPU peer review " + identity
    record = {"schema": "aos-gpu-peer-cpu-review.v1", "scope": __doc__, "checks": {},
              "client_source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              "before": existing, "runtime_directory": str(directory.relative_to(ROOT))}
    process = None
    observed = None
    try:
        command = [
            "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
            f"--unit={UNIT}", f"--description={description}", "--service-type=exec",
            "--property=MemoryMax=64M", "--property=MemorySwapMax=0", "--property=CPUQuota=100%",
            "--property=TasksMax=8", "--property=RuntimeMaxSec=25", "--property=TimeoutStopSec=3",
            "--property=KillMode=control-group", str(ROOT / ".venv/bin/python"), str(server), str(directory),
        ]
        record["server_command"] = command
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = show()
            if state.get("Description") == description and int(state.get("MainPID", "0")):
                observed = state
                if (directory / "broker.sock").exists():
                    break
            if process.poll() is not None:
                raise RuntimeError("fixture service exited before readiness")
            time.sleep(0.05)
        else:
            raise TimeoutError("fixture readiness expired")
        record["observed"] = observed
        environment = {**os.environ, "PYTHONPATH": str(SOURCE.parents[1]),
                       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
        result = subprocess.run([str(PYTHON), str(client), str(directory), observed["MainPID"]],
                                env=environment, capture_output=True, text=True, timeout=12, check=False)
        record.update({"client_exit_code": result.returncode, "client_stdout": result.stdout,
                       "client_stderr": result.stderr})
        stdout, stderr = process.communicate(timeout=5)
        record.update({"server_exit_code": process.returncode, "server_stdout": stdout, "server_stderr": stderr})
        if (directory / "client-result.json").exists():
            record["checks"].update(json.loads((directory / "client-result.json").read_bytes())["checks"])
        record["checks"]["client_and_fixture_exit_zero"] = result.returncode == process.returncode == 0
    except Exception as error:
        record["error_type"] = type(error).__name__
    finally:
        current = show()
        if current.get("MainPID", "0") != "0":
            if (observed and current.get("Description") == description
                    and current.get("InvocationID") == observed.get("InvocationID")):
                cleanup = subprocess.run(["/usr/bin/systemctl", "--user", "stop", UNIT],
                                         capture_output=True, timeout=5, check=False)
                record["cleanup_exit_code"] = cleanup.returncode
            else:
                record["foreign_generation_preserved"] = True
        if process is not None:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                record["fixture_wrapper_still_running"] = True
        record["after"] = show()
        record["checks"]["owned_fixture_stopped"] = record["after"].get("MainPID", "0") == "0"
        record["checks"]["source_unchanged"] = hashlib.sha256(SOURCE.read_bytes()).hexdigest() == record["client_source_sha256"]
        record["all_passed"] = all(record["checks"].values()) and "error_type" not in record
        with output.open("x") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
    print(json.dumps({k: record.get(k) for k in ("all_passed", "checks", "error_type", "client_exit_code", "server_exit_code")}))
    return 0 if record["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output))
