# ADR 0015: Training worker runtime and deadline fencing corrections

Status: implementation prepared; GPU/model acceptance remains pending.

## Corrections

The fixed training dispatcher binds its systemd working directory and
`PYTHONPATH` to the exact source root discovered from the installed CLI module.
The service therefore imports the same trusted worker source regardless of the
caller's shell directory or systemd user-manager defaults. The CPU-only trusted
maintenance parent retains its fixed principal, `NoNewPrivileges`, and all
resource ceilings, while omitting mount-namespace properties that remap host
path owners to UID 65534 and make the existing lexical shared-DB validator
reject `/home` ancestors. Its DB path is still validated before dispatch and
again inside the parent. The separate GPU training child retains its offline,
read-only, private-temporary-directory sandbox.

Child-unit inspection has two distinct outcomes: a successful `systemctl show`
with `LoadState=not-found` is explicit evidence that a unit is absent; any
nonzero `systemctl` exit is an inspection failure. Inspection failure blocks
drain and lease release. It is never treated as an absent child.

The QLoRA child's `RuntimeMaxSec` and parent execution deadline are derived
from the remaining immutable scheduler inference/total deadline. The worker
reserves the scheduler's existing maximum 30-second drain window and starts no
child when less than one second of execution remains after that reserve. The
shared scheduler limits are not extended. On deadline expiry, the worker
stops only the persisted child generation and records no measurement unless a
valid receipt was observed before the cutoff and the unit/GPU drain succeeds.

## Validation boundary

Focused tests cover working-directory/PYTHONPATH binding and the parent/child
sandbox boundary, inspection
errors versus explicit not-found, drain-verifier fail-closed behavior, child
runtime calculation against an inference deadline, and no-launch when the
drain reserve cannot be met. A separate bounded systemd CPU import probe checks
the exact project root and shared DB path validator in the trusted parent
context, with model libraries unimported. An earlier strict-mount probe failed
closed because systemd presented host path owners as UID 65534; it did not
weaken path validation. No GPU, model weights, shared GPU database, PostgreSQL,
or Docker is used by these checks. Ledger unit tests use private SQLite fixtures.
The actual QLoRA path and post-training local S1 smoke still require root
coordination and are not accepted by this supplement.
