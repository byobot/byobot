"""Read-only SQL over a store: the documented tables, an authorizer that keeps the rest out, and a time limit."""

from __future__ import annotations

import functools
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from ..util import ValidationFailed
from . import reducers
from .store import DDL, _statements


def ddl() -> str:
    """The store's SQL tables, for people and agents writing queries."""
    return "\n".join(s.strip() for s in _statements(DDL + reducers.DDL)) + "\n"


_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
# The store's own key and the paired devices' token hashes stay out of queries.
_UNREADABLE = {("meta", None), ("devices", "token_sha256")}


@functools.cache
def _readable() -> dict[str, frozenset[str]]:
    """The tables, views and columns `ddl()` documents, less those kept out; anything else in the file stays out."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(ddl())
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type IN ('table', 'view')")]
        found = {t: frozenset(c[1].lower() for c in conn.execute(f"PRAGMA table_xinfo({t})")
                              if (t, c[1]) not in _UNREADABLE) | {"rowid"}
                 for t in tables if (t, None) not in _UNREADABLE}
    finally:
        conn.close()
    schema = frozenset({"type", "name", "tbl_name", "rootpage", "sql"})
    return {**found, "sqlite_master": schema, "sqlite_schema": schema}


def _authorize(action: int, table: Any, column: Any, database: Any, *_: Any) -> int:
    # A read with no database is a row count or a CTE: it carries no column of a stored table.
    if action == sqlite3.SQLITE_READ and database is not None and column.lower() not in _readable().get(table, ()):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


PROGRESS_INTERVAL = 10_000  # SQLite steps between deadline checks


def limit_time(conn: sqlite3.Connection, seconds: float) -> None:
    """Stop statements on ``conn`` that run past ``seconds`` from now: SQLite raises ``OperationalError('interrupted')``."""
    deadline = time.monotonic() + seconds
    conn.set_progress_handler(lambda: time.monotonic() > deadline, PROGRESS_INTERVAL)


def interrupted(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "interrupt" in str(exc).lower()


def query(path: Path, sql: str, params: Iterable[Any] = (), limit: int = 1000, seconds: float = 10) -> dict:
    """Run one read-only SQL statement, stopped after `seconds`. Writes, ATTACH and PRAGMA are refused."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_authorize)
        limit_time(conn, seconds)
        try:
            cur = conn.execute(sql, tuple(params))
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(limit + 1)
        except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
            if interrupted(exc):
                raise ValidationFailed(f"query stopped after {seconds:g} s; narrow it or add a LIMIT") from None
            raise ValidationFailed(f"query refused: {exc}") from None
        rows = [{c: None if isinstance(v, float) and not math.isfinite(v) else v for c, v in zip(columns, r)}
                for r in fetched[:limit]]  # JSON has no Infinity or NaN
        return {"columns": columns, "rows": rows, "truncated": len(fetched) > limit}
    finally:
        conn.close()


def busy(exc: BaseException) -> bool:
    """Whether a store call failed only because another process held the write lock past the busy timeout."""
    return isinstance(exc, sqlite3.OperationalError) and str(exc).startswith(("database is locked", "database is busy"))
