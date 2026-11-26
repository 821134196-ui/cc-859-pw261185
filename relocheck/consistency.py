"""Consistency protocol: definitive verdicts vs deferred / undecidable segments.

Why this module exists
----------------------
Two live databases being written to cannot share a point-in-time snapshot.  A
naive scan can therefore return *equal* for a segment that merely happened to
be caught at two different replication states — or report a phantom
difference.  This module attaches watermarks and a confirmation read to every
segment and refuses to issue a definitive MATCH unless the captured state is
provably stable and aligned across both sides.

Strong basis (demo / configured event log)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The writer stamps every mutation with one shared ordered event sequence and
records it in a per-DB event log carrying the segment number.  A segment gets
a definitive verdict only when:

1. the post-read segment event token on A equals the one on B (same applied
   logical state for this key range);
2. the token does not move between the post-read marker and two bracketing
   verification markers;
3. a fresh multiset fingerprint equals the fingerprint computed from the rows
   that were actually compared.

Otherwise the segment is ``deferred`` (undecidable this round), **never**
counted as equal, and can be re-checked later.

Weaker / other bases
~~~~~~~~~~~~~~~~~~~~
* ``snapshot_token`` — caller pinned a joint snapshot / froze writes at a token.
* ``quiescent_window`` — no shared token, but a declared ``updated_at`` column
  proves the segment/table was quiet across the whole scan window.
* ``weak_two_pass`` — stability only; cross-side alignment unproven.  Only
  accepted when ``accept_weak=True`` and clearly labelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import TableConfig
from .engine import (
    ResolvedTable,
    SegmentCandidate,
    compare_segment_rows,
    resolve_table,
    rows_for_segment,
)
from .sources import Source


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Watermarks
# ---------------------------------------------------------------------------


@dataclass
class Watermark:
    event_seq: Optional[int] = None  # shared per-segment replication token
    global_seq: Optional[int] = None  # global applied token
    version: Any = None  # monotonic version_column MAX
    updated_at: Any = None  # MAX(updated_at_column)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_seq": self.event_seq,
            "global_seq": self.global_seq,
            "version": self.version,
            "updated_at": self.updated_at,
        }

    def identity_tuple(self) -> tuple[Any, ...]:
        # Signals that must not move after the rows were captured.
        return (self.event_seq, self.version)


def _event_log_parts(tcfg: TableConfig) -> Optional[tuple[str, str, Optional[str]]]:
    el = tcfg.event_log
    if not el:
        return None
    return el["table"], el.get("seq_column", "seq"), el.get("segment_column")


def read_watermark(source: Source, tcfg: TableConfig, seg: int) -> Watermark:
    wm = Watermark()
    parts = _event_log_parts(tcfg)
    if parts is not None:
        log_table, seq_col, seg_col = parts
        try:
            wm.global_seq = source.fetch_scalar(
                f'SELECT MAX("{seq_col}") FROM "{log_table}"'
            )
            if seg_col:
                wm.event_seq = source.fetch_scalar(
                    f'SELECT MAX("{seq_col}") FROM "{log_table}" WHERE "{seg_col}" = ?',
                    (seg,),
                )
            else:
                wm.event_seq = wm.global_seq
        except Exception:
            # Event log missing/unreadable: consistency degrades rather than
            # crashing the comparison; the segment will be deferred.
            wm.event_seq = None
            wm.global_seq = None
    if tcfg.version_column:
        try:
            wm.version = source.max_column(tcfg.name, tcfg.version_column)
        except Exception:
            wm.version = None
    if tcfg.updated_at_column:
        try:
            wm.updated_at = source.max_column(tcfg.name, tcfg.updated_at_column)
        except Exception:
            wm.updated_at = None
    return wm


def events_since(
    source: Source, tcfg: TableConfig, seg: int, since: Optional[int], limit: int = 20
) -> list[dict[str, Any]]:
    parts = _event_log_parts(tcfg)
    if parts is None:
        return []
    log_table, seq_col, seg_col = parts
    try:
        if seg_col:
            if since is None:
                sql = (
                    f'SELECT * FROM "{log_table}" WHERE "{seg_col}" = ? '
                    f'ORDER BY "{seq_col}" DESC LIMIT {int(limit)}'
                )
                rows = source.fetch_dicts(sql, (seg,))
                return list(reversed(rows))
            sql = (
                f'SELECT * FROM "{log_table}" WHERE "{seg_col}" = ? AND "{seq_col}" > ? '
                f'ORDER BY "{seq_col}" ASC LIMIT {int(limit)}'
            )
            return source.fetch_dicts(sql, (seg, since))
        sql = f'SELECT * FROM "{log_table}" WHERE "{seq_col}" > ? ORDER BY "{seq_col}" ASC LIMIT {int(limit)}'
        return source.fetch_dicts(sql, (since or 0,))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Timeline / report
# ---------------------------------------------------------------------------


@dataclass
class TimelineEvent:
    at: str
    code: str
    message: str
    side: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {"at": self.at, "code": self.code, "message": self.message, "side": self.side, "detail": self.detail}


REASON_TEXT = {
    "write_during_scan": "扫描期间该键段在至少一侧发生了写入，读到的行与水位无法对应到同一状态。",
    "verify_changed": "复核读取时键段指纹已变化：首次比较的内容在确认前就被改写。",
    "event_lag": "两侧记录的共享复制事件位点不一致（复制延迟/错版本），本次比较混合了两个逻辑状态。",
    "alignment_unproven": "缺少共享事件位点或静默时间窗证据，无法证明两侧读到的是同一状态。",
    "hot_window": "该表最新写入时间落在扫描时间窗内，无法排除两侧分别读到写入前后状态。",
    "snapshot_not_aligned": "声明的快照位点尚未在两侧同时应用，快照一致性不成立。",
    "weak_basis_rejected": "仅有两遍稳定性、缺少跨侧对齐证据，按严格策略不予确定。",
}


@dataclass
class SegmentReport:
    table: str
    index: int
    total_segments: int
    mode: str
    verdict: str  # match | diff | deferred
    candidate: SegmentCandidate
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    basis: Optional[str] = None
    snapshot_token: Optional[int] = None
    watermarks: dict[str, Any] = field(default_factory=dict)
    timeline: list[TimelineEvent] = field(default_factory=list)
    scanned_at: str = field(default_factory=_now)
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def definitive(self) -> bool:
        return self.verdict in ("match", "diff")

    def summary_wire(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "table": self.table,
            "index": self.index,
            "total_segments": self.total_segments,
            "verdict": self.verdict,
            "definitive": self.definitive,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "basis": self.basis,
            "mode": self.mode,
            "snapshot_token": self.snapshot_token,
            "scanned_at": self.scanned_at,
            "rows_a": c.rows_a,
            "rows_b": c.rows_b,
            "keys_total": c.keys_total,
            "keys_match": c.keys_match,
            "keys_diff": c.keys_diff,
            "missing_b": c.missing_b,
            "extra_b": c.extra_b,
            "duplicate_groups": c.duplicate_groups,
            "precision_warnings": c.precision_warnings,
            "fingerprint_a": c.fingerprint_a,
            "fingerprint_b": c.fingerprint_b,
            "provisional_content": "match" if c.content_equal else "diff",
            "watermarks": self.watermarks,
            "history": self.history,
        }

    def detail_wire(self) -> dict[str, Any]:
        out = self.summary_wire()
        out["timeline"] = [e.to_wire() for e in self.timeline]
        out["evidence"] = [k.to_wire() for k in self.candidate.key_evidence]
        return out


# ---------------------------------------------------------------------------
# Scan protocol
# ---------------------------------------------------------------------------

StageHook = Optional[Callable[[str, Source, Source], None]]


def scan_segment(
    tcfg: TableConfig,
    source_a: Source,
    source_b: Source,
    seg: int,
    *,
    mode: str = "live",
    snapshot_token: Optional[int] = None,
    accept_weak: bool = False,
    time_slack_seconds: float = 2.0,
    hook: StageHook = None,
    resolved_a: Optional[ResolvedTable] = None,
    resolved_b: Optional[ResolvedTable] = None,
) -> SegmentReport:
    """Run the full read → compare → confirm protocol for one segment."""

    def stage(name: str) -> None:
        if hook is not None:
            hook(name, source_a, source_b)

    timeline: list[TimelineEvent] = []

    def log(code: str, message: str, side: Optional[str] = None, **detail: Any) -> None:
        timeline.append(TimelineEvent(_now(), code, message, side, detail))

    total = tcfg.segment_count
    ra = resolved_a or resolve_table(tcfg, source_a)
    rb = resolved_b or resolve_table(tcfg, source_b)

    log("scan_start", f"开始扫描 {tcfg.name} 键段 #{seg}/{total}", detail={"segment": seg})

    # ---------------- snapshot mode ------------------------------------
    if mode == "snapshot":
        if snapshot_token is not None:
            wa = read_watermark(source_a, tcfg, seg)
            wb = read_watermark(source_b, tcfg, seg)
            ga, gb = wa.global_seq, wb.global_seq
            if ga is None or gb is None or ga < snapshot_token or gb < snapshot_token:
                reason = REASON_TEXT["snapshot_not_aligned"]
                log("snapshot_not_aligned", reason, detail={"token": snapshot_token, "a": ga, "b": gb})
                candidate = _empty_candidate(seg, total)
                return SegmentReport(
                    tcfg.name, seg, total, mode, "deferred", candidate,
                    "snapshot_not_aligned", reason, None, snapshot_token,
                    {"pre_a": wa.as_dict(), "pre_b": wb.as_dict()}, timeline,
                )
            log("snapshot_aligned", f"两侧均已应用快照位点 {snapshot_token}", detail={"token": snapshot_token})
        raw_a = rows_for_segment(source_a.fetch_all(tcfg.name), ra, seg, total)
        stage("snapshot_after_a")
        raw_b = rows_for_segment(source_b.fetch_all(tcfg.name), rb, seg, total)
        stage("snapshot_after_b")
        # Column metas could differ slightly between sides; compare against A's
        # resolved column list but type each side with its own metas.
        candidate = compare_segment_rows(seg, total, raw_a, raw_b, ra, tcfg, rb)
        log("verdict", "快照一致性读取完成，给出确定结论", detail={"content_equal": candidate.content_equal})
        verdict = "match" if candidate.content_equal else "diff"
        return SegmentReport(
            tcfg.name, seg, total, mode, verdict, candidate,
            basis=f"snapshot_token:{snapshot_token}" if snapshot_token is not None else "snapshot_asserted",
            snapshot_token=snapshot_token, timeline=timeline,
        )

    # ---------------- live mode ----------------------------------------
    # 1) pre-watermarks on BOTH sides before any row read.
    wm_a0 = read_watermark(source_a, tcfg, seg)
    wm_b0 = read_watermark(source_b, tcfg, seg)
    log("watermark_pre", "读取前水位", "A", detail=wm_a0.as_dict())
    log("watermark_pre", "读取前水位", "B", detail=wm_b0.as_dict())
    stage("after_pre_markers")

    # 2) row reads + post-read markers.
    raw_a = rows_for_segment(source_a.fetch_all(tcfg.name), ra, seg, total)
    wm_a1 = read_watermark(source_a, tcfg, seg)
    log("read", f"A 侧读到 {len(raw_a)} 行", "A", rows=len(raw_a), watermark=wm_a1.as_dict())
    stage("after_a_read")

    raw_b = rows_for_segment(source_b.fetch_all(tcfg.name), rb, seg, total)
    wm_b1 = read_watermark(source_b, tcfg, seg)
    log("read", f"B 侧读到 {len(raw_b)} 行", "B", rows=len(raw_b), watermark=wm_b1.as_dict())
    stage("after_b_read")

    candidate = compare_segment_rows(seg, total, raw_a, raw_b, ra, tcfg, rb)
    stage("after_compare")

    # 3) confirmation: marker -> fingerprint -> marker brackets.
    wm_a2a = read_watermark(source_a, tcfg, seg)
    fp_a2 = _refingerprint(source_a, tcfg, ra, seg, total)
    wm_a2b = read_watermark(source_a, tcfg, seg)
    stage("after_verify_a")
    wm_b2a = read_watermark(source_b, tcfg, seg)
    fp_b2 = _refingerprint(source_b, tcfg, rb, seg, total)
    wm_b2b = read_watermark(source_b, tcfg, seg)
    stage("after_verify_b")

    watermarks = {
        "pre_a": wm_a0.as_dict(), "post_a": wm_a1.as_dict(),
        "verify_a": wm_a2b.as_dict(),
        "pre_b": wm_b0.as_dict(), "post_b": wm_b1.as_dict(),
        "verify_b": wm_b2b.as_dict(),
        "verify_fingerprint_a": fp_a2, "verify_fingerprint_b": fp_b2,
    }
    log("verify", "确认读取完成", "A", fingerprint=fp_a2, watermark=wm_a2b.as_dict())
    log("verify", "确认读取完成", "B", fingerprint=fp_b2, watermark=wm_b2b.as_dict())

    # 4) decision.
    moved_a = wm_a1.identity_tuple() != wm_a2b.identity_tuple() or wm_a2a.identity_tuple() != wm_a2b.identity_tuple()
    moved_b = wm_b1.identity_tuple() != wm_b2b.identity_tuple() or wm_b2a.identity_tuple() != wm_b2b.identity_tuple()
    fp_change_a = fp_a2 != candidate.fingerprint_a
    fp_change_b = fp_b2 != candidate.fingerprint_b

    # Surface concrete write evidence on the timeline.
    if wm_a0.event_seq != wm_a1.event_seq or moved_a or fp_change_a:
        gap = events_since(source_a, tcfg, seg, wm_a0.event_seq)
        log("write_observed", "A 侧在扫描窗口内发生写入", "A",
            from_seq=wm_a0.event_seq, to_seq=wm_a2b.event_seq, events=gap)
    if wm_b0.event_seq != wm_b1.event_seq or moved_b or fp_change_b:
        gap = events_since(source_b, tcfg, seg, wm_b0.event_seq)
        log("write_observed", "B 侧在扫描窗口内发生写入", "B",
            from_seq=wm_b0.event_seq, to_seq=wm_b2b.event_seq, events=gap)

    reason_code: Optional[str] = None
    basis: Optional[str] = None

    if fp_change_a or fp_change_b:
        reason_code = "verify_changed"
    elif moved_a or moved_b:
        reason_code = "write_during_scan"
    elif wm_a1.event_seq is not None and wm_b1.event_seq is not None:
        if wm_a1.event_seq != wm_b1.event_seq:
            gap_a = events_since(source_a, tcfg, seg, wm_b1.event_seq)
            gap_b = events_since(source_b, tcfg, seg, wm_a1.event_seq)
            log("lag_observed", "两侧共享事件位点不一致，存在复制延迟",
                detail={"token_a": wm_a1.event_seq, "token_b": wm_b1.event_seq,
                        "a_ahead_events": gap_a, "b_ahead_events": gap_b})
            reason_code = "event_lag"
        else:
            basis = "aligned_event_token"
            log("aligned", f"两侧事件位点一致 (#{wm_a1.event_seq}) 且复核稳定，结论可确定")
    else:
        # No shared event token.  Try the updated_at quiescence window.
        hot = _hot_window(wm_a0, wm_b0, wm_a2b, time_slack_seconds)
        if hot:
            reason_code = "hot_window"
        elif wm_a1.version is not None and wm_b1.version is not None:
            if accept_weak:
                basis = "weak_two_pass"
                log("weak_basis", "仅证明两遍读取稳定，跨侧对齐未证实（按配置采纳为弱结论）")
            else:
                reason_code = "alignment_unproven"
                log("weak_basis_rejected", REASON_TEXT["alignment_unproven"])
        else:
            if accept_weak:
                basis = "weak_two_pass"
            else:
                reason_code = "alignment_unproven"

    if reason_code is not None:
        reason = REASON_TEXT[reason_code]
        log("verdict", f"本次对比不可判定：{reason_code}（临时内容结论：{'相同' if candidate.content_equal else '有差异'}，不得作为最终结论）",
            verdict="deferred", reason_code=reason_code, provisional_equal=candidate.content_equal)
        return SegmentReport(
            tcfg.name, seg, total, mode, "deferred", candidate,
            reason_code, reason, None, None, watermarks, timeline,
        )

    verdict = "match" if candidate.content_equal else "diff"
    log("verdict", f"键段结论确定：{verdict}（依据 {basis}）", verdict=verdict, basis=basis)
    return SegmentReport(
        tcfg.name, seg, total, mode, verdict, candidate,
        basis=basis, watermarks=watermarks, timeline=timeline,
    )


def _refingerprint(
    source: Source, tcfg: TableConfig, resolved: ResolvedTable, seg: int, total: int
) -> str:
    from .engine import fingerprint_rows, type_row

    raw = rows_for_segment(source.fetch_all(tcfg.name), resolved, seg, total)
    typed = [type_row(r, resolved) for r in raw]
    return fingerprint_rows(typed, resolved.compare_columns)


def _hot_window(wm_a0: Watermark, wm_b0: Watermark, wm_end: Watermark, slack: float) -> bool:
    """Best-effort check using updated_at strings (ISO order is chronological)."""
    # The demo/configurable path uses event tokens; implement a conservative
    # version: if updated_at is present on either side we cannot cheaply map it
    # to the wall clock without parsing, so parse ISO and compare with now.
    times = [w.updated_at for w in (wm_a0, wm_b0, wm_end) if w and w.updated_at]
    if not times:
        return False  # no timestamp evidence -> caller falls through
    from datetime import datetime, timedelta

    newest: Optional[datetime] = None
    for t in times:
        try:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if newest is None or dt > newest:
            newest = dt
    if newest is None:
        return False
    now = datetime.now(timezone.utc)
    return newest >= now - timedelta(seconds=max(slack, 0.0))


def _empty_candidate(seg: int, total: int) -> SegmentCandidate:
    return SegmentCandidate(seg, total, 0, 0, "", "", [], 0, 0, 0, 0, 0, 0, 0)
