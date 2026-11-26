"""Database sources.

A :class:`Source` is the small driver surface the engine needs: column metadata,
full table reads and simple watermark queries.  SQLite is supported out of the
box (the demo and tests use local ``.db`` files).  Other local/relational
databases can be reached through any DB-API 2.0 driver by DSN scheme
(``postgres://`` via ``psycopg2``, ``mysql://`` via ``pymysql`` ...) when the
driver happens to be installed.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from .values import TypeMeta, parse_sql_type


@dataclass
class ColumnMeta:
    name: str
    type_meta: Optional[TypeMeta]
    primary_key: bool = False


class Source:
    label: str

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def columns(self, table: str) -> list[ColumnMeta]:
        raise NotImplementedError

    def fetch_all(self, table: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_scalar(self, sql: str, params: tuple = ()) -> Any:
        raise NotImplementedError

    def fetch_dicts(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    # convenience --------------------------------------------------------
    def max_column(self, table: str, column: str, where: str = "", params: tuple = ()) -> Any:
        sql = f'SELECT MAX("{column}") FROM "{table}"'
        if where:
            sql += f" WHERE {where}"
        return self.fetch_scalar(sql, params)


class SQLiteSource(Source):
    def __init__(self, dsn: str, label: str = "A"):
        self.label = label
        path = _sqlite_path(dsn)
        if path != ":memory:" and not os.path.exists(path):
            # An empty sqlite file would otherwise be created silently and
            # produce a confusing "no such table".
            raise FileNotFoundError(f"[{label}] SQLite database file not found: {path}")
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def columns(self, table: str) -> list[ColumnMeta]:
        rows = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not rows:
            raise ValueError(f"[{self.label}] table not found: {table}")
        out: list[ColumnMeta] = []
        for r in rows:
            out.append(
                ColumnMeta(
                    name=r["name"],
                    type_meta=parse_sql_type(r["type"]),
                    primary_key=bool(r["pk"]),
                )
            )
        return out

    def fetch_all(self, table: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(f'SELECT * FROM "{table}"')
        return [dict(r) for r in cur.fetchall()]

    def fetch_scalar(self, sql: str, params: tuple = ()) -> Any:
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return row[0]

    def fetch_dicts(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


class DbApiSource(Source):
    """Generic DB-API source for postgres/mysql/etc.

    The DSN scheme selects the driver module if it is importable.  Identifiers
    are quoted with ANSI double quotes; parameters use the driver's
    ``paramstyle`` via a tiny translation from ``?`` placeholders.
    """

    _DRIVERS = {
        "postgres": ("psycopg2", "postgresql"),
        "postgresql": ("psycopg2", "postgresql"),
        "mysql": ("pymysql", None),
        "mariadb": ("pymysql", None),
    }

    def __init__(self, dsn: str, label: str = "A"):
        self.label = label
        scheme = urlparse(dsn).scheme.lower()
        if scheme not in self._DRIVERS:
            raise ValueError(f"unsupported DSN scheme: {scheme!r} ({dsn})")
        mod_name, alt = self._DRIVERS[scheme]
        try:
            mod = importlib.import_module(mod_name)
        except ImportError as e:  # pragma: no cover - depends on env
            raise RuntimeError(
                f"DSN {scheme}:// requires the {mod_name} driver: pip install {mod_name}"
            ) from e
        self._paramstyle = getattr(mod, "paramstyle", "qmark")
        self._conn = mod.connect(dsn=dsn) if mod_name == "psycopg2" else mod.connect(host=_h(dsn))

    def close(self) -> None:  # pragma: no cover
        self._conn.close()

    def _q(self, sql: str) -> str:  # pragma: no cover
        if self._paramstyle in ("format", "pyformat"):
            return sql.replace("?", "%s")
        if self._paramstyle == "numeric":
            i = 0

            def repl(_m):
                nonlocal i
                i += 1
                return f":{i}"

            import re

            return re.sub(r"\?", repl, sql)
        return sql

    def columns(self, table: str) -> list[ColumnMeta]:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns WHERE table_name = ? ORDER BY ordinal_position",
            (table,),
        )
        rows = cur.fetchall()
        return [
            ColumnMeta(
                name=r[0],
                type_meta=parse_sql_type(
                    f"{r[1]}({r[2]},{r[3]})" if r[3] is not None and r[2] is not None else r[1]
                ),
            )
            for r in rows
        ]

    def fetch_all(self, table: str) -> list[dict[str, Any]]:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def fetch_scalar(self, sql: str, params: tuple = ()) -> Any:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(self._q(sql), params)
        row = cur.fetchone()
        return None if row is None else row[0]

    def fetch_dicts(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:  # pragma: no cover
        cur = self._conn.cursor()
        cur.execute(self._q(sql), params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _h(dsn: str) -> dict[str, Any]:  # pragma: no cover
    u = urlparse(dsn)
    return {
        "host": u.hostname,
        "port": u.port or 3306,
        "user": u.username,
        "password": u.password,
        "database": (u.path or "/").lstrip("/"),
    }


def _sqlite_path(dsn: str) -> str:
    if dsn.startswith("sqlite:////"):
        return "/" + dsn[len("sqlite:////"):]
    if dsn.startswith("sqlite:///"):
        return dsn[len("sqlite:///"):]  # relative path
    if dsn.startswith("sqlite://"):
        rest = dsn[len("sqlite://"):]
        return rest.lstrip("/")
    return dsn


def open_source(dsn: str, label: str = "A") -> Source:
    """Open a source from a DSN.  Bare paths / ``sqlite://`` are SQLite."""
    if "://" not in dsn or dsn.startswith("sqlite:"):
        return SQLiteSource(dsn, label)
    return DbApiSource(dsn, label)
