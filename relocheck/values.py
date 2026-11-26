"""Typed cell values and type-aware equality / canonical encodings.

The single most important rule in this module: **equality is never decided by
comparing strings**.  Strings are only used afterwards to *render* or fingerprint
a value whose kind and exact numeric magnitude are already known.

Traps this closes:

* ``None`` (SQL ``NULL``) is its own kind.  It is equal only to another NULL and
  never to ``""``, ``"NULL"`` or ``0``.
* Exact numerics go through :class:`decimal.Decimal`, so neither binary-float
  noise nor textual formatting can manufacture equality.
* Keys carry their kind, so the integer ``1`` and the text ``"1"`` are different
  keys instead of collapsing into one aligned row.
"""

from __future__ import annotations

import base64
import datetime as _dt
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


class Kind:
    NULL = "null"
    BOOL = "bool"
    INT = "int"
    DECIMAL = "decimal"
    FLOAT = "float"
    TEXT = "text"
    BYTES = "bytes"
    DATE = "date"
    TIME = "time"
    DATETIME = "datetime"
    JSON = "json"


# Kinds that share ordering/comparison semantics.
EXACT_NUMERIC = {Kind.INT, Kind.DECIMAL}
NUMERIC = EXACT_NUMERIC | {Kind.FLOAT}

_TYPE_RE = re.compile(
    r"^\s*(?P<base>[A-Za-z ]+?)\s*(?:\(\s*(?P<p1>\d+)\s*(?:,\s*(?P<p2>-?\d+)\s*)?\))?\s*$"
)

# Map lower-cased SQL type spellings to a semantic family.
_TYPE_MAP = {
    "boolean": Kind.BOOL,
    "bool": Kind.BOOL,
    "tinyint": Kind.INT,  # note: tinyint(1) is often bool; declared meta can override
    "smallint": Kind.INT,
    "integer": Kind.INT,
    "int": Kind.INT,
    "int2": Kind.INT,
    "int4": Kind.INT,
    "int8": Kind.INT,
    "bigint": Kind.INT,
    "mediumint": Kind.INT,
    "serial": Kind.INT,
    "bigserial": Kind.INT,
    "numeric": Kind.DECIMAL,
    "decimal": Kind.DECIMAL,
    "number": Kind.DECIMAL,
    "money": Kind.DECIMAL,
    "float": Kind.FLOAT,
    "real": Kind.FLOAT,
    "double": Kind.FLOAT,
    "double precision": Kind.FLOAT,
    "float4": Kind.FLOAT,
    "float8": Kind.FLOAT,
    "binary_float": Kind.FLOAT,
    "binary_double": Kind.FLOAT,
    "varchar": Kind.TEXT,
    "char": Kind.TEXT,
    "text": Kind.TEXT,
    "clob": Kind.TEXT,
    "tinytext": Kind.TEXT,
    "mediumtext": Kind.TEXT,
    "longtext": Kind.TEXT,
    "nvarchar": Kind.TEXT,
    "nchar": Kind.TEXT,
    "string": Kind.TEXT,
    "blob": Kind.BYTES,
    "binary": Kind.BYTES,
    "varbinary": Kind.BYTES,
    "bytea": Kind.BYTES,
    "date": Kind.DATE,
    "time": Kind.TIME,
    "timestamp": Kind.DATETIME,
    "datetime": Kind.DATETIME,
    "timestamptz": Kind.DATETIME,
    "timestamp with time zone": Kind.DATETIME,
    "timestamp without time zone": Kind.DATETIME,
    "json": Kind.JSON,
    "jsonb": Kind.JSON,
}


@dataclass(frozen=True)
class TypeMeta:
    """Declared SQL type information for one column."""

    raw: str
    kind: str
    precision: Optional[int] = None
    scale: Optional[int] = None

    @property
    def strict_scale(self) -> Optional[int]:
        return self.scale if self.kind == Kind.DECIMAL else None


def parse_sql_type(declared: Optional[str]) -> Optional[TypeMeta]:
    if not declared:
        return None
    m = _TYPE_RE.match(declared)
    if not m:
        return TypeMeta(raw=declared, kind=Kind.TEXT)
    base = re.sub(r"\s+", " ", m.group("base").strip().lower())
    kind = _TYPE_MAP.get(base)
    if kind is None:
        # e.g. "character varying"
        kind = _TYPE_MAP.get(base.replace("character varying", "varchar"), Kind.TEXT)
    p1 = m.group("p1")
    p2 = m.group("p2")
    precision = int(p1) if p1 is not None else None
    scale = int(p2) if p2 is not None else None
    if kind == Kind.DECIMAL and scale is None and precision is not None:
        # DECIMAL(p) is SQL standard for scale 0.
        scale = 0
    return TypeMeta(raw=declared, kind=kind, precision=precision, scale=scale)


def _quantize(d: Decimal, scale: int) -> Decimal:
    quantum = Decimal(1).scaleb(-scale) if scale > 0 else Decimal(1)
    return d.quantize(quantum)


