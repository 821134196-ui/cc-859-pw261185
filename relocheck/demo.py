"""Deterministic two-DB demo environment and a controllable live writer.

The seed data deliberately contains every comparison trap the tool must not
paper over:

* physical primary keys that do not line up (aligned via ``order_no``);
* duplicate business-key groups (identical multiset vs extra/missing rows);
* NULL vs empty string / vs the text ``'NULL'`` / NULL on both sides;
* exact decimal differences, near-precision differences, float binary noise;
* declared scale drift ``DECIMAL(14,2)`` -> ``DECIMAL(14,4)``;
* numeric-looking text keys ``'00421'`` vs ``'421'`` that must not collapse.

A separate ``HOT-xx`` row pool is all the continuous writer touches so the
curated evidence never mutates.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import ConnectionConfig, ReconcileConfig, TableConfig
from .engine import segment_index
from .values import Key, Kind, TypeMeta, TypedValue

DEMO_SEGMENTS = 8
OLD_DB = "old_relational.db"
NEW_DB = "new_relational.db"
CONFIG_NAME = "relochk.demo.json"

SCHEMA_A = """
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    order_no    TEXT NOT NULL,
    customer    TEXT,
    amount      DECIMAL(14,2),
    rate        DECIMAL(10,6),
    ratio       DOUBLE,
    currency    TEXT(3),
    remark      TEXT,
    touch_count INTEGER NOT NULL DEFAULT 0,
    payload     TEXT,
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);
CREATE TABLE sync_events (
    seq     INTEGER PRIMARY KEY,
    segment INTEGER NOT NULL,
    order_no TEXT NOT NULL,
    op      TEXT NOT NULL,
    at      TEXT NOT NULL
);
"""

SCHEMA_B = SCHEMA_A.replace("amount      DECIMAL(14,2)", "amount      DECIMAL(14,4)")


def seg_of_order_no(order_no: str, n: int = DEMO_SEGMENTS) -> int:
    k = Key((TypedValue(Kind.TEXT, order_no, TypeMeta("TEXT", Kind.TEXT)),))
    return segment_index(k, n)


def demo_dir(base: Optional[str] = None) -> str:
    d = base or os.path.join(os.getcwd(), "demo_data")
    os.makedirs(d, exist_ok=True)
    return d


def demo_paths(base: Optional[str] = None) -> tuple[str, str, str]:
    d = demo_dir(base)
    return os.path.join(d, OLD_DB), os.path.join(d, NEW_DB), os.path.join(d, CONFIG_NAME)


def build_demo_config(old_path: str, new_path: str) -> ReconcileConfig:
    # Bare absolute paths are valid SQLite DSNs for this tool.
    return ReconcileConfig(
        source_a=ConnectionConfig("旧库 A", os.path.abspath(old_path)),
        source_b=ConnectionConfig("新库 B", os.path.abspath(new_path)),
        tables=[
            TableConfig(
                name="orders",
                business_key=["order_no"],
                segment_count=DEMO_SEGMENTS,
                version_column="version",
                updated_at_column="updated_at",
                event_log={"table": "sync_events", "seq_column": "seq", "segment_column": "segment"},
            )
        ],
    )


def write_config_file(cfg: ReconcileConfig, path: str) -> None:
    d = {
        "source_a": {"name": cfg.source_a.name, "dsn": cfg.source_a.dsn},
        "source_b": {"name": cfg.source_b.name, "dsn": cfg.source_b.dsn},
        "tables": [
            {
                "name": t.name,
                "business_key": t.business_key,
                "segment_count": t.segment_count,
                "version_column": t.version_column,
                "updated_at_column": t.updated_at_column,
                "event_log": t.event_log,
            }
            for t in cfg.tables
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _row(order_no: str, **kw: Any) -> dict[str, Any]:
    base = dict(
        order_no=order_no, customer="CUST-000", amount="100.00", rate="0.250000",
        ratio=0.5, currency="CNY", remark=None, touch_count=0, payload="seed",
        version=1, updated_at="2026-01-01T00:00:00+00:00",
    )
    base.update(kw)
    return base


def _common_rows() -> list[dict[str, Any]]:
    rnd = random.Random(20261126)
    rows: list[dict[str, Any]] = []
    for i in range(1, 161):
        amt = round(10 + (i * 37.719) % 8800, 2)
        rows.append(_row(
            f"ORD-{i:04d}",
            customer=f"CUST-{1 + (i % 24):03d}",
            amount=f"{amt:.2f}",
            rate=f"{rnd.uniform(0.01, 0.99):.6f}",
            ratio=rnd.uniform(0, 1),
            currency=["CNY", "USD", "EUR"][i % 3],
            remark=None if i % 4 else f"备注 {i}",
        ))
    for i in range(DEMO_SEGMENTS * 3):
        rows.append(_row(f"HOT-{i:02d}", customer="HOTPOOL", amount="1.00", payload="hot"))
    return rows


def _planted_shared() -> list[dict[str, Any]]:
    """Fixture rows inserted identically into BOTH sides before B mutations."""
    return [
        _row("FIX-NULL-EMPTY", customer="NULL对空串", remark=None, amount="12.00"),
        _row("FIX-NULL-STR", customer="NULL对文本NULL", remark=None, amount="13.00"),
        _row("FIX-NULL-BOTH", customer="两侧皆NULL", remark=None, amount="14.00"),
        _row("FIX-AMT-DIFF", customer="金额差异", amount="88.80"),
        _row("FIX-RATE-NEAR", customer="精度近邻差异", rate="0.100000"),
        _row("FIX-RATIO-FP", customer="浮点二进制差异", ratio=0.1 + 0.2),
        _row("FIX-SCALE", customer="标度漂移", amount="42.00"),
        # duplicate key with identical multiset on both sides
        _row("FIX-DUP-SAME", customer="DUP-A", amount="1.00"),
        _row("FIX-DUP-SAME", customer="DUP-B", amount="2.00"),
        # duplicate key, two identical base rows on both sides
        _row("FIX-DUP-EXTRA", customer="DUP2-A1", amount="3.00"),
        _row("FIX-DUP-EXTRA", customer="DUP2-A2", amount="4.00"),
        _row("FIX-DUP-MISSING", customer="DUP3-A1", amount="5.00"),
        _row("FIX-DUP-MISSING", customer="DUP3-A2", amount="6.00"),
    ]


def _planted_a_only() -> list[dict[str, Any]]:
    return [
        _row("FIX-MISSING-B", customer="仅旧库存在", amount="7.77"),
        # third row of the DUP-MISSING group exists only on A
        _row("FIX-DUP-MISSING", customer="DUP3-A3-旧库多余", amount="7.00"),
        # numeric-looking TEXT business key with leading zero
        _row("00421", customer="前导零文本键", amount="5.00"),
    ]


def _planted_b_only() -> list[dict[str, Any]]:
    return [
        _row("FIX-EXTRA-B", customer="仅新库存在", amount="8.88"),
        # third row of the DUP-EXTRA group exists only on B
        _row("FIX-DUP-EXTRA", customer="DUP2-B3-新库多余", amount="4.50"),
        # deliberately different TEXT key — must not align with '00421'
        _row("421", customer="去前导零文本键", amount="5.00"),
    ]


def init_demo(old_path: str, new_path: str, *, force: bool = True) -> ReconcileConfig:
    for p in (old_path, new_path):
        if force and os.path.exists(p):
            os.remove(p)
        for suffix in ("-wal", "-shm", "-journal"):
            side = p + suffix
            if force and os.path.exists(side):
                os.remove(side)

    ca = sqlite3.connect(old_path)
    cb = sqlite3.connect(new_path)
    try:
        ca.executescript(SCHEMA_A)
        cb.executescript(SCHEMA_B)

        common = _common_rows()
        shared_planted = _planted_shared()
        a_rows = [dict(r) for r in common] + [dict(r) for r in shared_planted] + _planted_a_only()
        b_rows = [dict(r) for r in common] + [dict(r) for r in shared_planted]

        # Apply planted B-side mutations.
        for r in b_rows:
            if r["order_no"] == "FIX-NULL-EMPTY":
                r["remark"] = ""
            elif r["order_no"] == "FIX-NULL-STR":
                r["remark"] = "NULL"
            elif r["order_no"] == "FIX-AMT-DIFF":
                r["amount"] = "88.08"
            elif r["order_no"] == "FIX-RATE-NEAR":
                r["rate"] = "0.100001"
            elif r["order_no"] == "FIX-RATIO-FP":
                r["ratio"] = 0.3

        b_rows.extend(_planted_b_only())

        _insert_rows(ca, a_rows, id_offset=0)
        _insert_rows(cb, b_rows, id_offset=10000)

        # Baseline event token for every segment, identical on both sides.
        now = "2026-01-01T00:00:00+00:00"
        for seg in range(DEMO_SEGMENTS):
            for c in (ca, cb):
                c.execute(
                    "INSERT INTO sync_events(seq, segment, order_no, op, at) VALUES (?,?,?,?,?)",
                    (seg + 1, seg, "__baseline__", "BASE", now),
                )
        ca.commit(); cb.commit()
        for c in (ca, cb):
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
    finally:
        ca.close(); cb.close()

    cfg = build_demo_config(old_path, new_path)
    write_config_file(cfg, demo_paths(os.path.dirname(old_path))[2])
    return cfg


_INSERT_COLS = [
    "order_no", "customer", "amount", "rate", "ratio", "currency", "remark",
    "touch_count", "payload", "version", "updated_at",
]


def _insert_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]], id_offset: int) -> None:
    collist = ", ".join(_INSERT_COLS)
    qs = ", ".join("?" for _ in _INSERT_COLS)
    id_sql = ""
    for i, r in enumerate(rows, start=1):
        vals = [r[c] for c in _INSERT_COLS]
        if id_offset:
            conn.execute(
                f"INSERT INTO orders(id, {collist}) VALUES (?, {qs})",
                [id_offset + i] + vals,
            )
        else:
            conn.execute(f"INSERT INTO orders({collist}) VALUES ({qs})", vals)
    conn.commit()


# ---------------------------------------------------------------------------
# Continuous writer
# ---------------------------------------------------------------------------


class DemoWriter:
    """Writes paired mutations to A then B with a replication-lag window."""

    def __init__(self, cfg: ReconcileConfig, old_path: str, new_path: str):
        self.cfg = cfg
        self.old_path = old_path
        self.new_path = new_path
        self._lock = threading.RLock()
        self._ca = sqlite3.connect(old_path, check_same_thread=False)
        self._cb = sqlite3.connect(new_path, check_same_thread=False)
        for c in (self._ca, self._cb):
            c.execute("PRAGMA busy_timeout=5000")
        self.seq = int(self._ca.execute("SELECT COALESCE(MAX(seq),0) FROM sync_events").fetchone()[0])
        self.running = False
        self.interval = 2.4
        self.lag = 1.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pending_events: list[threading.Event] = []
        self.last: dict[str, Any] = {}
        self.writes = 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            pending = sum(1 for e in self._pending_events if not e.is_set())
            ga = self._safe_max(self._ca)
            gb = self._safe_max(self._cb)
            return {
                "running": self.running,
                "interval_seconds": self.interval,
                "lag_seconds": self.lag,
                "next_seq": self.seq + 1,
                "global_seq_a": ga,
                "global_seq_b": gb,
                "pending_b": pending,
                "writes": self.writes,
                "last": self.last,
            }

    def _safe_max(self, c: sqlite3.Connection) -> Optional[int]:
        try:
            return c.execute("SELECT COALESCE(MAX(seq),0) FROM sync_events").fetchone()[0]
        except sqlite3.Error:
            return None

    def start(self, interval: float = 2.4, lag: float = 1.0) -> None:
        if self.running:
            return
        self.interval = interval
        self.lag = lag
        self._stop.clear()
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="demo-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop scheduling new writes; finish the in-flight paired write."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self.lag + 3)
        self.running = False

    def close(self) -> None:
        self.stop()
        with self._lock:
            self._ca.close(); self._cb.close()

    def flush_pending(self, timeout: float = 6.0) -> bool:
        deadline = time.time() + timeout
        for e in list(self._pending_events):
            remaining = max(0.0, deadline - time.time())
            e.wait(remaining)
        return all(e.is_set() for e in self._pending_events)

    # -- operations -------------------------------------------------------
    def _hot_order(self) -> str:
        n = DEMO_SEGMENTS * 3
        return f"HOT-{random.randrange(n):02d}"

    def _loop(self) -> None:
        while not self._stop.is_set():
            order_no = self._hot_order()
            try:
                self.apply_paired(order_no, lag=self.lag)
            except Exception as e:  # pragma: no cover
                self.last = {"error": repr(e)}
            # interruptible interval wait
            end = time.time() + self.interval
            while time.time() < end and not self._stop.is_set():
                time.sleep(0.1)

    def apply_paired(self, order_no: str, lag: float = 0.8) -> int:
        seq, at, payload = self._write_a(order_no)
        time.sleep(lag)
        self._write_b(order_no, seq, at, payload)
        return seq

    def burst(self, segment: Optional[int] = None, lag: float = 2.6) -> dict[str, Any]:
        """Write A immediately, defer B by ``lag`` seconds (visible lag window)."""
        if segment is None:
            order_no = self._hot_order()
        else:
            candidates = [f"HOT-{i:02d}" for i in range(DEMO_SEGMENTS * 3)
                           if seg_of_order_no(f"HOT-{i:02d}") == segment]
            order_no = random.choice(candidates) if candidates else self._hot_order()
        seq, at, payload = self._write_a(order_no)
        done = threading.Event()
        with self._lock:
            self._pending_events.append(done)

        def delayed_b() -> None:
            try:
                time.sleep(lag)
                self._write_b(order_no, seq, at, payload)
            finally:
                done.set()

        threading.Thread(target=delayed_b, daemon=True, name=f"burst-{seq}").start()
        return {"seq": seq, "order_no": order_no, "segment": seg_of_order_no(order_no), "b_after_seconds": lag}

    def _next_seq(self) -> int:
        with self._lock:
            self.seq += 1
            return self.seq

    def _write_a(self, order_no: str) -> tuple[int, str, str]:
        seq = self._next_seq()
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        # Payload is a deterministic function of the shared event sequence so
        # B's replay is logically identical; only the ignored updated_at
        # watermark is allowed to differ.
        payload = f"write#{seq}"
        seg = seg_of_order_no(order_no)
        with self._lock, self._ca:
            self._ca.execute(
                'UPDATE orders SET touch_count=touch_count+1, payload=?, '
                'version=version+1, updated_at=? WHERE order_no=?',
                (payload, at, order_no),
            )
            self._ca.execute(
                "INSERT INTO sync_events(seq, segment, order_no, op, at) VALUES (?,?,?,?,?)",
                (seq, seg, order_no, "UPDATE", at),
            )
            self.writes += 1
            self.last = {"seq": seq, "order_no": order_no, "segment": seg, "at": at, "side": "A"}
        return seq, at, payload

    def _write_b(self, order_no: str, seq: int, at: Optional[str] = None,
                 payload: Optional[str] = None) -> None:
        at = at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        payload = payload or f"write#{seq}"
        seg = seg_of_order_no(order_no)
        with self._lock, self._cb:
            self._cb.execute(
                'UPDATE orders SET touch_count=touch_count+1, payload=?, '
                'version=version+1, updated_at=? WHERE order_no=?',
                (payload, at, order_no),
            )
            self._cb.execute(
                "INSERT INTO sync_events(seq, segment, order_no, op, at) VALUES (?,?,?,?,?)",
                (seq, seg, order_no, "UPDATE", at),
            )
            self.last = {"seq": seq, "order_no": order_no, "segment": seg, "at": at, "side": "B"}
