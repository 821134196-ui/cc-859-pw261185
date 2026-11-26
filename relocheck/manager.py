"""Run manager: creates comparison runs, aggregates verdicts, handles rechecks.

The critical aggregate invariant lives here: **a deferred segment is never
counted as equal**, regardless of what its provisional row comparison says,
and a run with any deferred segment can never receive an overall "verified
equal" conclusion.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Optional

from .config import ReconcileConfig, TableConfig
from .consistency import SegmentReport, scan_segment
from .sources import Source, open_source


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunRecord:
    id: str
    created_at: str
    params: dict[str, Any]
    reports: dict[str, list[Optional[SegmentReport]]]  # table -> seg-indexed
    histories: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    schema_drift: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"  # running | done
    error: Optional[str] = None

    # -- aggregation ------------------------------------------------------
    def segments(self) -> list[SegmentReport]:
        out: list[SegmentReport] = []
        for arr in self.reports.values():
            out.extend(r for r in arr if r is not None)
        return out

    def totals(self) -> dict[str, Any]:
        segs = self.segments()
        t = {
            "segments_total": sum(len(a) for a in self.reports.values()),
            "segments_ready": len(segs),
            "match": 0, "diff": 0, "deferred": 0,
            "provisional_equal_among_deferred": 0,
            "keys_match": 0, "keys_diff": 0, "missing_b": 0, "extra_b": 0,
            "duplicate_groups": 0, "precision_warnings": 0,
            "rows_a": 0, "rows_b": 0,
        }
        for r in segs:
            t[r.verdict] += 1
            c = r.candidate
            t["keys_match"] += c.keys_match
            t["keys_diff"] += c.keys_diff
            t["missing_b"] += c.missing_b
            t["extra_b"] += c.extra_b
            t["duplicate_groups"] += c.duplicate_groups
            t["precision_warnings"] += c.precision_warnings
            t["rows_a"] += c.rows_a
            t["rows_b"] += c.rows_b
            if r.verdict == "deferred" and c.content_equal:
                t["provisional_equal_among_deferred"] += 1
        if self.status == "running" or t["segments_ready"] < t["segments_total"]:
            overall = "running"
        elif t["deferred"] > 0 and t["diff"] > 0:
            overall = "incomplete_with_diffs"
        elif t["deferred"] > 0:
            overall = "incomplete_deferred"
        elif t["diff"] > 0:
            overall = "differences_found"
        elif t["match"] > 0:
            overall = "verified_equal"
        else:
            overall = "empty"
        t["overall"] = overall
        return t

    def list_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "params": self.params,
            "status": self.status,
            "error": self.error,
            "totals": self.totals(),
        }

    def wire(self) -> dict[str, Any]:
        out = self.list_wire()
        tables: dict[str, Any] = {}
        for table, arr in self.reports.items():
            tables[table] = [r.summary_wire() if r else None for r in arr]
        out["tables"] = tables
        out["schema_drift"] = self.schema_drift
        return out


class RunManager:
    def __init__(self, config: ReconcileConfig):
        self.config = config
        self.runs: dict[str, RunRecord] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def get(self, run_id: str) -> RunRecord:
        return self.runs[run_id]

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.runs[i].list_wire() for i in reversed(self._order)]

    # -- run --------------------------------------------------------------
    def create_run(
        self,
        *,
        mode: str = "live",
        tables: Optional[list[str]] = None,
        accept_weak: bool = False,
        strict_precision: bool = False,
        snapshot_token: Optional[int] = None,
        run_in_background: bool = True,
    ) -> RunRecord:
        chosen = [t for t in self.config.tables if not tables or t.name in tables]
        params = {
            "mode": mode, "accept_weak": accept_weak,
            "strict_precision": strict_precision, "snapshot_token": snapshot_token,
            "tables": [t.name for t in chosen],
        }
        record = RunRecord(
            id=uuid.uuid4().hex[:12],
            created_at=_ts(),
            params=params,
            reports={t.name: [None] * t.segment_count for t in chosen},
        )
        with self._lock:
            self.runs[record.id] = record
            self._order.append(record.id)

        def work() -> None:
            sources = self._open_sources()
            try:
                record.schema_drift = self._detect_schema_drift(chosen, sources[0], sources[1])
                for tcfg0 in chosen:
                    tcfg = replace(tcfg0, strict_precision=strict_precision or tcfg0.strict_precision)
                    for seg in range(tcfg.segment_count):
                        rep = scan_segment(
                            tcfg, sources[0], sources[1], seg,
                            mode=mode, snapshot_token=snapshot_token,
                            accept_weak=accept_weak,
                        )
                        record.reports[tcfg.name][seg] = rep
                record.status = "done"
            except Exception as e:  # pragma: no cover - defensive
                record.error = f"{type(e).__name__}: {e}"
                record.status = "done"
            finally:
                sources[0].close(); sources[1].close()

        if run_in_background:
            threading.Thread(target=work, name=f"run-{record.id}", daemon=True).start()
        else:
            work()
        return record

    # -- recheck ----------------------------------------------------------
    def recheck(self, run_id: str, table: str, seg: int) -> SegmentReport:
        record = self.runs[run_id]
        tcfg = self.config.table(table)
        idx = seg
        key = f"{table}#{seg}"
        with self._lock:
            old = record.reports[table][idx]
            history = record.histories.setdefault(key, [])
            if old is not None:
                history.append({
                    "at": old.scanned_at,
                    "verdict": old.verdict,
                    "reason_code": old.reason_code,
                    "basis": old.basis,
                    "provisional_content": "match" if old.candidate.content_equal else "diff",
                })
            params = record.params
            strict = bool(params.get("strict_precision", False))
            tcfg_run = replace(tcfg, strict_precision=strict or tcfg.strict_precision)
            sources = self._open_sources()
            try:
                fresh = scan_segment(
                    tcfg_run, sources[0], sources[1], seg,
                    mode=params.get("mode", "live"),
                    snapshot_token=params.get("snapshot_token"),
                    accept_weak=params.get("accept_weak", False),
                )
            finally:
                sources[0].close(); sources[1].close()
            fresh.history = list(history)
            record.reports[table][idx] = fresh
            return fresh

    # -- helpers ----------------------------------------------------------
    def _open_sources(self) -> tuple[Source, Source]:
        a = open_source(self.config.source_a.dsn, self.config.source_a.name or "A")
        b = open_source(self.config.source_b.dsn, self.config.source_b.name or "B")
        return a, b

    def _detect_schema_drift(
        self, tables: list[TableConfig], sa: Source, sb: Source
    ) -> list[dict[str, Any]]:
        drift: list[dict[str, Any]] = []
        for t in tables:
            try:
                ca = {c.name: c for c in sa.columns(t.name)}
                cb = {c.name: c for c in sb.columns(t.name)}
            except Exception:
                continue
            for name in sorted(set(ca) & set(cb)):
                ma, mb = ca[name].type_meta, cb[name].type_meta
                ra, rb = (ma.raw if ma else ""), (mb.raw if mb else "")
                if ra.upper().replace(" ", "") == rb.upper().replace(" ", ""):
                    continue
                kind = "type"
                if ma and mb and ma.kind == mb.kind and (ma.scale != mb.scale or ma.precision != mb.precision):
                    kind = "precision_scale"
                drift.append({
                    "table": t.name, "column": name, "kind": kind,
                    "a_type": ra, "b_type": rb,
                    "a_scale": ma.scale if ma else None,
                    "b_scale": mb.scale if mb else None,
                })
            only_a = sorted(set(ca) - set(cb))
            only_b = sorted(set(cb) - set(ca))
            for n in only_a:
                drift.append({"table": t.name, "column": n, "kind": "only_in_a",
                              "a_type": ca[n].type_meta.raw if ca[n].type_meta else "", "b_type": None})
            for n in only_b:
                drift.append({"table": t.name, "column": n, "kind": "only_in_b",
                              "a_type": None, "b_type": cb[n].type_meta.raw if cb[n].type_meta else ""})
        return drift
