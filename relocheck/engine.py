"""Pure comparison engine: type-aware row / key-group / segment comparison.

Nothing in here opens a connection or looks at a clock; the consistency layer
feeds it rows and decides whether a candidate verdict is definitive.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from .config import TableConfig
from .sources import ColumnMeta, Source
from .values import Key, Kind, TypedValue, make_typed


# ---------------------------------------------------------------------------
# Column resolution / row typing
# ---------------------------------------------------------------------------


@dataclass
class ResolvedTable:
    name: str
    key_columns: list[str]
    compare_columns: list[str]
    metas: dict[str, Any]
    physical_pk: list[str]


def resolve_table(tcfg: TableConfig, source: Source) -> ResolvedTable:
    cols = source.columns(tcfg.name)
    metas = {c.name: c.type_meta for c in cols}
    names = [c.name for c in cols]
    physical_pk = tcfg.primary_key or [c.name for c in cols if c.primary_key]

    if tcfg.business_key:
        key_columns = list(tcfg.business_key)
    elif physical_pk:
        key_columns = list(physical_pk)
    else:
        raise ValueError(
            f"table {tcfg.name}: no business_key declared and no primary key "
            "detected; cannot align rows"
        )
    for k in key_columns:
        if k not in metas:
            raise ValueError(f"table {tcfg.name}: key column {k!r} does not exist")

    if tcfg.compare_columns:
        compare_columns = list(tcfg.compare_columns)
    else:
        ignored = set(tcfg.ignore_columns)
        if tcfg.business_key:
            # A declared business key means the physical PK is presumed unstable
            # (auto-increment values differ between old and new DB).
            ignored |= {p for p in physical_pk if p not in key_columns}
        # Watermark columns implement the consistency protocol; they are not
        # business data and must not create comparison noise.
        if tcfg.version_column:
            ignored.add(tcfg.version_column)
        if tcfg.updated_at_column:
            ignored.add(tcfg.updated_at_column)
        compare_columns = [n for n in names if n not in ignored]

    for c in compare_columns:
        if c not in metas:
            raise ValueError(f"table {tcfg.name}: compare column {c!r} does not exist")

    return ResolvedTable(tcfg.name, key_columns, compare_columns, metas, physical_pk)


def type_row(raw: dict[str, Any], resolved: ResolvedTable) -> dict[str, TypedValue]:
    return {
        name: make_typed(raw.get(name), resolved.metas.get(name))
        for name in resolved.compare_columns
    }


def key_of_raw(raw: dict[str, Any], resolved: ResolvedTable) -> Key:
    return Key(
        tuple(make_typed(raw.get(k), resolved.metas.get(k)) for k in resolved.key_columns)
    )


def segment_index(key: Key, segment_count: int) -> int:
    digest = hashlib.sha256(key.canonical().encode("utf-8")).digest()
    n = int.from_bytes(digest[:8], "big")
    return n % segment_count


def row_token(row: dict[str, TypedValue], columns: list[str]) -> str:
    return "|".join(row[c].canonical() for c in columns)


def row_signature(row: dict[str, TypedValue], columns: list[str]) -> str:
    """Scale-sensitive identity used for multiset cancellation.

    The canonical token decides *value* equality; this signature additionally
    carries declared decimal scale so that ``10.10`` (scale 2) and ``10.1000``
    (scale 4) are not canceled as the very same row — they must be paired and
    surfaced as precision drift evidence even though their magnitudes match.
    """
    parts = []
    for c in columns:
        v = row[c]
        tok = v.canonical()
        if v.kind == Kind.DECIMAL and v.meta is not None and v.meta.scale is not None:
            tok = f"{tok}#scale{v.meta.scale}"
        parts.append(tok)
    return "|".join(parts)


def fingerprint_rows(rows: list[dict[str, TypedValue]], columns: list[str]) -> str:
    """Order-independent multiset fingerprint (type-tagged per cell)."""
    h = hashlib.sha256()
    for token in sorted(row_token(r, columns) for r in rows):
        h.update(token.encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Evidence structures
# ---------------------------------------------------------------------------


@dataclass
class ColumnDiff:
    column: str
    a: dict[str, Any]
    b: dict[str, Any]
    code: str
    equal: bool

    def to_wire(self) -> dict[str, Any]:
        return {"column": self.column, "a": self.a, "b": self.b, "code": self.code, "equal": self.equal}


@dataclass
class RowEvidence:
    disposition: str  # paired | only_a | only_b | identical
    values_a: Optional[dict[str, dict[str, Any]]] = None
    values_b: Optional[dict[str, dict[str, Any]]] = None
    column_diffs: list[ColumnDiff] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "values_a": self.values_a,
            "values_b": self.values_b,
            "column_diffs": [d.to_wire() for d in self.column_diffs],
        }


@dataclass
class KeyEvidence:
    key: Key
    status: str  # match | content_diff | missing_b | extra_b
    multiplicity_a: int
    multiplicity_b: int
    duplicate_group: bool
    identical_rows: int = 0
    row_evidence: list[RowEvidence] = field(default_factory=list)
    diff_columns: list[str] = field(default_factory=list)

    def to_wire(self) -> dict[str, Any]:
        return {
            "key": self.key.to_wire(),
            "key_display": self.key.display(),
            "status": self.status,
            "multiplicity_a": self.multiplicity_a,
            "multiplicity_b": self.multiplicity_b,
            "duplicate_group": self.duplicate_group,
            "identical_rows": self.identical_rows,
            "row_evidence": [r.to_wire() for r in self.row_evidence],
            "diff_columns": self.diff_columns,
        }


@dataclass
class SegmentCandidate:
    """Pure comparison result before the consistency verdict is attached."""

    index: int
    total_segments: int
    rows_a: int
    rows_b: int
    fingerprint_a: str
    fingerprint_b: str
    key_evidence: list[KeyEvidence]
    keys_total: int
    keys_match: int
    keys_diff: int
    missing_b: int
    extra_b: int
    duplicate_groups: int
    precision_warnings: int

    @property
    def content_equal(self) -> bool:
        return self.keys_diff == 0 and self.missing_b == 0 and self.extra_b == 0


# ---------------------------------------------------------------------------
# Key-group (multi-row) multiset comparison
# ---------------------------------------------------------------------------


def _row_wire(row: dict[str, TypedValue], columns: list[str]) -> dict[str, dict[str, Any]]:
    return {c: row[c].to_wire() for c in columns}


def _column_diffs(
    row_a: dict[str, TypedValue],
    row_b: dict[str, TypedValue],
    columns: list[str],
    tcfg: TableConfig,
) -> tuple[list[ColumnDiff], int]:
    diffs: list[ColumnDiff] = []
    warnings = 0
    for c in columns:
        pa = tcfg.policy_for(c)
        res = row_a[c].typed_equals(row_b[c], abs_tolerance=pa.abs_tolerance)
        strict_precision = pa.strict_precision
        if res.equal and res.code == "equal_value_precision_drift":
            warnings += 1
            if strict_precision:
                diffs.append(
                    ColumnDiff(c, row_a[c].to_wire(), row_b[c].to_wire(), "precision_mismatch", False)
                )
            else:
                diffs.append(
                    ColumnDiff(c, row_a[c].to_wire(), row_b[c].to_wire(), res.code, True)
                )
        elif not res.equal:
            diffs.append(ColumnDiff(c, row_a[c].to_wire(), row_b[c].to_wire(), res.code, False))
    return diffs, warnings


def _distance(diffs: list[ColumnDiff]) -> int:
    return sum(1 for d in diffs if not d.equal)


def compare_key_group(
    key: Key,
    rows_a: list[dict[str, TypedValue]],
    rows_b: list[dict[str, TypedValue]],
    resolved: ResolvedTable,
    tcfg: TableConfig,
) -> tuple[KeyEvidence, int]:
    """Compare all rows sharing one business key using multiset cancellation."""
    cols = resolved.compare_columns
    sig_a = Counter(row_signature(r, cols) for r in rows_a)
    sig_b = Counter(row_signature(r, cols) for r in rows_b)
    common = sig_a & sig_b
    identical_rows = sum(common.values())

    # Rebuild leftover row lists (deterministic order: input order is stable).
    leftover_a: list[dict[str, TypedValue]] = []
    remain_a = dict(common)
    for r in rows_a:
        sig = row_signature(r, cols)
        if remain_a.get(sig, 0) > 0:
            remain_a[sig] -= 1  # canceled as identical
        else:
            leftover_a.append(r)
    leftover_b: list[dict[str, TypedValue]] = []
    remain_b = dict(common)
    for r in rows_b:
        sig = row_signature(r, cols)
        if remain_b.get(sig, 0) > 0:
            remain_b[sig] -= 1
        else:
            leftover_b.append(r)

    duplicate_group = len(rows_a) > 1 or len(rows_b) > 1
    all_column_diffs: list[ColumnDiff] = []
    precision_warnings = 0
    evidence: list[RowEvidence] = []

    # Pair leftovers to present column-level evidence.  Pairing is presentation
    # only — it never changes the multiset verdict.
    pairs, solo_a, solo_b = _pair_leftovers(leftover_a, leftover_b, cols, tcfg)
    hard_column_diff = False
    for ra, rb, diffs, warns in pairs:
        precision_warnings += warns
        all_column_diffs.extend(diffs)
        if any(not d.equal for d in diffs):
            hard_column_diff = True
        evidence.append(
            RowEvidence(
                "paired",
                _row_wire(ra, cols),
                _row_wire(rb, cols),
                diffs,
            )
        )
    for ra in solo_a:
        hard_column_diff = True
        evidence.append(RowEvidence("only_a", values_a=_row_wire(ra, cols)))
    for rb in solo_b:
        hard_column_diff = True
        evidence.append(RowEvidence("only_b", values_b=_row_wire(rb, cols)))

    if not hard_column_diff:
        # Either exactly identical multisets, or leftovers differ only by
        # non-strict precision/scale drift — same conclusion on values.
        status = "match"
    elif not rows_b:
        status = "missing_b"
    elif not rows_a:
        status = "extra_b"
    else:
        status = "content_diff"

    diff_columns = sorted({d.column for d in all_column_diffs if not d.equal})

    ke = KeyEvidence(
        key=key,
        status=status,
        multiplicity_a=len(rows_a),
        multiplicity_b=len(rows_b),
        duplicate_group=duplicate_group,
        identical_rows=identical_rows,
        row_evidence=evidence,
        diff_columns=diff_columns,
    )
    return ke, precision_warnings


def _pair_leftovers(
    a: list[dict[str, TypedValue]],
    b: list[dict[str, TypedValue]],
    cols: list[str],
    tcfg: TableConfig,
) -> tuple[list[tuple[Any, Any, list[ColumnDiff], int]], list[Any], list[Any]]:
    if not a or not b:
        return [], a, b

    # Greedy minimum-distance pairing; groups are almost always tiny.  Cap the
    # O(n^2) work for pathological duplicate piles and pair by index instead.
    if len(a) * len(b) > 2500:
        pairs = []
        n = min(len(a), len(b))
        for i in range(n):
            diffs, warns = _column_diffs(a[i], b[i], cols, tcfg)
            pairs.append((a[i], b[i], diffs, warns))
        return pairs, a[n:], b[n:]

    cache: dict[tuple[int, int], tuple[list[ColumnDiff], int]] = {}
    for i, ra in enumerate(a):
        for j, rb in enumerate(b):
            cache[(i, j)] = _column_diffs(ra, rb, cols, tcfg)

    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs = []
    # Repeatedly take the globally closest unused pair.
    candidates = sorted(
        (
            (_distance(cache[(i, j)][0]), i, j)
            for i in range(len(a))
            for j in range(len(b))
        )
    )
    for _dist, i, j in candidates:
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        diffs, warns = cache[(i, j)]
        pairs.append((a[i], b[j], diffs, warns))
    solo_a = [r for i, r in enumerate(a) if i not in used_a]
    solo_b = [r for j, r in enumerate(b) if j not in used_b]
    return pairs, solo_a, solo_b


# ---------------------------------------------------------------------------
# Segment comparison
# ---------------------------------------------------------------------------


def compare_segment_rows(
    seg_index: int,
    total_segments: int,
    raw_a: list[dict[str, Any]],
    raw_b: list[dict[str, Any]],
    resolved_a: ResolvedTable,
    tcfg: TableConfig,
    resolved_b: Optional[ResolvedTable] = None,
) -> SegmentCandidate:
    """Compare one segment.

    Each side is typed with its *own* declared schema (``resolved_b`` falls
    back to ``resolved_a``), so cross-database type/scale drift is preserved
    and can be reported as precision evidence.
    """
    ra = resolved_a
    rb = resolved_b or resolved_a
    # Column sets normally match; if they do not, compare the intersection.
    if ra.compare_columns != rb.compare_columns:
        common = [c for c in ra.compare_columns if c in rb.compare_columns]
        ra = ResolvedTable(ra.name, ra.key_columns, common, ra.metas, ra.physical_pk)
        rb = ResolvedTable(rb.name, rb.key_columns, common, rb.metas, rb.physical_pk)

    typed_a = [type_row(r, ra) for r in raw_a]
    typed_b = [type_row(r, rb) for r in raw_b]

    groups_a: dict[Key, list[dict[str, TypedValue]]] = {}
    groups_b: dict[Key, list[dict[str, TypedValue]]] = {}
    for raw, typed in zip(raw_a, typed_a):
        groups_a.setdefault(key_of_raw(raw, ra), []).append(typed)
    for raw, typed in zip(raw_b, typed_b):
        groups_b.setdefault(key_of_raw(raw, rb), []).append(typed)

    evidence: list[KeyEvidence] = []
    precision_warnings = 0
    keys_match = keys_diff = missing_b = extra_b = duplicate_groups = 0

    # Stable ordering for evidence: keys by display string.
    all_keys = sorted(set(groups_a) | set(groups_b), key=lambda k: k.canonical())
    for k in all_keys:
        rows_a = groups_a.get(k, [])
        rows_b = groups_b.get(k, [])
        ke, warns = compare_key_group(k, rows_a, rows_b, ra, tcfg)
        precision_warnings += warns
        if ke.duplicate_group:
            duplicate_groups += 1
        if ke.status == "match":
            keys_match += 1
        elif ke.status == "missing_b":
            missing_b += 1
            keys_diff += 1
        elif ke.status == "extra_b":
            extra_b += 1
            keys_diff += 1
        else:
            keys_diff += 1
        evidence.append(ke)

    return SegmentCandidate(
        index=seg_index,
        total_segments=total_segments,
        rows_a=len(raw_a),
        rows_b=len(raw_b),
        fingerprint_a=fingerprint_rows(typed_a, ra.compare_columns),
        fingerprint_b=fingerprint_rows(typed_b, rb.compare_columns),
        key_evidence=evidence,
        keys_total=len(all_keys),
        keys_match=keys_match,
        keys_diff=keys_diff,
        missing_b=missing_b,
        extra_b=extra_b,
        duplicate_groups=duplicate_groups,
        precision_warnings=precision_warnings,
    )


def rows_for_segment(
    raw_rows: list[dict[str, Any]], resolved: ResolvedTable, seg_index: int, total: int
) -> list[dict[str, Any]]:
    return [r for r in raw_rows if segment_index(key_of_raw(r, resolved), total) == seg_index]
