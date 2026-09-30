# ADR 0014: bounded local training maintenance

Status: implementation prepared; GPU and model-runtime acceptance pending coordinated measurement.

## Decision

Expose only `lab gpu lease TRAIN --noop` and `lab doctor train --dry-run`. Both enter the same fixed Lab maintenance principal, `swapp-lab-gpu-maintenance.service`, and the shared AOS/Lab scheduler. TRAIN remains one synthetic-only QLoRA measurement: Qwen3.5-9B, 4-bit loading, LoRA rank 32, 24,576 tokens, three steps, no adapter save. The no-op path only records a bounded lease and performs a fresh S1 local serve smoke after the lease drains.

The existing inference runtime is per-call: each call starts its transient service, performs one bounded request, drains it, and records the result. Training does not install a permanent vLLM server or model adapter. The post-training smoke is a separate normal S1 call after TRAIN releases the lease, so it verifies the existing lifecycle and remains in scheduler order.

## Fencing and recovery

A private SQLite ledger records immutable operation terms, state transitions, and the child unit generation. The QLoRA child starts behind a private gate and imports no torch, Transformers, PEFT, or Unsloth modules until the parent has read its systemd MainPID, `/proc` start ticks, boot ID, InvocationID, and cgroup, then durably stored that exact generation. The gate binds those values back to the child PID/start/boot/cgroup. Child stop and lease release require the same unit invocation and cgroup to be inactive and empty, followed by an independent GPU-process drain check.

If the parent dies, an unbound child cannot pass the gate and therefore cannot start model/GPU work. A later worker may recover only a quarantined lease whose owner PID/start/boot is proven dead. It uses the immutable child binding to stop and drain only that same child generation. If any identity is stale, unreadable, or inconsistent, the lease remains quarantined.

The scheduler's persisted queue deadlines and worker waits use `CLOCK_BOOTTIME`, keeping deadlines consistent across suspend. CPU/RAM/task/runtime limits are enforced by the fixed transient systemd unit. The operation uses offline package/model settings and synthetic token IDs only.

## Scope and acceptance

This code and its CPU-only tests do not establish that the pinned Unsloth path actually honors embedding CPU offload, that the requested long-context training fits, or that the GPU runtime reaches the target. The child reports observed embedding placement and GPU allocated/reserved peaks and rejects an offload request that was not realized. An actual, root-coordinated 24k/three-step GPU run and post-restore local smoke remain required before M0.14 is accepted. No new dependencies or model weights are downloaded by this path.
