"""Reconciliation configuration: connections, tables, declared business keys."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


@dataclass
class ColumnPolicy:
    name: str
    compare: bool = True
    abs_tolerance: Optional[Decimal] = None
    strict_precision: bool = False

    @staticmethod
    def from_obj(name: str, obj: Any) -> "ColumnPolicy":
        if obj is None:
            return ColumnPolicy(name)
        if isinstance(obj, str):
            return ColumnPolicy(name, compare=(obj != "ignore"))
        return ColumnPolicy(
            name=name,
            compare=obj.get("compare", True),
            abs_tolerance=Decimal(str(obj["abs_tolerance"])) if obj.get("abs_tolerance") is not None else None,
            strict_precision=bool(obj.get("strict_precision", False)),
        )


@dataclass
class TableConfig:
    name: str
    #: Declared business key columns used to align rows across the two DBs.
    business_key: list[str] = field(default_factory=list)
    #: Physical primary key columns; auto-detected when omitted.  These are
    #: ignored for comparison when ``business_key`` is set unless listed in
    #: ``compare_columns``.
    primary_key: list[str] = field(default_factory=list)
    #: Explicit compared columns; empty means "all columns minus ignore".
    compare_columns: list[str] = field(default_factory=list)
    ignore_columns: list[str] = field(default_factory=list)
    column_policies: dict[str, ColumnPolicy] = field(default_factory=dict)
    segment_count: int = 8
    #: Optional monotonic per-segment/table version column (e.g. update seq).
    version_column: Optional[str] = None
    updated_at_column: Optional[str] = None
    #: Optional replication event log table:
    #:   event_log: {table: sync_events, seq_column: seq, segment_column: segment}
    event_log: Optional[dict[str, str]] = None
    strict_precision: bool = False

    def policy_for(self, col: str) -> ColumnPolicy:
        return self.column_policies.get(col) or ColumnPolicy(col, strict_precision=self.strict_precision)


@dataclass
class ConnectionConfig:
    name: str
    dsn: str


@dataclass
class ReconcileConfig:
    source_a: ConnectionConfig
    source_b: ConnectionConfig
    tables: list[TableConfig]

    def table(self, name: str) -> TableConfig:
        for t in self.tables:
            if t.name == name:
                return t
        raise KeyError(f"table not configured: {name}")

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ReconcileConfig":
        a = d["source_a"]
        b = d["source_b"]
        source_a = ConnectionConfig(a.get("name", "A"), a["dsn"])
        source_b = ConnectionConfig(b.get("name", "B"), b["dsn"])
        tables: list[TableConfig] = []
        for td in d.get("tables", []):
            policies: dict[str, ColumnPolicy] = {}
            for cname, cobj in (td.get("columns") or {}).items():
                policies[cname] = ColumnPolicy.from_obj(cname, cobj)
            tables.append(
                TableConfig(
                    name=td["name"],
                    business_key=list(td.get("business_key", [])),
                    primary_key=list(td.get("primary_key", [])),
                    compare_columns=list(td.get("compare_columns", [])),
                    ignore_columns=list(td.get("ignore_columns", [])),
                    column_policies=policies,
                    segment_count=int(td.get("segment_count", d.get("segment_count", 8))),
                    version_column=td.get("version_column"),
                    updated_at_column=td.get("updated_at_column"),
                    event_log=td.get("event_log"),
                    strict_precision=bool(td.get("strict_precision", False)),
                )
            )
        return ReconcileConfig(source_a, source_b, tables)

    @staticmethod
    def load(path: str) -> "ReconcileConfig":
        with open(path, "r", encoding="utf-8") as f:
            return ReconcileConfig.from_dict(json.load(f))