def make_typed(raw: Any, type_meta: Optional[TypeMeta] = None) -> "TypedValue":
    """Convert a driver-native value into a :class:`TypedValue`.

    ``type_meta`` is the *declared* column type.  It is used to decode
    SQLite's REAL-affinity DECIMAL values back to exact numbers at their
    declared scale — a type-driven decode, never comparison-time string fudging.
    """
    if raw is None:
        return TypedValue(Kind.NULL, None, type_meta)

    # bool must be checked before int (bool is an int subclass).
    if isinstance(raw, bool):
        return TypedValue(Kind.BOOL, raw, type_meta)

    if isinstance(raw, Decimal):
        d = raw
        if type_meta and type_meta.scale is not None and type_meta.kind == Kind.DECIMAL:
            d = _quantize(d, type_meta.scale)
        return TypedValue(Kind.DECIMAL, d, type_meta)

    if isinstance(raw, int):
        if type_meta and type_meta.kind == Kind.DECIMAL:
            d = Decimal(raw)
            if type_meta.scale is not None:
                d = _quantize(d, type_meta.scale)
            return TypedValue(Kind.DECIMAL, d, type_meta)
        return TypedValue(Kind.INT, raw, type_meta)

    if isinstance(raw, float):
        if type_meta and type_meta.kind == Kind.DECIMAL:
            # Shortest round-trippable decimal of the actual stored double,
            # then decode to declared scale.
            d = Decimal(str(raw))
            if type_meta.scale is not None:
                d = _quantize(d, type_meta.scale)
            return TypedValue(Kind.DECIMAL, d, type_meta)
        return TypedValue(Kind.FLOAT, raw, type_meta)

    if isinstance(raw, (bytes, bytearray, memoryview)):
        return TypedValue(Kind.BYTES, bytes(raw), type_meta)

    if isinstance(raw, _dt.datetime):
        return TypedValue(Kind.DATETIME, raw, type_meta)
    if isinstance(raw, _dt.date):
        return TypedValue(Kind.DATE, raw, type_meta)
    if isinstance(raw, _dt.time):
        return TypedValue(Kind.TIME, raw, type_meta)

    if isinstance(raw, str):
        kind = type_meta.kind if type_meta else Kind.TEXT
        if kind in EXACT_NUMERIC:
            try:
                d = Decimal(raw.strip())
            except (InvalidOperation, ValueError):
                # Declared numeric but undecodable content: keep as text so the
                # mismatch surfaces instead of crashing the whole segment.
                return TypedValue(Kind.TEXT, raw, type_meta)
            if type_meta and type_meta.scale is not None and kind == Kind.DECIMAL:
                try:
                    d = _quantize(d, type_meta.scale)
                except InvalidOperation:
                    pass
            return TypedValue(Kind.DECIMAL, d, type_meta)
        if kind == Kind.FLOAT:
            try:
                return TypedValue(Kind.FLOAT, float(raw), type_meta)
            except ValueError:
                return TypedValue(Kind.TEXT, raw, type_meta)
        if kind in (Kind.DATE, Kind.DATETIME, Kind.TIME):
            parsed = _parse_temporal(raw, kind)
            if parsed is not None:
                return TypedValue(kind, parsed, type_meta)
        return TypedValue(Kind.TEXT, raw, type_meta)

    # Unknown driver type: render through repr but tag as json/unknown so it
    # still cannot collide with NULL or other kinds.
    return TypedValue(Kind.JSON, raw, type_meta)


def _parse_temporal(raw: str, kind: str) -> Optional[Any]:
    s = raw.strip().replace("Z", "+00:00")
    try:
        if kind == Kind.DATETIME:
            return _dt.datetime.fromisoformat(s)
        if kind == Kind.DATE:
            return _dt.date.fromisoformat(s[:10])
        if kind == Kind.TIME:
            return _dt.time.fromisoformat(s)
    except ValueError:
        return None
    return None


