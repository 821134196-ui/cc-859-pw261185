"""Command line interface: run segment-first reconciliation in a terminal."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .config import ReconcileConfig
from .demo import demo_paths, init_demo
from .manager import RunManager

_VERDICT_BADGE = {
    "match": "✅ 确定相同",
    "diff": "❌ 确定有差异",
    "deferred": "⏳ 延期复核(不可判定)",
}


def _print_segment_line(s: dict[str, Any]) -> None:
    badge = _VERDICT_BADGE.get(s["verdict"], s["verdict"])
    extra = (
        f"键 {s['keys_match']} 同/{s['keys_diff']} 异 "
        f"(缺B {s['missing_b']}, 多B {s['extra_b']}, 同键多行组 {s['duplicate_groups']})"
    )
    line = f"  段 #{s['index']:<2} {badge:<22} 行 {s['rows_a']}/{s['rows_b']}  {extra}"
    if s["verdict"] == "deferred":
        line += f"  原因={s['reason_code']}"
        line += f"  [临时内容: {'相同' if s['provisional_content'] == 'match' else '有差异'} — 不得作为结论]"
    elif s.get("basis"):
        line += f"  依据={s['basis']}"
    if s.get("precision_warnings"):
        line += f"  精度提示×{s['precision_warnings']}"
    print(line)


def print_run_report(rec: Any, *, cfg: Optional[ReconcileConfig] = None,
                     segment: Optional[int] = None, as_json: bool = False) -> None:
    if as_json:
        if segment is not None:
            for table, arr in rec.reports.items():
                rep = arr[segment]
                print(json.dumps(rep.detail_wire(), ensure_ascii=False, indent=2, default=str))
                return
        print(json.dumps(rec.wire(), ensure_ascii=False, indent=2, default=str))
        return

    totals = rec.totals()
    print(f"\n校对运行 {rec.id}  模式={rec.params.get('mode')}  状态={rec.status}")
    if rec.error:
        print(f"运行错误: {rec.error}")
    if rec.schema_drift:
        print("结构/精度漂移:")
        for d in rec.schema_drift:
            print(f"  - {d['table']}.{d['column']}: {d.get('a_type')}  →  {d.get('b_type')} ({d['kind']})")

    for table, arr in rec.reports.items():
        bk = ", ".join(cfg.table(table).business_key) if cfg else "?"
        print(f"\n表 {table} （业务键: {bk}，共 {len(arr)} 段）")
        for rep in arr:
            if rep is None:
                print("  段 <pending>")
                continue
            _print_segment_line(rep.summary_wire())

    print("\n汇总:")
    print(f"  确定相同段 {totals['match']} | 确定差异段 {totals['diff']} | "
          f"延期复核段 {totals['deferred']} / 共 {totals['segments_total']}")
    if totals["deferred"]:
        print(f"  其中临时内容恰好相同的延期段 {totals['provisional_equal_among_deferred']} 个"
              " —— 系统不会把它们计为相同。")
    overall_cn = {
        "verified_equal": "✅ 所有键段确定一致",
        "differences_found": "❌ 已确定存在差异",
        "incomplete_deferred": "⏳ 存在不可判定键段，整体结论：不能判定为一致，需延期复核",
        "incomplete_with_diffs": "⚠️ 已发现确定差异，同时仍有键段不可判定，需延期复核其余键段",
        "running": "运行中",
        "empty": "空",
    }.get(totals["overall"], totals["overall"])
    print(f"  总结论: {overall_cn}")

    if segment is not None:
        print_segment_evidence(rec, segment)


def print_segment_evidence(rec: Any, seg: int) -> None:
    for table, arr in rec.reports.items():
        rep = arr[seg]
        if rep is None:
            continue
        d = rep.detail_wire()
        print(f"\n行级证据 —— 表 {table} 段 #{seg}  判定={d['verdict']}")
        if d.get("reason"):
            print(f"不可判定原因: {d['reason']}")
        for ev in d["evidence"]:
            if ev["status"] == "match" and not ev["duplicate_group"]:
                continue
            print(f"\n  业务键 {ev['key_display']}  状态={ev['status']}  "
                  f"行数 A/B={ev['multiplicity_a']}/{ev['multiplicity_b']}"
                  f"{'  [同键多行组]' if ev['duplicate_group'] else ''}")
            for row in ev["row_evidence"]:
                disp = row["disposition"]
                if disp == "only_a":
                    print("    └─ 仅 A 有行:", _compact(row["values_a"]))
                elif disp == "only_b":
                    print("    └─ 仅 B 有行:", _compact(row["values_b"]))
                else:
                    hard = [c for c in row["column_diffs"] if not c["equal"]]
                    soft = [c for c in row["column_diffs"] if c["equal"]]
                    for c in hard:
                        print(f"    └─ 列 {c['column']}: A={_v(c['a'])}  B={_v(c['b'])}  [{c['code']}]")
                    for c in soft:
                        print(f"    · 列 {c['column']}: 数值相等但 {c['code']} "
                              f"(A {_v(c['a'])} / B {_v(c['b'])})")


def _v(wire: dict[str, Any]) -> str:
    if wire["kind"] == "null":
        return "NULL"
    return f"{wire['v']}({wire['kind']})"


def _compact(values: Optional[dict[str, dict[str, Any]]]) -> str:
    if not values:
        return ""
    keep = []
    for col, w in values.items():
        keep.append(f"{col}={_v(w)}")
    return "{" + ", ".join(keep[:6]) + (" ..." if len(keep) > 6 else "") + "}"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="relocheck", description="关系库切换校对工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-demo", help="创建内置演示库（含植入差异与持续写入器）")
    p_init.add_argument("--dir", default=None)

    p_run = sub.add_parser("run", help="按配置执行一次分段校对")
    p_run.add_argument("--config", help="JSON 配置；也可用 --a/--b/--table 临时指定")
    p_run.add_argument("--a", dest="dsn_a", help="A 库 DSN（本地路径或 sqlite:///…）")
    p_run.add_argument("--b", dest="dsn_b", help="B 库 DSN")
    p_run.add_argument("--table", help="临时模式下要校对的表")
    p_run.add_argument("--business-key", help="逗号分隔的业务键列，如 order_no 或 k1,k2")
    p_run.add_argument("--segment-count", type=int, default=8)
    p_run.add_argument("--mode", choices=["live", "snapshot"], default="live")
    p_run.add_argument("--snapshot-token", type=int, default=None)
    p_run.add_argument("--strict-precision", action="store_true")
    p_run.add_argument("--accept-weak", action="store_true")
    p_run.add_argument("--segment", type=int, default=None, help="展开指定段的行级证据")
    p_run.add_argument("--json", action="store_true")
    p_run.add_argument("--out", help="把完整 JSON 报告写入文件")

    p_serve = sub.add_parser("serve", help="启动 Web 界面 / API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--config", default=None)
    p_serve.add_argument("--demo-dir", default=None)
    p_serve.add_argument("--no-writer", action="store_true")
    p_serve.add_argument("--reset-demo", action="store_true", help="启动前重建演示库")

    args = p.parse_args(argv)

    if args.cmd == "init-demo":
        old, new, cfgp = demo_paths(args.dir)
        init_demo(old, new, force=True)
        print(f"旧库 A: {old}\n新库 B: {new}\n配置 : {cfgp}")
        return 0

    if args.cmd == "serve":
        from .server import create_server, main as server_main  # noqa: F401
        sys.argv = ["relocheck-server", "--host", args.host, "--port", str(args.port)]
        if args.config:
            sys.argv += ["--config", args.config]
        if args.demo_dir:
            sys.argv += ["--demo-dir", args.demo_dir]
        if args.no_writer:
            sys.argv += ["--no-writer"]
        if args.reset_demo:
            sys.argv += ["--reset-demo"]
        server_main()
        return 0

    if args.cmd == "run":
        if args.config:
            cfg = ReconcileConfig.load(args.config)
        else:
            missing = [n for n, v in (("--a", args.dsn_a), ("--b", args.dsn_b),
                                      ("--table", args.table),
                                      ("--business-key", args.business_key)) if not v]
            if missing:
                p.error("未提供 --config 时，必须同时给出 " + " ".join(missing))
            from .config import ConnectionConfig, TableConfig
            bk = [c.strip() for c in args.business_key.split(",") if c.strip()]
            cfg = ReconcileConfig(
                source_a=ConnectionConfig("A", args.dsn_a),
                source_b=ConnectionConfig("B", args.dsn_b),
                tables=[TableConfig(name=args.table, business_key=bk,
                                    segment_count=args.segment_count)],
            )
        mgr = RunManager(cfg)
        rec = mgr.create_run(
            mode=args.mode,
            snapshot_token=args.snapshot_token,
            strict_precision=args.strict_precision,
            accept_weak=args.accept_weak,
            run_in_background=False,
        )
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(rec.wire(), f, ensure_ascii=False, indent=2, default=str)
        print_run_report(rec, cfg=cfg, segment=args.segment, as_json=args.json)
        if args.out and not args.json:
            print(f"\n完整 JSON 报告已写入 {args.out}")
        # Non-zero exit when definitive differences exist (CI-friendly);
        # deferrals are NOT a clean pass either -> exit 3.
        t = rec.totals()
        if rec.error:
            return 2
        if t["deferred"]:
            return 3
        if t["diff"]:
            return 1
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
