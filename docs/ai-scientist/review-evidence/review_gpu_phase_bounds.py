"""Independent deadline probes against the production GPU scheduler.

The principal resolver and drain observer are fixtures; time is injected. This
checks scheduler behavior only, not authenticated systemd admission or CUDA.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from lab.llm.gpu_scheduler import (
    PrincipalReceipt,
    ProcessIdentity,
    SharedGpuScheduler,
)


class FixtureResolver:
    """Test composition: actual process identity, simulated principal authority."""

    def __init__(self) -> None:
        fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
        self.identity = ProcessIdentity(
            os.getpid(), int(fields[19]),
            Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        )

    def resolve(self, owner: str) -> PrincipalReceipt:
        return PrincipalReceipt(owner, self.identity, "cpu-review-" + owner, "1" * 32)

    def verify(self, receipt: PrincipalReceipt) -> bool:
        return receipt.identity == self.identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("refusing to overwrite an existing review record")
    source = ROOT / "lab/llm/gpu_scheduler.py"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    checks = {}
    observations = {}
    with tempfile.TemporaryDirectory(prefix="gpu-phase-review-", dir=ROOT / "data/runtime") as directory:
        clock = [0.0]

        def at(instant, method, *arguments):
            clock[0] = float(instant)
            return method(*arguments)

        def scheduler(name: str) -> SharedGpuScheduler:
            return SharedGpuScheduler(
                Path(directory) / (name + ".sqlite"),
                principal_resolver=FixtureResolver(),
                drain_verifier=lambda _lease: False,
                clock=lambda: clock[0],
            )

        repeated = scheduler("repeated")
        at(0, repeated.submit, "aos", "turn", b"immutable-30-second-request")
        first = at(0, repeated.try_acquire, "aos", "turn")
        heartbeat = at(1, repeated.heartbeat, first)
        ready = at(2, repeated.mark_ready, heartbeat)
        checks["heartbeat_preserves_requested_inference_budget"] = (
            heartbeat.slice_seconds == 30 and ready.inference_deadline == 32
        )
        before_expiry = at(3, repeated.mark_ready, first)
        checks["ready_retry_keeps_original_deadline"] = (
            before_expiry is not None and before_expiry.inference_deadline == 32
        )
        expired = at(1000, repeated.mark_ready, first)
        checks["expired_ready_retry_rejected"] = expired is None
        observations["initial_ready_deadline"] = ready.inference_deadline
        observations["expired_retry_returned_receipt"] = expired is not None

        receipt = scheduler("receipt")
        at(0, receipt.submit, "lab", "turn", b"immutable-30-second-request")
        original = at(0, receipt.try_acquire, "lab", "turn")
        changed = replace(original, slice_seconds=600, total_deadline=10000)
        changed_ready = at(1, receipt.mark_ready, changed)
        checks["caller_receipt_cannot_extend_persisted_inference_budget"] = (
            changed_ready is None or changed_ready.inference_deadline <= 31
        )
        observations["modified_receipt_inference_deadline"] = (
            None if changed_ready is None else changed_ready.inference_deadline
        )

        activation = scheduler("activation")
        at(0, activation.submit, "aos", "turn", b"bounded-activation")
        lease = at(0, activation.try_acquire, "aos", "turn")
        deadlines = [lease.activation_deadline]
        for instant in (29, 58, 87, 116, 145, 174):
            lease = at(instant, activation.heartbeat, lease)
            if lease is None:
                break
            deadlines.append(lease.activation_deadline)
        checks["heartbeats_cannot_extend_activation_deadline"] = (
            len(deadlines) == 7 and set(deadlines) == {180}
            and at(180, activation.heartbeat, lease) is None
        )
        checks["unverified_drain_blocks_recovery"] = (
            at(181, activation.recover_quarantined) is False
        )
    checks["production_source_unchanged_during_review"] = (
        hashlib.sha256(source.read_bytes()).hexdigest() == digest
    )
    record = {
        "schema": "gpu-phase-bound-review.v2",
        "command": ".venv/bin/python docs/ai-scientist/review-evidence/review_gpu_phase_bounds.py --output " + str(args.output),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": digest,
        "scope": "Production scheduler, private SQLite, fake clock/principal resolver/no-drain observer; no GPU, model or systemd unit.",
        "checks": checks,
        "observations": observations,
        "passed": all(checks.values()),
    }
    args.output.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
    print(json.dumps(record, indent=2))
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
