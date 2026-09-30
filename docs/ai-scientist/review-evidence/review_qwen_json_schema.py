"""Verify installed vLLM schema conversion and XGrammar on CPU with pinned tokenizer.

This does not invoke a model, allocate CUDA, or prove constrained S2 generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]

WORKER = r'''
import hashlib,json,sys,time
from pathlib import Path
import torch
import xgrammar as xgr
from transformers import AutoTokenizer
from vllm.entrypoints.generate.base.protocol import ResponseFormat,structured_outputs_from_response_format
directory=Path(sys.argv[1]);inputs=json.loads((directory/'inputs.json').read_text());started=time.monotonic()
schema=inputs['schema'];checks={}
wire={'type':'json_schema','json_schema':{'name':'candidate_proposal_v1','strict':True,'schema':schema}}
typed=ResponseFormat.model_validate(wire,strict=True)
params=structured_outputs_from_response_format(None,typed)
checks['vllm_preserves_exact_json_schema']=params.json==schema
tokenizer=AutoTokenizer.from_pretrained(inputs['model_directory'],local_files_only=True,trust_remote_code=False)
info=xgr.TokenizerInfo.from_huggingface(tokenizer)
compiler=xgr.GrammarCompiler(info,max_threads=1,cache_enabled=False)
compiled=compiler.compile_json_schema(params.json)
def accepts(value):
 matcher=xgr.GrammarMatcher(compiled,terminate_without_stop_token=True)
 return matcher.accept_string(value) and matcher.is_terminated()
valid={'hypothesis':'CPU grammar fixture','move_type':'features','candidate_source':'def build_candidate(): return None','predicted_delta':0.1}
encoded=json.dumps(valid,separators=(',',':'))
checks['valid_complete_candidate_json_accepted']=accepts(encoded)
checks['markdown_fenced_json_rejected']=not accepts('```json\n'+encoded+'\n```')
checks['unknown_move_rejected']=not accepts(json.dumps({**valid,'move_type':'unknown'}))
checks['model_verdict_field_rejected']=not accepts(json.dumps({**valid,'verdict':'KEEP'}))
checks['missing_source_field_rejected']=not accepts(json.dumps({k:v for k,v in valid.items() if k!='candidate_source'}))
checks['cuda_not_initialized']=not torch.cuda.is_initialized()
out={'scope':'CPU vLLM request-schema conversion and real pinned-tokenizer XGrammar compilation only; no model generation',
     'checks':checks,'seconds':time.monotonic()-started,'tokenizer_class':type(tokenizer).__name__,
     'schema_sha256':hashlib.sha256(json.dumps(schema,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
(directory/'result.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out));raise SystemExit(0 if all(checks.values()) else 1)
'''


def main(output: Path) -> int:
    from lab.director.contracts import CandidateProposal
    from lab.llm.native_runtime import ModelPin

    if output.exists():
        raise ValueError("refusing to overwrite evidence")
    os.umask(0o077)
    pin = ModelPin.from_repository()
    source = ROOT / "data/runtime/vllm/.venv/lib/python3.12/site-packages"
    tracked = [ROOT / "lab/director/contracts.py", Path(__file__),
               source / "vllm/entrypoints/generate/base/protocol.py",
               source / "xgrammar/compiler.py", source / "xgrammar/tokenizer_info.py"]
    tokenizer_files = {name: (size, digest) for name, size, digest in pin.files
                       if name in {"tokenizer.json", "tokenizer_config.json", "chat_template.jinja",
                                   "config.json", "vocab.json", "merges.txt"}}
    for name, (size, digest) in tokenizer_files.items():
        file = pin.directory / name
        if file.stat().st_size != size or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError("pinned tokenizer file changed")
        tracked.append(file)
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in tracked}
    identity = uuid4().hex
    directory = ROOT / "data/runtime/qwen-json-schema-review" / identity
    directory.mkdir(parents=True, mode=0o700)
    (directory / "inputs.json").write_text(json.dumps({"schema": CandidateProposal.model_json_schema(),
                                                     "model_directory": str(pin.directory)}))
    (directory / "worker.py").write_text(WORKER)
    command = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
        f"--unit=swapp-review-schema-{identity}.service", "--service-type=exec",
        f"--working-directory={ROOT}", "--property=MemoryMax=2G", "--property=MemorySwapMax=0",
        "--property=CPUQuota=100%", "--property=TasksMax=64", "--property=RuntimeMaxSec=90",
        "--property=TimeoutStopSec=5", "--property=KillMode=control-group", "--property=LimitFSIZE=1048576",
        "--setenv=CUDA_VISIBLE_DEVICES=", "--setenv=HF_HUB_OFFLINE=1", "--setenv=TRANSFORMERS_OFFLINE=1",
        "--setenv=OPENBLAS_NUM_THREADS=1", "--setenv=OMP_NUM_THREADS=1", "--setenv=MKL_NUM_THREADS=1",
        "--setenv=TOKENIZERS_PARALLELISM=false", f"--setenv=XDG_CACHE_HOME={directory / 'cache'}",
        f"--setenv=TRITON_CACHE_DIR={directory / 'cache/triton'}",
        f"--setenv=TORCHINDUCTOR_CACHE_DIR={directory / 'cache/torchinductor'}",
        str(ROOT / "data/runtime/vllm/.venv/bin/python"), str(directory / "worker.py"), str(directory),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    record = {"schema": "qwen-json-schema-cpu-review.v1", "command": command,
              "exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
              "source_sha256": hashes, "runtime_directory": str(directory.relative_to(ROOT)),
              "sources_unchanged": all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h
                                       for p, h in hashes.items())}
    if (directory / "result.json").exists():
        record["result"] = json.loads((directory / "result.json").read_bytes())
    record["all_passed"] = (result.returncode == 0 and record["sources_unchanged"]
                            and all(record.get("result", {}).get("checks", {"missing": False}).values()))
    with output.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"all_passed": record["all_passed"], "worker_exit_code": result.returncode,
                      "result": record.get("result"), "stderr_tail": result.stderr[-1800:]}))
    return 0 if record["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(main(args.output))
