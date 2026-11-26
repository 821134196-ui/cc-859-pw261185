"""Consistency protocol tests.

These verify the central safety property: a segment whose scan overlaps
continuous writes can never be reported as definitively equal — even when its
provisional row-by-row comparison happens to show equality — and deferred
segments survive aggregate accounting as "not verified".
"""

import os
import tempfile
import unittest

from relocheck.demo import DemoWriter, demo_paths, init_demo, seg_of_order_no
from relocheck.manager import RunManager
from relocheck.sources import open_source
from relocheck.consistency import scan_segment


class DemoTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="relochk-cons-")
        self.old, self.new, _ = demo_paths(self.dir)
        self.cfg = init_demo(self.old, self.new)
        self.tcfg = self.cfg.tables[0]
        self.writer = DemoWriter(self.cfg, self.old, self.new)

    def tearDown(self):
        self.writer.close()

    def scan(self, seg, **kw):
        sa = open_source(self.old, "A")
        sb = open_source(self.new, "B")
        try:
            return scan_segment(self.tcfg, sa, sb, seg, **kw)
        finally:
            sa.close(); sb.close()

    def clean_hot_target(self):
        """Return (segment, hot_order_no) for a segment whose baseline is match."""
        for seg in range(self.tcfg.segment_count):
            if self.scan(seg).verdict != "match":
                continue
            for i in range(self.tcfg.segment_count * 3):
                key = f"HOT-{i:02d}"
                if seg_of_order_no(key, self.tcfg.segment_count) == seg:
                    return seg, key
        raise AssertionError("no clean segment containing a HOT key")


class StableBaseline(DemoTestBase):
    def test_every_segment_definitive_when_idle_and_aligned(self):
        for seg in range(self.tcfg.segment_count):
            r = self.scan(seg)
            self.assertIn(r.verdict, ("match", "diff"))
            self.assertTrue(r.definitive)
            self.assertEqual(r.basis, "aligned_event_token")

    def test_seeded_differences_are_found(self):
        mgr = RunManager(self.cfg)
        rec = mgr.create_run(run_in_background=False)
        t = rec.totals()
        self.assertEqual(t["deferred"], 0)
        self.assertGreater(t["diff"], 0)
        self.assertEqual(t["missing_b"], 2)
        self.assertEqual(t["extra_b"], 2)
        self.assertEqual(t["duplicate_groups"], 3)
        self.assertEqual(t["overall"], "differences_found")


