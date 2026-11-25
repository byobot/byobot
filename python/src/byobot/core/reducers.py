"""Per-record projections: each kind's reducer rewrites that one record's rows from its current content."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Callable

from .contract import Contract, instant

DDL = """
CREATE TABLE IF NOT EXISTS activities (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, movement_type TEXT, fatigue INTEGER, tags TEXT, equipment TEXT);
CREATE TABLE IF NOT EXISTS locations (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT, starts_on TEXT NOT NULL, ends_on TEXT);
CREATE TABLE IF NOT EXISTS planned_sessions (
  id TEXT PRIMARY KEY, plan_id TEXT, scheduled_on TEXT NOT NULL, name TEXT NOT NULL, location_id TEXT,
  status TEXT);
CREATE INDEX IF NOT EXISTS planned_sessions_day ON planned_sessions (scheduled_on);
CREATE TABLE IF NOT EXISTS planned_blocks (
  planned_session_id TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT, rounds INTEGER,
  PRIMARY KEY (planned_session_id, id));
CREATE TABLE IF NOT EXISTS planned_efforts (
  planned_session_id TEXT NOT NULL, block_id TEXT NOT NULL, id TEXT NOT NULL, position INTEGER NOT NULL,
  activity_id TEXT NOT NULL, side TEXT, PRIMARY KEY (planned_session_id, id));
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, plan_id TEXT, planned_session_id TEXT, name TEXT, location_id TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER, offset_min INTEGER NOT NULL,
  local_date TEXT NOT NULL, zone TEXT, status TEXT, modality TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_start ON sessions (start_ms);
