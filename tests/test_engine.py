"""Tests for alignment, duplicate keys, null/precision diffs and segmentation."""

import os
import sqlite3
import tempfile
import unittest

from relocheck.config import TableConfig
from relocheck.engine import resolve_table, compare_segment_rows, segment_index, key_of_raw
from relocheck.sources import SQLiteSource


def make_db(path, ddl, rows):
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    cols = rows[0].keys() if rows else []
    if rows:
        ph = ",".join("?" for _ in cols)
        conn.executemany(
            f'INSERT INTO t ({",".join(cols)}) VALUES ({ph})',
            [tuple(r[c] for c in cols) for r in rows],
        )
    conn.commit()
    conn.close()


DDL_A = "CREATE TABLE t (id INTEGER PRIMARY KEY, bk TEXT, v TEXT, n DECIMAL(10,2), r DOUBLE)"
DDL_B = "CREATE TABLE t (id INTEGER PRIMARY KEY, bk TEXT, v TEXT, n DECIMAL(10,4), r DOUBLE)"


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="relochk-engine-")
        self.pa = os.path.join(self.dir, "a.db")
        self.pb = os.path.join(self.dir, "b.db")
        self.tcfg = TableConfig(name="t", business_key=["bk"], segment_count=4)

    def compare(self, rows_a, rows_b):
        make_db(self.pa, DDL_A, rows_a)
        make_db(self.pb, DDL_B, rows_b)
        sa = SQLiteSource(self.pa, "A")
        sb = SQLiteSource(self.pb, "B")
        ra = resolve_table(self.tcfg, sa)
        rb = resolve_table(self.tcfg, sb)
        # compare all segments merged by running each segment and joining
        reports = []
        for seg in range(4):
            raw_a = [r for r in sa.fetch_all("t")
                     if segment_index(key_of_raw(r, ra), 4) == seg]
            raw_b = [r for r in sb.fetch_all("t")
                     if segment_index(key_of_raw(r, rb), 4) == seg]
            reports.append(compare_segment_rows(seg, 4, raw_a, raw_b, ra, self.tcfg, rb))
        sa.close(); sb.close()
        return reports

    def all_evidence(self, reports):
        out = {}
        for rep in reports:
            for ke in rep.key_evidence:
                out[ke.key.display()] = ke
        return out


class BusinessKeyAlignment(EngineTestBase):
    def test_unstable_physical_id_ignored(self):
        rows_a = [dict(id=1, bk="K1", v="x", n=1.0, r=0.5),
                  dict(id=2, bk="K2", v="y", n=2.0, r=0.6)]
        rows_b = [dict(id=9001, bk="K1", v="x", n=1.0000, r=0.5),
                  dict(id=9002, bk="K2", v="y", n=2.0000, r=0.6)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        self.assertEqual(ev["K1"].status, "match")
        self.assertEqual(ev["K2"].status, "match")

    def test_missing_and_extra(self):
        base = dict(v="x", n=1.0, r=0.5)
        rows_a = [dict(id=1, bk="KA", **base), dict(id=2, bk="ONLY_A", **base)]
        rows_b = [dict(id=50, bk="KA", **base), dict(id=51, bk="ONLY_B", **base)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        self.assertEqual(ev["ONLY_A"].status, "missing_b")
        self.assertEqual(ev["ONLY_B"].status, "extra_b")


class DuplicateKeys(EngineTestBase):
    def test_identical_duplicate_multiset_is_match(self):
        base = dict(v="x", n=1.0, r=0.5)
        rows = [dict(id=1, bk="DUP", **base), dict(id=2, bk="DUP", v="y", n=2.0, r=0.7)]
        ev = self.all_evidence(self.compare(rows, [dict(r) for r in rows]))
        ke = ev["DUP"]
        self.assertEqual(ke.status, "match")
        self.assertTrue(ke.duplicate_group)
        self.assertEqual((ke.multiplicity_a, ke.multiplicity_b), (2, 2))

    def test_extra_duplicate_row_detected_not_first_row_picked(self):
        r1 = dict(id=1, bk="DUP", v="x", n=1.0, r=0.5)
        r2 = dict(id=2, bk="DUP", v="y", n=2.0, r=0.7)
        r3 = dict(id=3, bk="DUP", v="z_extra", n=3.0, r=0.9)
        ev = self.all_evidence(self.compare([r1, r2], [dict(r1), dict(r2), r3]))
        ke = ev["DUP"]
        self.assertNotEqual(ke.status, "match")
        self.assertEqual((ke.multiplicity_a, ke.multiplicity_b), (2, 3))
        dispositions = [rd.disposition for rd in ke.row_evidence]
        self.assertIn("only_b", dispositions)

    def test_same_count_different_content(self):
        r1 = dict(id=1, bk="DUP", v="x", n=1.0, r=0.5)
        r2 = dict(id=2, bk="DUP", v="y", n=2.0, r=0.7)
        b1 = dict(r1); b2 = dict(r2); b2["v"] = "CHANGED"
        ev = self.all_evidence(self.compare([r1, r2], [b1, b2]))
        ke = ev["DUP"]
        self.assertEqual(ke.status, "content_diff")
        codes = [d.code for rd in ke.row_evidence for d in rd.column_diffs if not d.equal]
        self.assertIn("value_mismatch", codes)


class NullAndPrecision(EngineTestBase):
    def test_null_vs_empty_string_flagged(self):
        rows_a = [dict(id=1, bk="K", v=None, n=1.0, r=0.5)]
        rows_b = [dict(id=700, bk="K", v="", n=1.0, r=0.5)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        ke = ev["K"]
        self.assertEqual(ke.status, "content_diff")
        diffs = [(d.column, d.code) for rd in ke.row_evidence for d in rd.column_diffs if not d.equal]
        self.assertIn(("v", "null_mismatch"), diffs)

    def test_null_both_match(self):
        rows = [dict(id=1, bk="K", v=None, n=None, r=None)]
        ev = self.all_evidence(self.compare(rows, [dict(id=800, **{k: v for k, v in rows[0].items() if k != "id"})]))
        self.assertEqual(ev["K"].status, "match")

    def test_decimal_scale_drift_is_soft_warning_by_default(self):
        rows_a = [dict(id=1, bk="K", v="x", n="12.30", r=0.5)]
        rows_b = [dict(id=900, bk="K", v="x", n="12.3000", r=0.5)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        ke = ev["K"]
        self.assertEqual(ke.status, "match")
        soft = [d.code for rd in ke.row_evidence for d in rd.column_diffs if d.equal]
        self.assertIn("equal_value_precision_drift", soft)

    def test_strict_precision_turns_drift_into_difference(self):
        self.tcfg.strict_precision = True
        rows_a = [dict(id=1, bk="K", v="x", n="12.30", r=0.5)]
        rows_b = [dict(id=900, bk="K", v="x", n="12.3000", r=0.5)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        self.assertEqual(ev["K"].status, "content_diff")

    def test_float_artifact_flagged(self):
        rows_a = [dict(id=1, bk="K", v="x", n=1.0, r=0.1 + 0.2)]
        rows_b = [dict(id=900, bk="K", v="x", n=1.0, r=0.3)]
        ev = self.all_evidence(self.compare(rows_a, rows_b))
        codes = [d.code for rd in ev["K"].row_evidence for d in rd.column_diffs if not d.equal]
        self.assertIn("value_mismatch", codes)


if __name__ == "__main__":
    unittest.main()
