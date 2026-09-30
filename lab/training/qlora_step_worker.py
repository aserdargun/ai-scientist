"""One fixed, offline 9B QLoRA dry-run; called only by the bounded systemd child."""

# pylint: disable=too-many-boolean-expressions,missing-function-docstring,missing-class-docstring,too-many-locals,too-many-statements,import-outside-toplevel,invalid-name,import-error,broad-exception-caught

from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SEQUENCE_LENGTH = 24_576
STEPS = 3
LORA_RANK = 32
MODEL_REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIRECTORY = PROJECT_ROOT / "models/Qwen3.5-9B" / MODEL_REVISION
MAX_RESULT_BYTES = 64 * 1024


def _write_result(path: Path, value: dict[str, object]) -> None:
    if path.is_symlink() or path.exists():
        raise RuntimeError("result path already exists or is unsafe")
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > MAX_RESULT_BYTES:
        raise RuntimeError("training result exceeds its bound")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".train-result-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _await_start_gate(
    path: Path, operation_id: str, timeout_seconds: int = 45
) -> dict[str, object]:
    """Block before importing GPU libraries until the parent binds this process."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.is_symlink():
            raise RuntimeError("training start gate is unsafe")
        if path.is_file():
            if path.stat().st_size > 4096:
                raise RuntimeError("training start gate exceeds its bound")
            try:
                value = json.loads(path.read_text(encoding="ascii"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("training start gate is malformed") from exc
            required = {
                "operation_id",
                "unit",
                "pid",
                "start_ticks",
                "boot_id",
                "invocation_id",
                "cgroup",
            }
            if not isinstance(value, dict) or set(value) != required:
                raise RuntimeError("training start gate fields are invalid")
            if value["operation_id"] != operation_id or value["pid"] != os.getpid():
                raise RuntimeError("training start gate process binding does not match")
            if (
                not isinstance(value["start_ticks"], int)
                or isinstance(value["start_ticks"], bool)
                or value["start_ticks"] <= 0
                or not isinstance(value["boot_id"], str)
                or re.fullmatch(r"[0-9a-f-]{36}", value["boot_id"]) is None
                or not isinstance(value["invocation_id"], str)
                or re.fullmatch(r"[0-9a-f]{32}", value["invocation_id"]) is None
                or not isinstance(value["unit"], str)
                or re.fullmatch(r"swapp-lab-train-job-[0-9a-f]{32}\.service", value["unit"]) is None
                or not isinstance(value["cgroup"], str)
                or "/swapp-gpu.slice/" not in value["cgroup"]
                or not value["cgroup"].endswith("/" + value["unit"])
            ):
                raise RuntimeError("training start gate generation is invalid")
            proc_stat = Path(f"/proc/{os.getpid()}/stat").read_text(encoding="ascii")
            start_ticks = int(proc_stat.rsplit(")", maxsplit=1)[1].split()[19])
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            proc_cgroup = Path(f"/proc/{os.getpid()}/cgroup").read_text(encoding="ascii")
            unified = next(
                (
                    line.split(":", 2)[2]
                    for line in proc_cgroup.splitlines()
                    if line.startswith("0::")
                ),
                "",
            )
            if (
                start_ticks != value["start_ticks"]
                or boot_id != value["boot_id"]
                or unified.rstrip("/") != value["cgroup"]
            ):
                raise RuntimeError("training start gate no longer names this process generation")
            return value
        time.sleep(0.1)
    raise RuntimeError("training parent did not durably bind this child generation")


def run(operation_id: str, result_path: Path, gate_path: Path) -> dict[str, object]:
    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("TRANSFORMERS_OFFLINE") != "1":
        raise RuntimeError("training process must run in offline mode")
    if os.environ.get("HF_HUB_DISABLE_TELEMETRY") != "1":
        raise RuntimeError("training telemetry must be disabled")
    if not MODEL_DIRECTORY.is_dir() or MODEL_DIRECTORY.is_symlink():
        raise RuntimeError("pinned model directory is unavailable")

    _await_start_gate(gate_path, operation_id)

    # Unsloth must patch supported Transformers/PEFT call paths before import.
    import importlib

    FastLanguageModel = importlib.import_module("unsloth").FastLanguageModel

    # These imports intentionally occur only after the durable child binding.
    import torch
    from transformers import Trainer, TrainingArguments, default_data_collator

    class SyntheticTokenDataset:
        def __init__(self, vocab_size: int, count: int = STEPS) -> None:
            generator = random.Random(20260925)  # nosec B311 -- deterministic synthetic input only.
            low_token = 16
            rows = [
                [
                    generator.randrange(low_token, max(low_token + 1, vocab_size - 1))
                    for _ in range(SEQUENCE_LENGTH)
                ]
                for _ in range(count)
            ]
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def __getitem__(self, index: int) -> dict[str, Any]:
            ids = torch.tensor(self.rows[index], dtype=torch.long)
            return {
                "input_ids": ids,
                "attention_mask": torch.ones_like(ids),
                "labels": ids.clone(),
            }

    torch.manual_seed(20260925)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA was not active; this cannot be recorded as a VRAM measurement")
    torch.cuda.reset_peak_memory_stats()
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=str(MODEL_DIRECTORY),
        max_seq_length=SEQUENCE_LENGTH,
        dtype=None,
        load_in_4bit=True,
        local_files_only=True,
        offload_embedding=True,
        device_map="auto",
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_RANK,
        target_modules=[
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ],
        lora_alpha=LORA_RANK,
        lora_dropout=0.0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=20260925,
        max_seq_length=SEQUENCE_LENGTH,
        offload_embedding=True,
    )
    embedding = model.get_input_embeddings()
    embedding_devices = {str(parameter.device) for parameter in embedding.parameters()}
    if len(embedding_devices) != 1:
        raise RuntimeError("embedding placement is mixed and cannot be reported as CPU offload")
    embedding_device = next(iter(embedding_devices))
    cpu_offload_embeddings = embedding_device == "cpu"
    if not cpu_offload_embeddings:
        raise RuntimeError("the requested CPU embedding offload was not realized by the model")
    model.config.use_cache = False
    training_args = TrainingArguments(
        output_dir=str(result_path.parent / f"{operation_id}.tmp"),
        max_steps=STEPS,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=1,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="adamw_8bit",
        learning_rate=2e-4,
        logging_strategy="no",
        save_strategy="no",
        report_to=[],
        remove_unused_columns=False,
        dataloader_num_workers=0,
        dataloader_pin_memory=False,
        disable_tqdm=True,
        bf16=bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported()),
        fp16=False,
    )
    data = SyntheticTokenDataset(int(tokenizer.vocab_size))
    if len(data.rows[0]) != SEQUENCE_LENGTH:
        raise RuntimeError("synthetic sequence length differs from the fixed profile")
    temporary_output = Path(training_args.output_dir)
    atexit.register(shutil.rmtree, temporary_output, ignore_errors=True)
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=data,
        data_collator=default_data_collator,
    )
    result = trainer.train()
    steps_completed = int(result.global_step)
    if steps_completed != STEPS:
        raise RuntimeError("trainer completed a different number of steps")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA was not active; this cannot be recorded as a VRAM measurement")
    peak = int(torch.cuda.max_memory_allocated())
    peak_reserved = int(torch.cuda.max_memory_reserved())
    if peak <= 0 or peak_reserved < peak:
        raise RuntimeError("CUDA peak memory measurement is missing")
    output = {
        "adapter_rank": LORA_RANK,
        "adapter_saved": False,
        "cpu_offload_embeddings": True,
        "embedding_device": embedding_device,
        "schema": "training-dry-run.v1",
        "load_in_4bit": True,
        "status": "ok",
        "steps_completed": steps_completed,
        "sequence_length": SEQUENCE_LENGTH,
        "peak_gpu_allocated_bytes": peak,
        "peak_gpu_reserved_bytes": peak_reserved,
        "synthetic_data_only": True,
    }
    del trainer, model, tokenizer, data
    torch.cuda.empty_cache()
    shutil.rmtree(temporary_output, ignore_errors=True)
    _write_result(result_path, output)
    # Keep this exact transient generation alive until the parent has verified
    # the atomic result and explicitly drains the unit through systemd.
    while True:
        time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab-training-qlora-worker")
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--start-gate", required=True, type=Path)
    args = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{32}", args.operation_id) is None:
        parser.error("operation ID is invalid")
    try:
        run(args.operation_id, args.result_file, args.start_gate)
        return 0
    except Exception as exc:
        # Error text/logging can contain source paths or model metadata. The
        # supervisor records only this stable class code and nonzero exit.
        print(f"training_dry_run_failed:{type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