CREATE INDEX IF NOT EXISTS sessions_day ON sessions (local_date);
CREATE TABLE IF NOT EXISTS efforts (
  id TEXT PRIMARY KEY, session_id TEXT, activity_id TEXT NOT NULL, planned_session_id TEXT,
  planned_effort_id TEXT, round INTEGER, side TEXT, location_id TEXT, performed_at TEXT NOT NULL, ended_at TEXT,
  start_ms INTEGER NOT NULL, end_ms INTEGER, offset_min INTEGER NOT NULL, local_date TEXT NOT NULL, zone TEXT,
  status TEXT, modality TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS efforts_start ON efforts (start_ms);
CREATE INDEX IF NOT EXISTS efforts_day ON efforts (local_date);
CREATE INDEX IF NOT EXISTS efforts_session ON efforts (session_id);
CREATE INDEX IF NOT EXISTS efforts_planned ON efforts (planned_session_id, planned_effort_id);
CREATE TABLE IF NOT EXISTS observations (
  id TEXT PRIMARY KEY, code TEXT NOT NULL, value REAL, text TEXT, unit TEXT, started_at TEXT NOT NULL,
  ended_at TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER, offset_min INTEGER NOT NULL, local_date TEXT NOT NULL,
  zone TEXT, modality TEXT NOT NULL, std_value REAL, std_unit TEXT);
CREATE INDEX IF NOT EXISTS observations_code ON observations (code, start_ms);
CREATE INDEX IF NOT EXISTS observations_day ON observations (local_date);
CREATE TABLE IF NOT EXISTS metrics (
  owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, part_id TEXT NOT NULL DEFAULT '', code TEXT NOT NULL,
  value REAL, min REAL, max REAL, text TEXT, unit TEXT, std_value REAL, std_min REAL, std_max REAL, std_unit TEXT,
  PRIMARY KEY (owner_kind, owner_id, part_id, code));
"""

SHAPE = hashlib.sha256(DDL.encode()).hexdigest()[:16]
Reducer = Callable[[sqlite3.Connection, Contract, str, dict], None]
TABLES = {
    "activity": ("activities",), "location": ("locations",), "setting": ("settings",), "plan": ("plans",),
    "planned_session": ("planned_sessions", "planned_blocks", "planned_efforts"), "session": ("sessions",),
    "effort": ("efforts",), "observation": ("observations",),
}
OWNER_COLUMN = {"planned_blocks": "planned_session_id", "planned_efforts": "planned_session_id"}


def project(conn: sqlite3.Connection, c: Contract, kind: str, rid: str, payload: dict | None) -> None:
    """Replace one record's rows: from `payload`, or none when it isn't live."""
    for table in TABLES.get(kind, ()):
        conn.execute(f"DELETE FROM {table} WHERE {OWNER_COLUMN.get(table, 'id')} = ?", (rid,))
    conn.execute("DELETE FROM metrics WHERE owner_kind = ? AND owner_id = ?", (kind, rid))
    if payload is not None and kind in REDUCERS:
        REDUCERS[kind](conn, c, rid, payload)


def clear(conn: sqlite3.Connection) -> None:
    for table in {t for ts in TABLES.values() for t in ts} | {"metrics"}:
        conn.execute(f"DELETE FROM {table}")


def drop(conn: sqlite3.Connection) -> None:
    """Remove the derived tables, for a store written by a build whose tables had another shape."""
    for table in {t for ts in TABLES.values() for t in ts} | {"metrics"}:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _ms(ts: str | None) -> int | None:
    return None if ts is None else int(instant(ts).timestamp() * 1000)


def _when(t: dict) -> tuple:
    """start, end, start_ms, end_ms, offset_min, local_date, zone: the local date is the start's own, as written."""
    start = instant(t["start"])
    offset = int(start.utcoffset().total_seconds() // 60)
    return (t["start"], t.get("end"), _ms(t["start"]), _ms(t.get("end")), offset, start.date().isoformat(),
            t.get("zone"))


def _link(p: dict, name: str) -> dict:
    return (p.get("links") or {}).get(name) or {}


def _std(c: Contract, code: str, m: dict) -> tuple:
    """The value, min and max in the code's own unit, and that unit: None where they can't be converted."""
    values = tuple(c.canonical(code, m, k) for k in ("value", "min", "max"))
    unit = c.registry["codes"].get(code, {}).get("unit") if any(v is not None for v in values) else None
    return (*values, unit)


def _metrics(conn: sqlite3.Connection, c: Contract, kind: str, rid: str, metrics: dict | None, part: str = "") -> None:
    for code, m in (metrics or {}).items():
        value = m.get("value")
        conn.execute("INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            kind, rid, part, code, value if not isinstance(value, str) else None, m.get("min"), m.get("max"),
            value if isinstance(value, str) else None, m.get("unit"), *_std(c, code, m)))


def _activity(conn, c, rid, p):
    conn.execute("INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?)", (
        rid, p["name"], p.get("movement_type"), p.get("fatigue"), _json(p.get("tags")), _json(p.get("equipment"))))


def _location(conn, c, rid, p):
    conn.execute("INSERT INTO locations VALUES (?, ?)", (rid, p["name"]))


def _setting(conn, c, rid, p):
    conn.execute("INSERT INTO settings VALUES (?, ?)", (rid, _json(p.get("value"))))


def _plan(conn, c, rid, p):
    conn.execute("INSERT INTO plans VALUES (?, ?, ?, ?, ?)",
                 (rid, p["name"], p.get("goal"), p["starts_on"], p.get("ends_on")))


def _planned_session(conn, c, rid, p):
    conn.execute("INSERT INTO planned_sessions VALUES (?, ?, ?, ?, ?, ?)", (
        rid, _link(p, "part_of").get("id"), p["scheduled_on"], p["name"], p.get("location_id"), p.get("status")))
    _metrics(conn, c, "planned_session", rid, p.get("metrics"))
    for bi, block in enumerate(p.get("blocks", [])):
        conn.execute("INSERT INTO planned_blocks VALUES (?, ?, ?, ?, ?)",
                     (rid, block["id"], bi, block.get("name"), block.get("rounds")))
        _metrics(conn, c, "planned_session", rid, block.get("metrics"), block["id"])
        for ei, effort in enumerate(block.get("efforts", [])):
            conn.execute("INSERT INTO planned_efforts VALUES (?, ?, ?, ?, ?, ?)",
                         (rid, block["id"], effort["id"], ei, effort["activity_id"], effort.get("side")))
            _metrics(conn, c, "planned_session", rid, effort.get("metrics"), effort["id"])


def _session(conn, c, rid, p):
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, _link(p, "part_of").get("id"), _link(p, "answers").get("id"), p.get("name"), p.get("location_id"),
        *_when(p["time"]), p.get("status"), p["modality"]))
    _metrics(conn, c, "session", rid, p.get("metrics"))


def _effort(conn, c, rid, p):
    answers = _link(p, "answers")
    start, end, start_ms, end_ms, offset, day, zone = _when(p["time"])
    conn.execute("INSERT INTO efforts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, _link(p, "part_of").get("id"), p["activity_id"], answers.get("id"), answers.get("part"), p.get("round"),
        p.get("side"), p.get("location_id"), start, end, start_ms, end_ms, offset, day, zone, p.get("status"),
        p["modality"]))
    _metrics(conn, c, "effort", rid, p.get("metrics"))


def _observation(conn, c, rid, p):
    value = p.get("value")
    conn.execute("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, p["code"], value if not isinstance(value, str) else None, value if isinstance(value, str) else None,
        p.get("unit"), *_when(p["time"]), p["modality"], *_std(c, p["code"], p)[::3]))


REDUCERS: dict[str, Reducer] = {
    "activity": _activity, "location": _location, "setting": _setting, "plan": _plan,
    "planned_session": _planned_session, "session": _session, "effort": _effort, "observation": _observation,
}
