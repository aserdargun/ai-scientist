"""Check the native UDS client's wall deadline against a slow response body.

Only an owned Unix socket and a fixed JSON response are used; no GPU or model.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))


def main(output: Path) -> int:
    if output.exists():
        raise RuntimeError('refusing to overwrite HTTP deadline evidence')
    identity=uuid.uuid4().hex
    private=ROOT/'data/runtime/native-http-review'/identity
    private.mkdir(mode=0o700,parents=True)
    frozen=private/'native_runtime.py'
    frozen.write_bytes((ROOT/'lab/llm/native_runtime.py').read_bytes())
    (private/'review_driver.py').write_bytes(Path(__file__).read_bytes())
    spec=importlib.util.spec_from_file_location('native_http_frozen',frozen)
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    runtime=Path(f'/run/user/{os.getuid()}')/f'swapp-http-{identity}'
    runtime.mkdir(mode=0o700)
    path=runtime/'probe.sock'
    body=b'{"ok":true}'
    headers=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'
    stop=threading.Event()
    failures=[]
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        os.chmod(path,0o600)
        listener.listen(1)
        listener.settimeout(2)
        def serve():
            try:
                connection,_=listener.accept()
                with connection:
                    connection.settimeout(2)
                    connection.recv(4096)
                    connection.sendall(headers)
                    for value in body:
                        if stop.wait(.04):
                            break
                        connection.sendall(bytes([value]))
            except (BrokenPipeError,ConnectionResetError):
                pass
            except Exception as error:
                failures.append(f'{type(error).__name__}: {error}')
        thread=threading.Thread(target=serve,daemon=True)
        thread.start()
        started=time.monotonic()
        result=None
        error=None
        try:
            result=module._read_uds_json(path,'GET','/v1/models',None,timeout=.1)
        except Exception as caught:
            error=f'{type(caught).__name__}: {caught}'
        elapsed=time.monotonic()-started
        stop.set()
        thread.join(timeout=3)
    path.unlink()
    runtime.rmdir()
    checks={'worker_finished':not thread.is_alive() and not failures,'wall_deadline_bounded':elapsed<=.2,'slow_response_not_accepted_after_deadline':result is None}
    record={'schema':'native-http-deadline-review.v1','scope':'Actual private UDS HTTP function against a fixture server sending fixed JSON bytes every40ms; 100ms configured timeout,200ms acceptance tolerance. No production model or GPU.','source_sha256':hashlib.sha256(frozen.read_bytes()).hexdigest(),'frozen_source_path':str(frozen.relative_to(ROOT)),'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'timeout_seconds':.1,'elapsed_seconds':elapsed,'response':result,'error':error,'server_errors':failures,'checks':checks,'passed':all(checks.values())}
    output.write_text(json.dumps(record,indent=2,allow_nan=False)+'\n')
    print(json.dumps(record))
    return 0 if record['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(main(parser.parse_args().output))
