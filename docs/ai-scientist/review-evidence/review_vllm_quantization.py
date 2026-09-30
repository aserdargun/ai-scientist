"""Inspect installed quantization code and checkpoint headers without loading CUDA."""
from __future__ import annotations
import ast
from collections import Counter
from datetime import UTC,datetime
import hashlib
import json
from pathlib import Path
import struct

ROOT=Path(__file__).resolve().parents[3]
PACKAGE=ROOT/'data/runtime/vllm/.venv/lib/python3.12/site-packages/vllm'
MODEL=ROOT/'models/Qwen3.5-9B/c202236235762e1c871ad0ccb60c8ee5ba337b9a'


def main():
    source_paths=[PACKAGE/'model_executor/layers/quantization/__init__.py',
                  PACKAGE/'config/quantization.py',
                  PACKAGE/'model_executor/layers/quantization/online/fp8.py',
                  PACKAGE/'model_executor/model_loader/default_loader.py',
                  PACKAGE/'model_executor/models/qwen3_5.py']
    constants=[]
    for node in ast.walk(ast.parse(source_paths[0].read_text())):
        if isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='QuantizationMethods' for target in node.targets):
            constants=[sub.value for sub in ast.walk(node.value) if isinstance(sub,ast.Constant) and isinstance(sub.value,str)]
    sizes=Counter();types=Counter();headers=[]
    for path in sorted(MODEL.glob('*.safetensors')):
        with path.open('rb') as stream:
            length=struct.unpack('<Q',stream.read(8))[0]
            if length>16*1024**2:raise ValueError('unbounded checkpoint header')
            header=stream.read(length)
        headers.append({'file':path.name,'header_bytes':length,'header_sha256':hashlib.sha256(header).hexdigest()})
        for key,info in json.loads(header).items():
            if key=='__metadata__':continue
            byte_count=info['data_offsets'][1]-info['data_offsets'][0]
            sizes['.'.join(key.split('.')[:2])]+=byte_count
            types[info['dtype']]+=byte_count
    result={'checked_at':datetime.now(UTC).isoformat(),'vllm_version':'0.30.0',
            'source_sha256':{str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
            'installed_quantization_methods':constants,
            'bitsandbytes_is_builtin': 'bitsandbytes' in constants,
            'fp8_per_tensor_is_builtin':'fp8_per_tensor' in constants,
            'fp8_online_meta_weight_allocation_found':'device="meta"' in source_paths[2].read_text(),
            'checkpoint_headers':headers,'checkpoint_bytes_by_prefix':dict(sizes),'checkpoint_bytes_by_dtype':dict(types),
            'language_model_and_lm_head_bf16_bytes':sizes['model.language_model']+sizes['lm_head.weight'],
            'reading_scope':'AST/text and safetensors headers only; no tensor contents, model construction, CUDA call, server or GPU inference',
            'implications':['Use the installed fp8_per_tensor online scheme for the planned FP8 experiment; older BitsAndBytes examples do not describe this installed registry.',
                            'Meta weight allocation is a source observation, not proof of actual startup peak memory or kernel compatibility.',
                            'Language model plus lm_head BF16 tensors already exceed 16 GiB; unquantized all-GPU loading cannot fit this host.',
                            'Actual sm89 kernel support, 32k context with two sequences, throughput, prefix caching, S1/S2 tools and bounded GPU handoff remain unverified.'],
            'primary_sources':['https://docs.vllm.ai/en/latest/features/quantization/online/','https://recipes.vllm.ai/Qwen/Qwen3.5-9B'],
            'command':'.venv/bin/python docs/ai-scientist/review-evidence/review_vllm_quantization.py',
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    Path(__file__).with_name('vllm-quantization-preflight.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({key:result[key] for key in ('bitsandbytes_is_builtin','fp8_per_tensor_is_builtin','fp8_online_meta_weight_allocation_found','language_model_and_lm_head_bf16_bytes')}))


if __name__=='__main__':main()
