"""Zero-dependency HTTP API + static UI server (stdlib ``http.server``)."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

from . import __version__
from .config import ReconcileConfig
from .demo import DemoWriter, demo_paths, init_demo
from .manager import RunManager

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class AppContext:
    def __init__(self, config: ReconcileConfig, *, demo: bool = False,
                 old_path: Optional[str] = None, new_path: Optional[str] = None):
        self.config = config
        self.demo = demo
        self.old_path = old_path
        self.new_path = new_path
        self.manager = RunManager(config)
        self.writer: Optional[DemoWriter] = None
        if demo and old_path and new_path:
            self.writer = DemoWriter(config, old_path, new_path)

    def reset_demo(self) -> dict[str, Any]:
        if not (self.demo and self.old_path and self.new_path):
            raise RuntimeError("not a demo context")
        if self.writer is not None:
            self.writer.close()
        cfg = init_demo(self.old_path, self.new_path, force=True)
        self.config = cfg
        self.manager = RunManager(cfg)
        self.writer = DemoWriter(cfg, self.old_path, self.new_path)
        return {"reset": True}

    def prepare_snapshot(self) -> dict[str, Any]:
        """Stop writes, drain the A->B lag, return an aligned snapshot token."""
        w = self.writer
        if w is None:
            raise RuntimeError("writer unavailable")
        w.stop()
        w.flush_pending()
        deadline = time.time() + 5
        while time.time() < deadline:
            st = w.status()
            if st["global_seq_a"] == st["global_seq_b"] and st["pending_b"] == 0:
                return {"token": st["global_seq_a"], "writer": st}
            time.sleep(0.1)
        return {"token": None, "writer": w.status(),
                "error": "两侧事件位点在超时内未对齐，无法建立快照一致性"}


def build_handler(ctx: AppContext):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"relocheck/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
            pass

        # -- helpers ------------------------------------------------------
        def _send_json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _error(self, status: int, msg: str) -> None:
            self._send_json({"error": msg}, status)

        # -- GET ----------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            path = u.path
            q = parse_qs(u.query)
            try:
                if path == "/api/health":
                    return self._send_json({"ok": True, "version": __version__})
                if path == "/api/context":
                    return self._send_json(self._context_wire())
                if path == "/api/runs":
                    return self._send_json({"runs": ctx.manager.list_runs()})
                if path.startswith("/api/runs/"):
                    rest = path[len("/api/runs/"):]
                    if rest and "/" not in rest:
                        rec = ctx.manager.get(rest)
                        return self._send_json(rec.wire())
                if path == "/api/segment":
                    return self._segment_detail(q)
                if path == "/" or path == "/index.html":
                    return self._serve_static("index.html", "text/html; charset=utf-8")
                if path in ("/app.js", "/styles.css"):
                    ctype = "application/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
                    return self._serve_static(path.lstrip("/"), ctype)
                return self._error(404, f"unknown path {path}")
            except KeyError as e:
                return self._error(404, f"run/table not found: {e}")
            except Exception as e:  # pragma: no cover
                return self._error(500, f"{type(e).__name__}: {e}")

        # -- POST ---------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            path = u.path
            body = self._read_json()
            try:
                if path == "/api/runs":
                    rec = ctx.manager.create_run(
                        mode=body.get("mode", "live"),
                        strict_precision=bool(body.get("strict_precision", False)),
                        accept_weak=bool(body.get("accept_weak", False)),
                        snapshot_token=body.get("snapshot_token"),
                    )
                    return self._send_json({"run_id": rec.id})
                if path == "/api/recheck":
                    rep = ctx.manager.recheck(
                        body["run_id"], body["table"], int(body["segment"])
                    )
                    return self._send_json(rep.detail_wire())
                if path == "/api/writer":
                    return self._writer_action(body)
                if path == "/api/demo/reset":
                    return self._send_json(ctx.reset_demo())
                return self._error(404, f"unknown path {path}")
            except KeyError as e:
                return self._error(400, f"missing field: {e}")
            except Exception as e:
                return self._error(500, f"{type(e).__name__}: {e}")

        # -- pieces -------------------------------------------------------
        def _context_wire(self) -> dict[str, Any]:
            c = ctx.config
            return {
                "demo": ctx.demo,
                "source_a": {"name": c.source_a.name, "dsn": c.source_a.dsn},
                "source_b": {"name": c.source_b.name, "dsn": c.source_b.dsn},
                "tables": [
                    {"name": t.name, "business_key": t.business_key,
                     "segment_count": t.segment_count,
                     "version_column": t.version_column,
                     "updated_at_column": t.updated_at_column,
                     "event_log": t.event_log}
                    for t in c.tables
                ],
                "writer": ctx.writer.status() if ctx.writer else None,
            }

        def _segment_detail(self, q: dict[str, list[str]]) -> None:
            run_id = q["run_id"][0]
            table = q["table"][0]
            seg = int(q["segment"][0])
            rec = ctx.manager.get(run_id)
            rep = rec.reports[table][seg]
            if rep is None:
                return self._error(409, "segment not scanned yet")
            self._send_json(rep.detail_wire())

        def _writer_action(self, body: dict[str, Any]) -> None:
            w = ctx.writer
            if w is None:
                return self._error(400, "current context has no demo writer")
            action = body.get("action")
            if action == "start":
                w.start(
                    interval=float(body.get("interval", 2.4)),
                    lag=float(body.get("lag", 1.0)),
                )
            elif action == "stop":
                w.stop()
            elif action == "burst":
                info = w.burst(segment=body.get("segment"), lag=float(body.get("lag", 2.6)))
                return self._send_json({"burst": info, "writer": w.status()})
            elif action == "prepare_snapshot":
                info = ctx.prepare_snapshot()
                return self._send_json(info)
            else:
                return self._error(400, f"unknown writer action {action!r}")
            self._send_json({"writer": w.status()})

        def _serve_static(self, name: str, ctype: str) -> None:
            fp = os.path.join(WEB_DIR, name)
            if not os.path.isfile(fp):
                return self._error(404, f"web asset missing: {name}")
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def create_server(host: str = "127.0.0.1", port: int = 8765,
                  config: Optional[ReconcileConfig] = None,
                  demo_dir: Optional[str] = None,
                  auto_writer: bool = True) -> tuple[ThreadingHTTPServer, AppContext]:
    if config is None:
        old, new, _ = demo_paths(demo_dir)
        config = init_demo(old, new, force=not all(os.path.exists(p) for p in (old, new)))
        ctx = AppContext(config, demo=True, old_path=old, new_path=new)
        if auto_writer and ctx.writer is not None:
            ctx.writer.start()
    else:
        ctx = AppContext(config)
    httpd = ThreadingHTTPServer((host, port), build_handler(ctx))
    httpd.daemon_threads = True
    return httpd, ctx


def main() -> None:  # pragma: no cover
    import argparse

    p = argparse.ArgumentParser(prog="relocheck-server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--config", help="reconciliation JSON config; defaults to built-in demo DBs")
    p.add_argument("--demo-dir", default=None)
    p.add_argument("--no-writer", action="store_true")
    p.add_argument("--reset-demo", action="store_true")
    args = p.parse_args()

    if args.config:
        cfg = ReconcileConfig.load(args.config)
        ctx = AppContext(cfg)
        httpd = ThreadingHTTPServer((args.host, args.port), build_handler(ctx))
    else:
        old, new, _ = demo_paths(args.demo_dir)
        if args.reset_demo or not all(os.path.exists(x) for x in (old, new)):
            init_demo(old, new, force=True)
        cfg = ReconcileConfig.load(demo_paths(args.demo_dir)[2])
        ctx = AppContext(cfg, demo=True, old_path=old, new_path=new)
        if not args.no_writer:
            ctx.writer.start()
        httpd = ThreadingHTTPServer((args.host, args.port), build_handler(ctx))

    print(f"relocheck UI:  http://{args.host}:{args.port}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if ctx.writer:
            ctx.writer.close()
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
