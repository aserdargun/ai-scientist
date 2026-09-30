"""Focused browser regression for delayed stop; all experiment replies are fixtures."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime

from playwright.sync_api import sync_playwright

from review_local_console import BASE, ROOT, browser_fixture_workflow


def main() -> int:
    record = {
        "schema": "local-console-stop-review.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "method": "Playwright Chromium; intercepted experiment responses",
        "real_upstream_runs_created": 0,
        "cpu_checks_started": 0,
        "app_sha256": hashlib.sha256((ROOT / "console/web/src/ui/App.tsx").read_bytes()).hexdigest(),
        "dist_index_sha256": hashlib.sha256((ROOT / "console/web/dist/index.html").read_bytes()).hexdigest(),
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
            context = browser.new_context()
            response = context.request.get(BASE + "/console-api/overview")
            assert response.status == 200
            html = context.request.get(BASE).text()
            assert html == (ROOT / "console/web/dist/index.html").read_text()
            record["fixture"] = browser_fixture_workflow(browser, response.json())
            browser.close()
        record["exit_code"] = 0
        record["status"] = "passed"
    except Exception as exc:
        record["exit_code"] = 1
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
    output = ROOT / "docs/ai-scientist/review-evidence/local-console-stop-review.json"
    output.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False))
    return record["exit_code"]


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data/runtime/console-browser"))
    raise SystemExit(main())