class DeferralNeverEqual(DemoTestBase):
    def test_write_during_a_read_defers_even_when_provisionally_equal(self):
        seg, key = self.clean_hot_target()

        def hook(stage, sa, sb):
            # A is mutated only AFTER its rows were read, so the compared
            # snapshots A(old) and B(old) are identical — provisional MATCH.
            if stage == "after_a_read":
                self.writer._write_a(key)

        r = self.scan(seg, hook=hook)
        self.assertEqual(r.verdict, "deferred")
        self.assertFalse(r.definitive)
        self.assertTrue(
            r.candidate.content_equal,
            "test setup wants a provisionally equal candidate",
        )
        # The guard: provisional equality must not surface as a match verdict.
        self.assertNotEqual(r.verdict, "match")
        self.assertIn(r.reason_code, ("verify_changed", "write_during_scan"))

    def test_lagging_side_defers_with_event_lag(self):
        seg = seg_of_order_no("HOT-02")
        seq, _, _ = self.writer._write_a("HOT-02")  # A ahead, B behind
        r = self.scan(seg)
        self.assertEqual(r.verdict, "deferred")
        self.assertEqual(r.reason_code, "event_lag")
        self.assertIsNotNone(r.timeline)
        codes = [e.code for e in r.timeline]
        self.assertIn("lag_observed", codes)

    def test_deferred_segment_not_counted_as_match_in_run_totals(self):
        mgr = RunManager(self.cfg)
        # Force one segment into A-ahead lag for the whole run.
        self.writer._write_a("HOT-03")
        rec = mgr.create_run(run_in_background=False)
        t = rec.totals()
        self.assertGreaterEqual(t["deferred"], 1)
        # match count must equal the number of segments literally verdict=match
        literal_matches = sum(
            1 for arr in rec.reports.values() for r in arr if r and r.verdict == "match"
        )
        self.assertEqual(t["match"], literal_matches)
        # and the overall conclusion can never be a clean verified_equal
        self.assertNotEqual(t["overall"], "verified_equal")
        self.assertIn(t["overall"], ("incomplete_deferred", "incomplete_with_diffs"))

    def test_provisionally_equal_deferred_is_counted_by_aggregate(self):
        mgr = RunManager(self.cfg)
        rec = mgr.create_run(run_in_background=False)
        seg, key = self.clean_hot_target()

        def _hook(stage, sa, sb):
            if stage == "after_a_read":
                self.writer._write_a(key)

        deferred_equal = self.scan(seg, hook=_hook)
        self.assertEqual(deferred_equal.verdict, "deferred")
        self.assertTrue(deferred_equal.candidate.content_equal)
        rec.reports["orders"][seg] = deferred_equal

        t = rec.totals()
        self.assertGreaterEqual(t["provisional_equal_among_deferred"], 1)
        # It still must not be counted as a definitive match.
        self.assertEqual(t["match"], sum(
            1 for arr in rec.reports.values() for r in arr if r and r.verdict == "match"))
        self.assertNotEqual(t["overall"], "verified_equal")

    def test_recheck_after_catchup_becomes_definitive_match_on_clean_seg(self):
        mgr = RunManager(self.cfg)
        rec = mgr.create_run(run_in_background=False)
        seg, key = self.clean_hot_target()
        seq, at, payload = self.writer._write_a(key)
        before = mgr.recheck(rec.id, "orders", seg)
        self.assertEqual(before.verdict, "deferred")
        self.assertEqual(before.reason_code, "event_lag")
        # B replays the same shared event sequence / logical values.
        self.writer._write_b(key, seq, at, payload)
        after = mgr.recheck(rec.id, "orders", seg)
        self.assertEqual(after.verdict, "match",
                         "aligned replay on a clean segment must verify equal")
        self.assertTrue(after.definitive)
        self.assertTrue(any(h["verdict"] == "deferred" for h in after.history))

    def test_recheck_can_defer_again_when_writes_resume(self):
        mgr = RunManager(self.cfg)
        rec = mgr.create_run(run_in_background=False)
        seg, key = self.clean_hot_target()

        self.assertEqual(mgr.recheck(rec.id, "orders", seg).verdict, "match")

        seq, at, payload = self.writer._write_a(key)
        self.assertEqual(mgr.recheck(rec.id, "orders", seg).verdict, "deferred")

        self.writer._write_b(key, seq, at, payload)
        self.assertEqual(mgr.recheck(rec.id, "orders", seg).verdict, "match")

        # A fresh write means the next recheck is undecidable again — a
        # previously-good verdict must not make the UI trust this scan.
        self.writer._write_a(key)
        again = mgr.recheck(rec.id, "orders", seg)
        self.assertEqual(again.verdict, "deferred")
        self.assertTrue(len(again.history) >= 3)


class SnapshotMode(DemoTestBase):
    def test_snapshot_with_aligned_token_is_definitive(self):
        token = int(self.writer._ca.execute("SELECT MAX(seq) FROM sync_events").fetchone()[0])
        r = self.scan(0, mode="snapshot", snapshot_token=token)
        self.assertIn(r.verdict, ("match", "diff"))
        self.assertTrue((r.basis or "").startswith("snapshot_token"))

    def test_snapshot_token_not_applied_on_b_defers(self):
        # Advertise a token higher than B has applied.
        a_max = int(self.writer._ca.execute("SELECT MAX(seq) FROM sync_events").fetchone()[0])
        self.writer._write_a("HOT-07")
        r = self.scan(0, mode="snapshot", snapshot_token=a_max + 1)
        self.assertEqual(r.verdict, "deferred")
        self.assertEqual(r.reason_code, "snapshot_not_aligned")


if __name__ == "__main__":
    unittest.main()
