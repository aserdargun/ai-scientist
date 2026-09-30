"""Final layout-only console QA; does not submit any jobs or research runs."""

import json
import os
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs/ai-scientist/ui"
BASE = "http://127.0.0.1:8788"


def main() -> None:
    requests = set()
    record = {"schema": "local-console-final-visual.v1", "created_at": datetime.now(UTC).isoformat(),
              "desktop": [1505, 1045], "mobile": [390, 844], "jobs_started": 0}
    record["app_sha256"] = hashlib.sha256((ROOT / "console/web/src/ui/App.tsx").read_bytes()).hexdigest()
    record["dist_index_sha256"] = hashlib.sha256((ROOT / "console/web/dist/index.html").read_bytes()).hexdigest()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
        page = browser.new_page(viewport={"width":1505,"height":1045}, device_scale_factor=1)
        page.on("request", lambda req: requests.add(urlsplit(req.url).netloc))
        page.goto(BASE, wait_until="networkidle")
        expect(page.get_by_text("10 / 22", exact=True)).to_be_visible()
        record["computed_styles"] = page.evaluate("""() => ({
          background:getComputedStyle(document.body).backgroundColor,
          rootBackground:getComputedStyle(document.documentElement).backgroundColor,
          headerSize:getComputedStyle(document.querySelector('h1')).fontSize,
          headerWeight:getComputedStyle(document.querySelector('h1')).fontWeight,
          sidebarWidth:document.querySelector('.sidebar').getBoundingClientRect().width,
          title:document.querySelector('h1').textContent,
          subtitle:document.querySelector('.page-header p').textContent,
          nav:[...document.querySelectorAll('.side-nav button')].map(x=>x.textContent),
          horizontalOverflow:document.documentElement.scrollWidth>innerWidth
        })""")
        assert not record["computed_styles"]["horizontalOverflow"]
        page.screenshot(path=str(OUT / "console-desktop.png"))
        page.screenshot(path=str(OUT / "console-desktop-full.png"), full_page=True)
        record["above_fold_text"] = page.locator("body").inner_text()
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_timeout(100)
        for name in ("Genel bakış", "Kabul maddeleri", "Deneyler", "Sistem"):
            control = page.get_by_role("button", name=name, exact=True)
            expect(control).to_be_visible()
            box = control.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(OUT / "console-mobile.png"), full_page=True)
        record["all_mobile_navigation_visible"] = True
        record["mobile_no_overflow"] = True
        record["browser_origins"] = sorted(requests)
        assert requests == {"127.0.0.1:8788"}
        browser.close()
    record["status"] = "passed"
    (ROOT / "docs/ai-scientist/review-evidence/local-console-final-visual.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    )
    print(json.dumps({"status":"passed", "jobs_started":0, "all_mobile_navigation_visible":True,
                      "browser_origins":sorted(requests)},ensure_ascii=False))


if __name__ == "__main__":
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "data/runtime/console-browser"))
    main()
