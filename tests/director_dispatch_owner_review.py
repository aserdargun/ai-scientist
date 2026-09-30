"""Run the production Director claim path inside its run-bound systemd unit."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from lab import cli


class _Registry:
    def __init__(self, root: Path) -> None:
        self.entry = SimpleNamespace(
            suite_id="recovery.dispatch.review.v1",
            track="anomaly",
            program_version="director.v1",
            provider="fake-json",
            suite_manifest_sha256="a" * 64,
            scenario_sha256="b" * 64,
            provider_config_sha256=None,
            proposal_limit=1,
        )
        self.root = root

    def get(self, suite_id: str) -> SimpleNamespace:
        if suite_id != self.entry.suite_id:
            raise KeyError(suite_id)
        return self.entry

    def verify_entry(self, entry: SimpleNamespace) -> tuple[Path, Path]:
        if entry != self.entry:
            raise ValueError("review registry entry changed")
        fixture = self.root / "pyproject.toml"
        return fixture, fixture

    @staticmethod
    def entry_sha256(_entry: SimpleNamespace) -> str:
        return "c" * 64


class _IntentionalPreMeasurementFailure(RuntimeError):
    """Test-only failure after claim and before any experiment work."""


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True, type=UUID)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(cli.__file__).resolve().parents[1] != root.resolve():
        raise RuntimeError("review worker imported lab.cli outside the isolated worktree")

    cli.load_suite_registry = lambda *_args: _Registry(root)  # type: ignore[assignment]
    cli.load_fake_provider = lambda _path: [object()]  # type: ignore[assignment]

    def fail_before_measurement(_args: argparse.Namespace) -> dict[str, object]:
        raise _IntentionalPreMeasurementFailure("owned dispatch receipt review")

    cli._run_director = fail_before_measurement
    result = cli._dispatch_director_run_owned(args.run_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("state") == "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
