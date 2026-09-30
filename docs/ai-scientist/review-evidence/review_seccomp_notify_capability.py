"""Probe rootless seccomp notifications inside an owned CPU-only sandbox."""
from __future__ import annotations
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from lab.sandbox.docker_runner import LocalDockerRunner, SandboxProfile

ROOT=Path(__file__).resolve().parents[3]
IMAGE='sha256:3f9df3002bccd1cb74aa5f6060da74dbdecc4c65fc0c8f712ecf67f0590754df'
PROBE=r'''import array,ctypes,errno,json,os,platform,select,socket
from pathlib import Path
if platform.machine() != 'x86_64': raise RuntimeError('fixture is x86_64 only')
libc=ctypes.CDLL(None,use_errno=True)
class Filter(ctypes.Structure):
 _fields_=[('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class Program(ctypes.Structure):
 _fields_=[('length',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
class Data(ctypes.Structure):
 _fields_=[('nr',ctypes.c_int),('arch',ctypes.c_uint),('ip',ctypes.c_ulonglong),('args',ctypes.c_ulonglong*6)]
class Notification(ctypes.Structure):
 _fields_=[('id',ctypes.c_ulonglong),('pid',ctypes.c_uint),('flags',ctypes.c_uint),('data',Data)]
class Response(ctypes.Structure):
 _fields_=[('id',ctypes.c_ulonglong),('value',ctypes.c_longlong),('error',ctypes.c_int),('flags',ctypes.c_uint)]
assert ctypes.sizeof(Notification)==80 and ctypes.sizeof(Response)==24
parent,child=socket.socketpair()
pid=os.fork()
if pid==0:
 parent.close()
 if libc.prctl(38,1,0,0,0)!=0:
  child.send(json.dumps({'stage':'no_new_privs','errno':ctypes.get_errno()}).encode());os._exit(1)
 instructions=(Filter*7)(Filter(0x20,0,0,4),Filter(0x15,1,0,0xc000003e),Filter(0x06,0,0,0x80000000),Filter(0x20,0,0,0),Filter(0x15,0,1,39),Filter(0x06,0,0,0x7fc00000),Filter(0x06,0,0,0x7fff0000))
 program=Program(len(instructions),instructions)
 fd=libc.syscall(317,1,8,ctypes.byref(program))
 if fd<0:
  child.send(json.dumps({'stage':'listener_install','errno':ctypes.get_errno()}).encode());os._exit(1)
 child.sendmsg([b'listener'],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array('i',[fd]))])
 os.close(fd)
 ctypes.set_errno(0)
 result=libc.syscall(39)
 child.send(json.dumps({'raw_getpid_result':result,'errno':ctypes.get_errno()}).encode())
 os._exit(0)
child.close()
parent.settimeout(4)
message,ancillary,flags,address=parent.recvmsg(1024,socket.CMSG_SPACE(array.array('i').itemsize))
if not ancillary:
 result={'available':False,'child_error':json.loads(message)}
else:
 fds=array.array('i');fds.frombytes(ancillary[0][2][:fds.itemsize]);fd=fds[0]
 if not select.select([fd],[],[],4)[0]:raise TimeoutError('no notification')
 request=Notification()
 if libc.ioctl(fd,0xc0502100,ctypes.byref(request))!=0:raise OSError(ctypes.get_errno(),'receive notification failed')
 response=Response(request.id,0,-errno.EPERM,0)
 if libc.ioctl(fd,0xc0182101,ctypes.byref(response))!=0:raise OSError(ctypes.get_errno(),'respond failed')
 observed=json.loads(parent.recv(1024))
 result={'available':True,'notification_pid_matches_child':request.pid==pid,'syscall_number':request.data.nr,'architecture':hex(request.data.arch),'original_syscall_denied':observed=={'raw_getpid_result':-1,'errno':errno.EPERM},'child_observed':observed}
 os.close(fd)
parent.close()
_,status=os.waitpid(pid,0)
result['child_wait_status']=status
Path('/output/seccomp.json').write_text(json.dumps(result,sort_keys=True))
'''


def main():
    work_root=ROOT/'data/runtime'/f'seccomp-capability-review-{uuid4().hex}'
    record={'checked_at':datetime.now(UTC).isoformat(),'image':IMAGE,
        'scope':'Rootless seccomp capability only: harmless raw getpid syscall is reported to a separate parent process and denied. Existing network-none/cap-drop-all/no-new-privileges/UID10001 sandbox. No host syscall policy, AOS service, network connection, GPU or dataset changes. Does not prove production tamper-proof audit or exhaustive syscall coverage.',
        'source_urls':['https://www.man7.org/linux/man-pages/man2/seccomp_unotify.2.html','https://docs.python.org/3.12/library/sys.html#sys.addaudithook'],
        'candidate_source_sha256':hashlib.sha256(PROBE.encode()).hexdigest()}
    try:
        runner=LocalDockerRunner(image=IMAGE,work_root=work_root,profile=SandboxProfile(memory_bytes=128*1024**2,cpus=.25,pids=16,timeout_seconds=10,output_bytes=4096))
        result=runner.run_phase(phase='fit',candidate_source=PROBE.encode(),arrow_input=b'')
        payload=next(item.content for item in result.artifacts if item.name=='seccomp.json')
        record.update(observation=json.loads(payload),container=result.container_name,container_exit_code=result.exit_code,
                      elapsed_seconds=result.elapsed_seconds)
    finally:
        if work_root.exists():work_root.rmdir()
    record['script_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    record['command']='.venv/bin/python docs/ai-scientist/review-evidence/review_seccomp_notify_capability.py'
    Path(__file__).with_name('seccomp-notify-capability-review.json').write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps(record,indent=2))

if __name__=='__main__':main()