@dataclass(frozen=True)
class TypedValue:
    kind: str
    value: Any
    meta: Optional[TypeMeta] = None

    # ---- equality -------------------------------------------------------
    def typed_equals(
        self,
        other: "TypedValue",
        *,
        abs_tolerance: Optional[Decimal] = None,
    ) -> "CompareResult":
        """Return a structured comparison.  Never coerces kinds to strings."""
        if self.kind == Kind.NULL or other.kind == Kind.NULL:
            if self.kind == Kind.NULL and other.kind == Kind.NULL:
                return CompareResult(True, "both_null")
            return CompareResult(False, "null_mismatch")

        # Exact numerics compare by magnitude across INT/DECIMAL.
        if self.kind in EXACT_NUMERIC and other.kind in EXACT_NUMERIC:
            a = _as_decimal(self)
            b = _as_decimal(other)
            if a == b:
                drift = _scale_drift(self.meta, other.meta)
                if drift:
                    return CompareResult(True, "equal_value_precision_drift", precision_drift=drift)
                return CompareResult(True, "equal")
            return CompareResult(False, "value_mismatch")

        if self.kind == Kind.FLOAT or other.kind == Kind.FLOAT:
            if self.kind == Kind.FLOAT and other.kind == Kind.FLOAT:
                a, b = self.value, other.value
                if _float_equal(a, b):
                    return CompareResult(True, "equal")
                if abs_tolerance is not None:
                    try:
                        if abs(Decimal(repr(a)) - Decimal(repr(b))) <= abs_tolerance:
                            return CompareResult(True, "within_tolerance")
                    except InvalidOperation:
                        pass
                return CompareResult(False, "value_mismatch")
            # float vs exact numeric: compare exact binary-derived decimals;
            # no rounding is applied unless tolerance was declared.
            a = Decimal(repr(self.value)) if self.kind == Kind.FLOAT else _as_decimal(self)
            b = Decimal(repr(other.value)) if other.kind == Kind.FLOAT else _as_decimal(other)
            if a == b:
                return CompareResult(True, "equal")
            if abs_tolerance is not None and abs(a - b) <= abs_tolerance:
                return CompareResult(True, "within_tolerance")
            return CompareResult(False, "numeric_kind_mismatch")

        if self.kind != other.kind:
            return CompareResult(False, "type_mismatch")

        if self.value == other.value:
            return CompareResult(True, "equal")
        return CompareResult(False, "value_mismatch")

    # ---- canonical fingerprint token -----------------------------------
    def canonical(self) -> str:
        k = self.kind
        if k == Kind.NULL:
            return "N:"
        if k == Kind.BOOL:
            return f"B:{1 if self.value else 0}"
        if k in EXACT_NUMERIC:
            d = _as_decimal(self)
            return f"Q:{_canonical_decimal(d)}"
        if k == Kind.FLOAT:
            if math.isnan(self.value):
                return "F:nan"
            return f"F:{self.value.hex()}"
        if k == Kind.TEXT:
            s = self.value
            return f"T:{len(s.encode('utf-8'))}:{s}"
        if k == Kind.BYTES:
            return f"X:{len(self.value)}:{base64.b64encode(self.value).decode('ascii')}"
        if k in (Kind.DATE, Kind.TIME, Kind.DATETIME):
            return f"D:{k}:{self.value.isoformat()}"
        return f"J:{len(str(self.value))}:{self.value}"

    # ---- wire / UI rendering -------------------------------------------
    def to_wire(self) -> dict[str, Any]:
        if self.kind == Kind.NULL:
            display = None
        elif self.kind in EXACT_NUMERIC:
            display = format(_as_decimal(self), "f")
        elif self.kind == Kind.FLOAT:
            # Full round-trippable repr — UI must not reformat this away.
            display = repr(self.value)
        elif self.kind == Kind.BYTES:
            display = "0x" + self.value.hex()
        elif self.kind in (Kind.DATE, Kind.TIME, Kind.DATETIME):
            display = self.value.isoformat()
        else:
            display = self.value if isinstance(self.value, (str, int, float, bool)) else str(self.value)
        out: dict[str, Any] = {"kind": self.kind, "v": display}
        if self.meta is not None and (self.meta.precision is not None or self.meta.scale is not None):
            out["sql_type"] = self.meta.raw
            out["scale"] = self.meta.scale
            out["precision"] = self.meta.precision
        return out


@dataclass(frozen=True)
class CompareResult:
    equal: bool
    code: str
    precision_drift: Optional[tuple[Optional[int], Optional[int]]] = None


def _as_decimal(tv: TypedValue) -> Decimal:
    if tv.kind == Kind.DECIMAL:
        return tv.value
    if tv.kind == Kind.INT:
        return Decimal(tv.value)
    raise TypeError(f"not exact numeric: {tv.kind}")


def _canonical_decimal(d: Decimal) -> str:
    if d == 0:
        return "0"
    sign, digits, exp = d.as_tuple()
    # Build fixed notation then strip trailing zeros so 1.20 and 1.2 share a
    # token *because Decimal says they are equal*, not because strings match.
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        return "0"
    return s


def _scale_drift(ma: Optional[TypeMeta], mb: Optional[TypeMeta]) -> Optional[tuple[int, int]]:
    sa = ma.scale if ma is not None else None
    sb = mb.scale if mb is not None else None
    if sa is not None and sb is not None and sa != sb:
        return (sa, sb)
    return None


def _float_equal(a: float, b: float) -> bool:
    if math.isnan(a) and math.isnan(b):
        return True
    return a == b


# ---------------------------------------------------------------------------
# Business keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    """A type-tagged composite business key."""

    parts: tuple[TypedValue, ...]

    def canonical(self) -> str:
        return "K(" + ",".join(p.canonical() for p in self.parts) + ")"

    def to_wire(self) -> list[dict[str, Any]]:
        return [p.to_wire() for p in self.parts]

    def display(self) -> str:
        return "|".join(_display_plain(p) for p in self.parts)


def _display_plain(p: TypedValue) -> str:
    if p.kind == Kind.NULL:
        return "NULL"
    if p.kind in EXACT_NUMERIC:
        return format(_as_decimal(p), "f")
    return str(p.value)
