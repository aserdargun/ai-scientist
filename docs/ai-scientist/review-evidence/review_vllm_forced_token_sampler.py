"""Review pinned vLLM sampling on synthetic logits, with no model or AOS.

All numbers represent constructed tensors. No production runtime or package is
modified. A private bounded unit is stopped only when its generation matches.
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

from review_gpu_host import GIB, ROOT, snapshot
from review_native_cuda import show

WORKER = r'''
import json,os,pathlib,sys
import torch
torch.set_num_threads(1)
from vllm.v1.sample.ops.topk_topp_sampler import apply_top_k_top_p
from vllm.v1.worker.gpu.sample.gumbel import gumbel_sample
target=pathlib.Path(sys.argv[1])
rows=[]
vocab=248320
forced_id=248068
torch.manual_seed(0)
original=torch.randn((1,vocab),dtype=torch.float32,device='cpu').cuda()
idx=torch.tensor([0],dtype=torch.int32,device='cuda')
pos=torch.tensor([511],dtype=torch.int64,device='cuda')
seed=torch.tensor([0],dtype=torch.int64,device='cuda')
for force_value,temperature,top_k,top_p in [
    (30.,.6,20,.95),(1e9,.6,20,.95),(1e9,1.,20,.95),
    (1e9,.6,None,.95),(1e9,.6,None,1.),(1e9,.6,20,1.),
]:
    logits=original.clone()
    logits[0,forced_id]=force_value
    logits.div_(temperature)
    k=None if top_k is None else torch.tensor([top_k],dtype=torch.int32,device='cuda')
    p=None if top_p==1 else torch.tensor([top_p],dtype=torch.float32,device='cuda')
    filtered=apply_top_k_top_p(logits,k,p)
    sampled=gumbel_sample(filtered,idx,torch.tensor([temperature],device='cuda'),
                          seed,pos,apply_temperature=False,is_drafting=False,use_fp64=False)
    torch.cuda.synchronize()
    rows.append({'force_value':force_value,'temperature':temperature,'top_k':top_k,'top_p':top_p,
                 'forced_token_survived':bool(torch.isfinite(filtered[0,forced_id]).item()),
                 'finite_count':int(torch.isfinite(filtered).sum().item()),
                 'nan_count':int(torch.isnan(filtered).sum().item()),
                 'sampled_expected_token':int(sampled.item())==forced_id,
                 'sampled_token_id_synthetic':int(sampled.item())})
record={'scope':'synthetic logits only; no model generation','pid':os.getpid(),
        'netns':os.readlink('/proc/self/ns/net'),
        'cgroup':pathlib.Path('/proc/self/cgroup').read_text().strip(),
        'cases':rows,'peak_allocated_bytes':torch.cuda.max_memory_allocated(),
        'peak_reserved_bytes':torch.cuda.max_memory_reserved(),
        'torch':torch.__version__,'cuda':torch.version.cuda}
target.write_text(json.dumps(record,indent=2)+'\n')
'''


def main(output: Path) -> int:
    if output.exists() or output.is_symlink():
        raise ValueError("refusing to overwrite evidence")
    task_id = uuid4().hex
    unit = f"swapp-review-vllm-sampler-{task_id}.service"
    runtime = ROOT / "data/runtime/gpu" / f"sampler-review-{task_id}"
    runtime.mkdir(mode=0o700)
    worker = runtime / "worker.py"
    worker.write_text(WORKER)
    worker.chmod(0o600)
    result = runtime / "result.json"
    record = {"schema": "vllm-synthetic-forced-token-sampler-review.v1",
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "worker_sha256": hashlib.sha256(WORKER.encode()).hexdigest(),
              "scope": "Actual GPU, constructed logits, no model or AOS acceptance",
              "unit": unit, "samples": [], "checks": {}}
    site = ROOT / "data/runtime/vllm/.venv/lib/python3.12/site-packages"
    names = ("vllm/v1/sample/ops/topk_topp_sampler.py",
             "vllm/v1/sample/ops/topk_topp_triton.py",
             "vllm/v1/worker/gpu/sample/gumbel.py")
    record["installed_source_sha256"] = {
        name: hashlib.sha256((site / name).read_bytes()).hexdigest() for name in names
    }
    process = None
    identity = None
    parent_netns = os.readlink("/proc/self/ns/net")
    started = time.monotonic()
    try:
        before = snapshot()
        record["before"] = before
        if (before["memory_available_bytes"] < 16 * GIB
                or before["disk_available_bytes"] < 20 * GIB
                or before["gpu"]["temperature_c"] >= 83
                or any(not row["existing_display_exemption"] for row in before["gpu_consumers"])):
            raise RuntimeError("host preflight refuses CUDA probe")
        argv = ["/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--collect",
                f"--unit={unit}", "--property=MemoryMax=3G", "--property=MemorySwapMax=0",
                "--property=CPUQuota=150%", "--property=TasksMax=64", "--property=RuntimeMaxSec=120",
                "--property=KillMode=control-group", "--property=TimeoutStopSec=3",
                "--property=NoNewPrivileges=yes", "--property=UMask=0077",
                f"--setenv=TRITON_CACHE_DIR={runtime / 'triton'}",
                f"--setenv=XDG_CACHE_HOME={runtime / 'cache'}",
                "--setenv=VLLM_USE_FLASHINFER_SAMPLER=0", "--setenv=HF_HUB_OFFLINE=1",
                "--setenv=VLLM_NO_USAGE_STATS=1", "--setenv=OMP_NUM_THREADS=1",
                "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=MAX_JOBS=1",
                "/usr/bin/unshare", "--user", "--map-root-user", "--net",
                str(ROOT / "data/runtime/vllm/.venv/bin/python"), str(worker), str(result)]
        record["command"] = argv
        with (runtime / "worker.log").open("xb") as log:
            process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                current = show(unit)
                if current.get("InvocationID"):
                    if identity is not None and identity["InvocationID"] != current["InvocationID"]:
                        raise RuntimeError("owned probe generation changed")
                    identity = current
                observed = snapshot()
                record["samples"].append(observed)
                pid = int(current.get("MainPID", "0"))
                if any(not row["existing_display_exemption"] and row["pid"] != pid
                       for row in observed["gpu_consumers"]):
                    raise RuntimeError("foreign GPU consumer appeared")
                if (observed["memory_available_bytes"] < 6 * GIB
                        or observed["disk_available_bytes"] < 20 * GIB
                        or observed["gpu"]["temperature_c"] >= 83):
                    raise RuntimeError("host reserve threshold reached")
                if time.monotonic() - started > 130:
                    raise TimeoutError("review exceeded wall limit")
                time.sleep(.5)
            record["process_exit_code"] = process.wait(timeout=2)
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        if process is not None and process.poll() is None:
            current = show(unit)
            if identity is not None and current.get("InvocationID") == identity["InvocationID"]:
                subprocess.run(["systemctl", "--user", "stop", unit], check=True, timeout=10)
            record["process_exit_code"] = process.wait(timeout=10)
        record["unit_identity"] = identity
        record["final_unit"] = show(unit)
        record["after"] = snapshot()
        record["elapsed_seconds"] = time.monotonic() - started
    if result.exists():
        record["worker"] = json.loads(result.read_text())
    record["checks"] = {
        "no_observer_error": "error" not in record,
        "process_exit_zero": record.get("process_exit_code") == 0,
        "all_forced_tokens_sampled": bool(record.get("worker")) and all(
            row["sampled_expected_token"] for row in record["worker"]["cases"]),
        "unit_terminal": record["final_unit"].get("MainPID") == "0",
        "no_non_display_gpu_after": all(row["existing_display_exemption"]
                                       for row in record["after"]["gpu_consumers"]),
        "private_netns": bool(record.get("worker")) and record["worker"]["netns"] != parent_netns,
        "source_unchanged": record["installed_source_sha256"] == {
            name: hashlib.sha256((site / name).read_bytes()).hexdigest() for name in names},
    }
    record["passed"] = all(record["checks"].values())
    output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "checks": record["checks"], "error": record.get("error")}))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(main(parser.parse_args().output))
