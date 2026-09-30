"""Exercise the live console in Chromium; never launch research or GPU work."""

from __future__ import annotations

import json
import copy
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/ai-scientist/ui"
BASE = "http://127.0.0.1:8788"


def browser_fixture_workflow(browser, real_overview) -> dict:
    """Verify form behavior with intercepted responses, never an upstream run."""
    data = copy.deepcopy(real_overview)
    data["lab"] = {"configured": True, "connected": True, "reason": None,
                   "model_runs_enabled": False,
                   "suites": [{"suite_id":"console.fixture", "track":"anomaly",
                               "program_version":"director.v1", "provider":"fake-json", "proposal_limit":2}]}
    run_id = "76ef8935-dfb0-4b7b-83bb-7c91ca49475f"
    run = {"run_id":run_id, "origin":"local", "state":"queued", "created_at":"2026-09-26T00:00:00Z",
           "updated_at":"2026-09-26T00:00:00Z", "stop_requested":False, "report_sha256":None}
    sent = []
    started = False
    stop_sent = False
    stop_status_polls = 0

    def route_handler(route):
        nonlocal started, stop_sent, stop_status_polls
        path = route.request.url.removeprefix(BASE + "/console-api")
        status = 200
        if path == "/overview":
            payload = data
        elif path == "/checks":
            payload = {"items":[]}
        elif path == "/runs" and route.request.method == "POST":
            sent.append(route.request.post_data_json)
            if len(sent) < 3:
                status, payload = 503, {"detail":"Fixture: retry behavior only; no run created."}
            else:
                started = True
                payload = {"run_id":run_id, "state":"queued", "reused":False}
        elif path == "/runs":
            payload = {"items":[run] if started else []}
        elif path == f"/runs/{run_id}/stop":
            stop_sent = True
            run.update(state="stop_requested", stop_requested=True)
            payload = run
        elif path == f"/runs/{run_id}/report":
            payload = {"run_id":run_id,"report_sha256":"b"*64,"report":{"fixture":"browser-only"}}
        elif path == f"/runs/{run_id}":
            if stop_sent:
                stop_status_polls += 1
                if stop_status_polls >= 2:
                    run.update(state="stopped", report_sha256="b"*64)
            payload = run
        else:
            status, payload = 404, {"detail":"Unknown fixture route"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))

    context = browser.new_context(viewport={"width":1505,"height":1045})
    page = context.new_page()
    page.route(BASE + "/console-api/**", route_handler)
    page.goto(BASE, wait_until="networkidle")
    page.get_by_role("button", name="Deneyler", exact=True).click()
    create = page.get_by_role("button", name="Yeni deney başlat", exact=True)
    expect(create).to_be_enabled()
    create.click()
    dialog = page.get_by_role("dialog")
    expect(dialog.get_by_label("Deney adedi")).to_have_attribute("max", "2")
    submit = dialog.get_by_role("button", name="Deneyi başlat", exact=True)
    submit.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Fixture")
    submit.click()
    expect(dialog.get_by_role("alert")).to_contain_text("Fixture")
    dialog.get_by_label("Deney adedi").fill("2")
    submit.click()
    expect(page.get_by_role("heading", name="Deney ayrıntısı", exact=True)).to_be_visible()
    assert len(sent) == 3
    assert sent[0]["idempotency_key"] == sent[1]["idempotency_key"]
    assert sent[1]["idempotency_key"] != sent[2]["idempotency_key"]
    assert sent[2]["budget"]["experiments"] == 2
    page.get_by_role("button", name="Deneyi durdur", exact=True).click()
    expect(page.get_by_role("dialog").get_by_text("Durdurma bekleniyor", exact=True)).to_be_visible()
    expect(page.get_by_role("dialog").get_by_role("button", name="Durdurma bekleniyor…", exact=True)).to_be_disabled()
    expect(page.get_by_role("dialog").get_by_text("Durduruldu", exact=True)).to_be_visible(timeout=10000)
    assert stop_sent
    assert stop_status_polls >= 2
    page.screenshot(path=str(OUT / "console-stop-fixture.png"), full_page=True)
    page.get_by_role("button", name="Kapat", exact=True).click()
    expect(page.get_by_role("button", name="Rapor", exact=True)).to_be_enabled(timeout=10000)
    page.get_by_role("button", name="Rapor", exact=True).click()
    expect(page.get_by_role("heading", name="Doğrulanmış rapor", exact=True)).to_be_visible()
    expect(page.locator(".report-view pre")).to_contain_text("browser-only")
    context.close()
    return {"fixture_only":True, "upstream_runs_created":0, "start_attempts":len(sent),
            "same_payload_same_key":True, "changed_payload_new_key":True,
            "fake_provider_enabled_without_model_flag":True, "suite_limit":2,
            "stop_requested_to_stopped": True, "stop_status_polls": stop_status_polls,
            "stop_and_report":True}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    result = {"schema": "local-console-browser-review.v1", "started_at": datetime.now(UTC).isoformat(),
              "checks": {}, "method": "Playwright Chromium; Browser/IAB unavailable",
              "model_or_research_started": False, "viewport": {"width": 1505, "height": 1045}}
    checks = result["checks"]
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
            page = browser.new_page(viewport=result["viewport"], device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            overview_response = page.request.get(BASE + "/console-api/overview")
            assert overview_response.status == 200
            data = overview_response.json()
            counts = data["acceptance"]
            assert counts["total"] == len(counts["items"]) == 22
            assert sum(counts[key] for key in ("passed", "partial", "open")) == 22
            result["acceptance"] = {key: counts[key] for key in ("total", "passed", "partial", "open", "source_sha256")}
            checks["real_acceptance_source"] = True
            page.goto(BASE, wait_until="networkidle")
            expect(page.get_by_role("heading", name="Araştırma kontrol merkezi")).to_be_visible()
            expect(page.get_by_text(f'{counts["passed"]} / {counts["total"]}', exact=True)).to_be_visible()
            assert not page.get_by_text("SWAPP · YEREL KONSOL", exact=True).count()
            checks["overview_real_counts_and_copy"] = True
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(OUT / "console-desktop.png"))
            result["above_fold_text"] = page.locator("body").inner_text()
            page.get_by_role("button", name="Kabul maddeleri", exact=True).click()
            expect(page.get_by_label("Duruma göre filtrele")).to_be_visible()
            for state, count_key in (("open", "open"), ("partial", "partial"), ("passed", "passed")):
                page.get_by_label("Duruma göre filtrele").select_option(state)
                expect(page.locator(".acceptance-row")).to_have_count(counts[count_key])
            checks["all_three_status_filters"] = True
            page.get_by_label("Duruma göre filtrele").select_option("all")
            expect(page.locator(".acceptance-row")).to_have_count(22)
            page.locator(".acceptance-row").filter(has_text="M0.2").first.click()
            dialog = page.get_by_role("dialog")
            expect(dialog).to_be_visible()
            expect(dialog.get_by_text("Kanıtlar", exact=True)).to_be_visible()
            if dialog.locator(".evidence-link").count():
                dialog.locator(".evidence-link").first.click()
                expect(dialog.locator(".evidence-content pre")).not_to_be_empty()
                checks["evidence_document_rendered"] = True
            else:
                raise AssertionError("M0.2 has no evidence link")
            page.screenshot(path=str(OUT / "console-evidence.png"), full_page=True)
            page.keyboard.press("Escape")
            expect(page.get_by_role("dialog")).to_have_count(0)
            checks["detail_and_escape"] = True
            evidence_ref = next(item["evidence"][0] for item in counts["items"] if item["evidence"])
            page.request.get(BASE + "/console-api/overview")
            evidence_response = page.request.get(BASE + evidence_ref["url"])
            assert evidence_response.status == 200
            assert len(evidence_response.json()["sha256"]) == 64
            checks["evidence_survives_refresh"] = True
            assert page.request.get(BASE + "/console-api/evidence/not-registered").status == 404
            assert page.request.post(BASE + "/console-api/checks", data={}).status == 403
            headers = {"Origin": BASE, "X-Lab-Console": "1"}
            assert page.request.post(BASE + "/console-api/checks", data={"command":"anything"}, headers=headers).status == 422
            checks["bounded_actions_and_origin_guard"] = True
            page.get_by_role("button", name="Deneyler", exact=True).click()
            if not data["lab"]["connected"]:
                expect(page.get_by_role("button", name="Yeni deney başlat")).to_be_disabled()
                page.get_by_label("Koşu UUID’si").fill("ee89492f-9788-485f-8317-757396340dcf")
                page.get_by_role("button", name="İzle", exact=True).click()
                expect(page.get_by_role("status")).to_be_visible()
                runs = page.request.get(BASE + "/console-api/runs").json()
                assert not runs["items"]
                checks["disconnected_api_does_not_invent_run"] = True
            page.get_by_role("button", name="Genel bakış", exact=True).click()
            page.get_by_role("button", name="Kontrolü çalıştır", exact=True).first.click()
            end = time.monotonic() + 140
            final = None
            while time.monotonic() < end:
                all_checks = page.request.get(BASE + "/console-api/checks").json()["items"]
                if all_checks:
                    final = all_checks[-1]
                    if final["state"] in {"passed", "failed"}:
                        break
                page.wait_for_timeout(500)
            assert final and final["state"] == "passed" and final["exit_code"] == 0, final
            assert "passed" in final["output"]
            result["real_cpu_check"] = final
            checks["real_cpu_smoke_exit_zero"] = True
            page.get_by_role("button", name="Sistem", exact=True).click()
            expect(page.get_by_text("Çıkış kodu: 0", exact=False).first).to_be_visible(timeout=10000)
            page.screenshot(path=str(OUT / "console-check.png"), full_page=True)
            checks["cpu_result_visible"] = True
            after = page.request.get(BASE + "/console-api/overview").json()["acceptance"]
            assert (after["passed"], after["partial"], after["open"]) == (counts["passed"], counts["partial"], counts["open"])
            checks["smoke_does_not_change_acceptance"] = True
            page.get_by_role("button", name="Genel bakış", exact=True).click()
            page.set_viewport_size({"width":390,"height":844})
            page.wait_for_timeout(100)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile horizontal overflow"
            expect(page.get_by_role("heading", name="Araştırma kontrol merkezi")).to_be_visible()
            page.screenshot(path=str(OUT / "console-mobile.png"), full_page=True)
            checks["mobile_390px_no_overflow"] = True
            assert not errors, errors
            checks["no_browser_exceptions"] = True
            result["browser_form_fixture"] = browser_fixture_workflow(browser, data)
            checks["browser_form_fixture_only"] = True
            browser.close()
        result["status"] = "passed"
        code = 0
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    result["finished_at"] = datetime.now(UTC).isoformat()
    result["browser_errors"] = errors
    result["exit_code"] = code
    path = ROOT / "docs/ai-scientist/review-evidence/local-console-browser-review.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": result["status"], "checks": checks, "error":result.get("error"), "evidence":str(path)}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data/runtime/console-browser"))
    raise SystemExit(main())
