"""Replay retained real Scorer report bytes through the isolated AOS HTTP client."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import tempfile
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[3]
COPY = ROOT / "data/runtime/aos-coexistence/source-current"
sys.path.insert(0, str(COPY / "src"))

from aos.lab_external import LabApiClient  # noqa: E402


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def envelope(run_id, body):
    return {
        "run_id": str(run_id),
        "report_sha256": hashlib.sha256(canonical(body)).hexdigest(),
        "report": body,
        "verified_at": "2026-09-24T19:18:00+00:00",
    }


def main():
    source = COPY / "src/aos/lab_external.py"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    evidence_path = Path(__file__).with_name("director20-cli-review.json")
    retained_bytes = evidence_path.read_bytes()
    retained = json.loads(retained_bytes)
    body = retained["report"]["report_json"]
    run_id = UUID(body["run_id"])
    assert body["run_id"] == retained["run_id"]
    assert hashlib.sha256(canonical(body)).hexdigest() == retained["report"]["report_sha256"]
    actual = envelope(run_id, body)
    minimal = {"run_id": str(run_id), "status": "completed", "task_scores": []}
    wrong_run = {**minimal, "run_id": str(uuid4())}
    incorrect_hash = envelope(run_id, minimal)
    incorrect_hash["report_sha256"] = "0" * 64
    cases = (
        ("valid_minimal", envelope(run_id, minimal), True),
        ("retained_real_eighty_score_report", actual, True),
        ("wrong_inner_run_with_valid_hash", envelope(run_id, wrong_run), False),
        ("incorrect_report_hash", incorrect_hash, False),
    )
    token = secrets.token_urlsafe(32)
    serving = {"payload": b""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            if (
                self.path != f"/v1/runs/{run_id}/report"
                or self.headers.get("Authorization") != f"Bearer {token}"
            ):
                self.send_error(403)
                return
            raw = serving["payload"]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    observations = {}
    with tempfile.TemporaryDirectory(prefix="aos-report-wire-", dir=ROOT / "data/runtime") as tmp:
        token_path = Path(tmp) / "review.token"
        token_path.write_text(token)
        token_path.chmod(0o600)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = LabApiClient(f"http://127.0.0.1:{server.server_port}", token_path)
            for name, payload, should_accept in cases:
                serving["payload"] = canonical(payload)
                error_type = None
                try:
                    report = client.report(run_id)
                    accepted = report.report_sha256 == payload["report_sha256"]
                except (ValueError, RuntimeError) as exc:
                    accepted = False
                    error_type = type(exc).__name__
                observations[name] = {
                    "response_bytes": len(serving["payload"]),
                    "expected_accept": should_accept,
                    "actual_accept": accepted,
                    "error_type": error_type,
                    "passed": accepted == should_accept,
                }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
    record = {
        "checked_at": datetime.now(UTC).isoformat(),
        "scope": (
            "Isolated AOS production client against a controlled authenticated loopback HTTP "
            "server. The 80-score report is retained actual Director20/Scorer output; small "
            "identity/hash controls are fixtures. No new research, real Lab server, AOS service, "
            "model or GPU run."
        ),
        "python": sys.version.split()[0],
        "source_sha256": source_hash,
        "retained_evidence_sha256": hashlib.sha256(retained_bytes).hexdigest(),
        "retained_report_sha256": actual["report_sha256"],
        "retained_task_score_count": len(body["task_scores"]),
        "observations": observations,
        "source_unchanged": hashlib.sha256(source.read_bytes()).hexdigest() == source_hash,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "owned_http_server_stopped": not thread.is_alive(),
    }
    record["all_passed"] = (
        record["source_unchanged"] and record["owned_http_server_stopped"]
        and all(item["passed"] for item in observations.values())
    )
    Path(__file__).with_name("aos-report-transport-review.json").write_text(
        json.dumps(record, indent=2) + "\n"
    )
    print(json.dumps(record, indent=2))
    if not record["all_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
