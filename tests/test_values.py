"""Tests for type-aware value/key comparison semantics."""

import unittest
from decimal import Decimal

from relocheck.values import Key, Kind, parse_sql_type, make_typed


def tv(raw, sql_type=None):
    meta = parse_sql_type(sql_type) if sql_type else None
    return make_typed(raw, meta)


class NullSemantics(unittest.TestCase):
    def test_null_equals_null(self):
        r = tv(None).typed_equals(tv(None))
        self.assertTrue(r.equal)
        self.assertEqual(r.code, "both_null")

    def test_null_not_equal_empty_string(self):
        r = tv(None).typed_equals(tv(""))
        self.assertFalse(r.equal)
        self.assertEqual(r.code, "null_mismatch")

    def test_null_not_equal_text_null(self):
        self.assertFalse(tv(None).typed_equals(tv("NULL")).equal)
        self.assertFalse(tv(None).typed_equals(tv("null")).equal)

    def test_null_not_equal_zero(self):
        self.assertFalse(tv(None).typed_equals(tv(0)).equal)

    def test_null_canonical_differs_from_empty_text(self):
        self.assertNotEqual(tv(None).canonical(), tv("").canonical())
        self.assertNotEqual(tv(None).canonical(), tv("NULL").canonical())


class NumericPrecision(unittest.TestCase):
    def test_decimal_exact_equality_trailing_scale(self):
        a = tv("10.10", "DECIMAL(14,2)")
        b = tv("10.1000", "DECIMAL(14,4)")
        r = a.typed_equals(b)
        self.assertTrue(r.equal)
        self.assertEqual(r.code, "equal_value_precision_drift")
        self.assertEqual(r.precision_drift, (2, 4))

    def test_decimal_canonical_same_for_equal_magnitude(self):
        a = tv("10.10", "DECIMAL(14,2)")
        b = tv("10.1000", "DECIMAL(14,4)")
        self.assertEqual(a.canonical(), b.canonical())

    def test_near_precision_values_are_different(self):
        a = tv("0.100000", "DECIMAL(10,6)")
        b = tv("0.100001", "DECIMAL(10,6)")
        r = a.typed_equals(b)
        self.assertFalse(r.equal)
        self.assertEqual(r.code, "value_mismatch")

    def test_string_rounding_cannot_make_these_equal(self):
        # Both format to "0.10" at 2 decimal places — string formatting would
        # cobble equality; exact Decimal comparison must refuse.
        a = tv("0.104", "DECIMAL(10,3)")
        b = tv("0.096", "DECIMAL(10,3)")
        self.assertEqual(f"{Decimal('0.104'):.2f}", f"{Decimal('0.096'):.2f}")
        self.assertFalse(a.typed_equals(b).equal)

    def test_float_binary_artifact_not_pretended_equal(self):
        a = tv(0.1 + 0.2, "DOUBLE")
        b = tv(0.3, "DOUBLE")
        self.assertNotEqual(a.value, b.value)  # the trap is real
        r = a.typed_equals(b)
        self.assertFalse(r.equal)
        self.assertEqual(r.code, "value_mismatch")

    def test_declared_tolerance_makes_float_equal(self):
        from decimal import Decimal as D
        a = tv(0.1 + 0.2, "DOUBLE")
        b = tv(0.3, "DOUBLE")
        r = a.typed_equals(b, abs_tolerance=D("1e-9"))
        self.assertTrue(r.equal)
        self.assertEqual(r.code, "within_tolerance")

    def test_int_and_decimal_same_magnitude(self):
        self.assertTrue(tv(1).typed_equals(tv("1.00", "DECIMAL(10,2)")).equal)

    def test_bool_is_not_int(self):
        self.assertNotEqual(tv(True).kind, tv(1).kind)
        self.assertFalse(tv(True).typed_equals(tv(1)).equal)


class TypedKeys(unittest.TestCase):
    def test_int_one_and_text_one_are_different_keys(self):
        ki = Key((tv(1, "INTEGER"),))
        ks = Key((tv("1", "TEXT"),))
        self.assertNotEqual(ki, ks)
        self.assertNotEqual(hash(ki), hash(ks))
        self.assertNotEqual(ki.canonical(), ks.canonical())

    def test_leading_zero_text_keys_distinct(self):
        a = Key((tv("00421", "TEXT"),))
        b = Key((tv("421", "TEXT"),))
        self.assertNotEqual(a, b)

    def test_composite_key_equality(self):
        a = Key((tv("ORD-1", "TEXT"), tv(2, "INTEGER")))
        b = Key((tv("ORD-1", "TEXT"), tv(2, "INTEGER")))
        c = Key((tv("ORD-1", "TEXT"), tv(3, "INTEGER")))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class CanonicalEncoding(unittest.TestCase):
    def test_delimiter_values_do_not_collide(self):
        x = tv("a|T:1:b", "TEXT").canonical()
        y1 = tv("a", "TEXT").canonical()
        y2 = tv("b", "TEXT").canonical()
        self.assertNotEqual(x, y1 + "|" + y2)

    def test_wire_decimal_is_exact_string(self):
        w = tv("88.80", "DECIMAL(14,2)").to_wire()
        self.assertEqual(w["v"], "88.80")
        self.assertEqual(w["kind"], Kind.DECIMAL)


if __name__ == "__main__":
    unittest.main()
