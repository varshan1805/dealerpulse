"""Browser tests for the dashboard.

These need Playwright and a Chromium build, which the unit tests do not, so the
whole module skips when either is missing:

    pip install playwright && playwright install chromium
    python -m unittest tests.test_dashboard -v

What they cover:
  * live mode   - ?api=<url> fetches and renders real API payloads
  * sample mode - no ?api falls back to the bundled synthetic data
  * degraded    - with Chart.js unreachable, KPIs and tables STILL render

The degraded case is a regression test. Chart.js is loaded from a CDN, and
before the fallback existed a failed load threw ReferenceError out of render(),
leaving the viewer with an empty page and the raw text "Chart is not defined".
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "dashboard", "index.html")
EVIDENCE = os.path.join(ROOT, "docs", "evidence")

try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT = True
except ImportError:  # pragma: no cover
    _PLAYWRIGHT = False

# Mirrors the CORS config in template.yaml: AllowOrigins ["*"], GET only.
CHART_STUB = """
window.__charts = [];
window.Chart = function (el, cfg) {
  window.__charts.push({canvas: el ? el.id : null, type: cfg.type,
    labels: (cfg.data && cfg.data.labels) || [],
    lens: ((cfg.data && cfg.data.datasets) || []).map(d => (d.data || []).length)});
  return {destroy() {}};
};
window.Chart.defaults = {color: '', borderColor: ''};
"""


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, payloads, *a, **kw):
        self.payloads = payloads
        super().__init__(*a, **kw)

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.rstrip("/").rsplit("/", 1)[-1]
        if path in self.payloads:
            self._send(200, self.payloads[path])
        else:
            self._send(404, '{"error":"not found"}')

    def log_message(self, *a):
        pass


@unittest.skipUnless(_PLAYWRIGHT, "playwright not installed")
class DashboardTests(unittest.TestCase):
    server = None
    thread = None
    api = ""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(EVIDENCE, "api_metrics_response.json")) as fh:
            metrics = fh.read()
        with open(os.path.join(EVIDENCE, "api_runs_response.json")) as fh:
            runs = fh.read()
        payloads = {"metrics": metrics, "runs": runs, "health": '{"status":"ok"}'}
        cls.server = HTTPServer(("127.0.0.1", 0), partial(_Handler, payloads))
        cls.api = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.expected = json.loads(metrics)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()

    def _load(self, query="", block_cdn=False):
        """Render the page and return a snapshot of what the viewer would see."""
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1600})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            if block_cdn:
                page.route("**cdn.jsdelivr.net**", lambda r: r.abort())
            else:
                page.route("**cdn.jsdelivr.net**", lambda r: r.fulfill(
                    status=200, content_type="application/javascript", body=CHART_STUB))
            page.goto("file://%s%s" % (PAGE, query), wait_until="load")
            page.wait_for_timeout(1500)
            snap = page.evaluate("""() => ({
                status: document.getElementById('status').textContent.trim(),
                bannerShown: getComputedStyle(document.getElementById('banner')).display !== 'none',
                banner: document.getElementById('banner').textContent.trim(),
                kpis: Object.fromEntries(Array.from(document.querySelectorAll('#kpis .kpi'))
                        .map(d => [d.querySelector('.label').textContent.trim(),
                                   d.querySelector('.value').textContent.trim()])),
                people: document.querySelectorAll('#people tbody tr').length,
                overdue: document.querySelectorAll('#overdue tbody tr').length,
                runs: document.querySelectorAll('#runs tbody tr').length,
                charts: window.__charts || [],
                fallbacks: document.querySelectorAll('.chart-fallback').length,
                scrollW: document.documentElement.scrollWidth
            })""")
            snap["pageErrors"] = errors
            browser.close()
            return snap

    def test_live_mode_renders_api_payload(self):
        snap = self._load("?api=" + self.api)
        self.assertEqual(snap["pageErrors"], [])
        self.assertTrue(snap["status"].startswith("live"), snap["status"])
        self.assertFalse(snap["bannerShown"])
        totals = self.expected["totals"]
        self.assertEqual(snap["kpis"]["Total leads"], str(totals["leads"]))
        self.assertEqual(snap["kpis"]["Sold (booked + delivered)"], str(totals["sold"]))
        self.assertEqual(snap["overdue"], len(self.expected["overdue_followups"]["top"]))
        self.assertEqual(snap["people"], len(self.expected["salespeople"]))
        self.assertEqual(snap["runs"], 1)

    def test_every_chart_gets_matching_labels_and_data(self):
        snap = self._load("?api=" + self.api)
        self.assertEqual(len(snap["charts"]), 4)
        for c in snap["charts"]:
            for n in c["lens"]:
                self.assertEqual(n, len(c["labels"]),
                                 "%s: %d points for %d labels" % (c["canvas"], n, len(c["labels"])))

    def test_sample_mode_without_api_param(self):
        snap = self._load()
        self.assertEqual(snap["pageErrors"], [])
        self.assertEqual(snap["status"], "sample data")
        self.assertTrue(snap["bannerShown"])
        self.assertEqual(len(snap["charts"]), 4)
        self.assertGreater(snap["people"], 0)

    def test_tables_still_render_when_chartjs_cannot_load(self):
        """Regression: a missing Chart.js must not blank the page."""
        snap = self._load("?api=" + self.api, block_cdn=True)
        self.assertEqual(snap["pageErrors"], [])
        self.assertTrue(snap["status"].startswith("live"), snap["status"])
        self.assertEqual(snap["people"], len(self.expected["salespeople"]))
        self.assertEqual(snap["overdue"], len(self.expected["overdue_followups"]["top"]))
        self.assertEqual(snap["fallbacks"], 4)
        self.assertIn("Charts could not load", snap["banner"])
        self.assertNotIn("not defined", snap["banner"])

    def test_no_horizontal_scroll(self):
        self.assertLessEqual(self._load("?api=" + self.api)["scrollW"], 1440)


if __name__ == "__main__":
    unittest.main()
