"""End-to-end tests over the real HTTP server and JSON API."""

import json
import tempfile
import threading
import time
import unittest
import urllib.request

from relocheck.demo import seg_of_order_no
from relocheck.server import create_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="relochk-api-")
        cls.httpd, cls.ctx = create_server(
            host="127.0.0.1", port=0, demo_dir=cls.dir, auto_writer=False
        )
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        if cls.ctx.writer:
            cls.ctx.writer.close()
        cls.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def _post(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self.base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())

    def _wait_run(self, run_id, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, body = self._get(f"/api/runs/{run_id}")
            if body["status"] == "done":
                return body
            time.sleep(0.05)
        raise AssertionError("run did not finish")

    def test_health_and_context(self):
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        _, ctx = self._get("/api/context")
        self.assertEqual(ctx["tables"][0]["business_key"], ["order_no"])

    def test_index_html_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("relocheck", html.lower())

    def test_clean_run_finds_seeded_differences(self):
        _, created = self._post("/api/runs", {"mode": "live"})
        run = self._wait_run(created["run_id"])
        self.assertEqual(run["totals"]["deferred"], 0)
        self.assertGreater(run["totals"]["diff"], 0)
        self.assertIn(run["totals"]["overall"], ("differences_found",))

    def test_lag_segment_deferred_then_recheck_definitive(self):
        seg = seg_of_order_no("HOT-09")
        seq, at, payload = self.ctx.writer._write_a("HOT-09")

        _, created = self._post("/api/runs", {"mode": "live"})
        run = self._wait_run(created["run_id"])
        self.assertGreaterEqual(run["totals"]["deferred"], 1)
        self.assertNotEqual(run["totals"]["overall"], "verified_equal")

        # segment detail carries timeline + evidence
        _, detail = self._get(f"/api/segment?run_id={run['id']}&table=orders&segment={seg}")
        self.assertEqual(detail["verdict"], "deferred")
        self.assertTrue(detail["timeline"])

        # B catches up -> recheck gives a definitive verdict
        self.ctx.writer._write_b("HOT-09", seq, at, payload)
        _, rechecked = self._post("/api/recheck", {
            "run_id": run["id"], "table": "orders", "segment": seg,
        })
        self.assertIn(rechecked["verdict"], ("match", "diff"))
        self.assertTrue(rechecked["definitive"])

    def test_deferred_payload_never_says_match(self):
        seg = seg_of_order_no("HOT-10")
        self.ctx.writer._write_a("HOT-10")
        _, created = self._post("/api/runs", {"mode": "live"})
        run = self._wait_run(created["run_id"])
        for table, segs in run["tables"].items():
            for s in segs:
                if s and s["verdict"] == "deferred":
                    self.assertNotEqual(s["verdict"], "match")
                    self.assertIn(s["provisional_content"], ("match", "diff"))


if __name__ == "__main__":
    unittest.main()
