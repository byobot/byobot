"""Per-record projections: each kind's reducer rewrites that one record's rows from its current content."""

from __future__ import annotations

import collections
import hashlib
import json
import sqlite3
from typing import Any, Callable

from .. import library
from . import mappers, occurrences
from .contract import Contract, instant

DDL = """
CREATE TABLE IF NOT EXISTS activities (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, movement_type TEXT, fatigue INTEGER, tags TEXT, equipment TEXT,
  targets TEXT, default_metrics TEXT, resistance_curve TEXT, source TEXT NOT NULL);
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
  id TEXT PRIMARY KEY, code TEXT NOT NULL, value REAL, min REAL, max REAL, text TEXT, unit TEXT,
  started_at TEXT NOT NULL, ended_at TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER, offset_min INTEGER NOT NULL,
  local_date TEXT NOT NULL, zone TEXT, modality TEXT NOT NULL, std_value REAL, std_min REAL, std_max REAL,
  std_unit TEXT, source TEXT, source_record_id TEXT, overlay_id TEXT);
CREATE INDEX IF NOT EXISTS observations_code ON observations (code, start_ms);
CREATE INDEX IF NOT EXISTS observations_day ON observations (local_date);
CREATE INDEX IF NOT EXISTS observations_source ON observations (source_record_id) WHERE source_record_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS metrics (
  owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, part_id TEXT NOT NULL DEFAULT '', code TEXT NOT NULL,
  value REAL, min REAL, max REAL, text TEXT, unit TEXT, std_value REAL, std_min REAL, std_max REAL, std_unit TEXT,
  overlay_id TEXT, PRIMARY KEY (owner_kind, owner_id, part_id, code));
CREATE TABLE IF NOT EXISTS source_records (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
  revised_at TEXT NOT NULL, excluded INTEGER NOT NULL, raw TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_records_key ON source_records (source, source_type, source_id);
CREATE TABLE IF NOT EXISTS source_links (id TEXT NOT NULL, related_id TEXT NOT NULL, PRIMARY KEY (id, related_id));
CREATE INDEX IF NOT EXISTS source_links_related ON source_links (related_id);
CREATE TABLE IF NOT EXISTS source_sessions (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, name TEXT, started_at TEXT NOT NULL, ended_at TEXT,
  start_ms INTEGER NOT NULL, end_ms INTEGER, offset_min INTEGER NOT NULL, local_date TEXT NOT NULL, zone TEXT,
  overlay_id TEXT);
CREATE INDEX IF NOT EXISTS source_sessions_start ON source_sessions (start_ms);
CREATE TABLE IF NOT EXISTS overlays (
  id TEXT PRIMARY KEY, effect TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, with_kind TEXT,
  with_id TEXT, fields TEXT, arrangement TEXT);
CREATE INDEX IF NOT EXISTS overlays_target ON overlays (target_kind, target_id);
CREATE INDEX IF NOT EXISTS overlays_with ON overlays (with_kind, with_id) WHERE with_id IS NOT NULL;
"""

DDL += occurrences.DDL
VIEWS = {
    "strength_sets": """
CREATE VIEW IF NOT EXISTS strength_sets AS
SELECT e.id, e.activity_id, e.session_id, e.local_date, e.performed_at, e.round, e.side,
  coalesce(e.location_id, s.location_id) AS location_id,
  (SELECT value FROM metrics WHERE owner_kind = 'effort' AND owner_id = e.id AND part_id = '' AND code = 'reps') AS reps,
  (SELECT std_value FROM metrics WHERE owner_kind = 'effort' AND owner_id = e.id AND part_id = '' AND code = 'load')
    AS load_kg,
  (SELECT value FROM metrics WHERE owner_kind = 'effort' AND owner_id = e.id AND part_id = '' AND code = 'rir') AS rir,
  (SELECT value FROM metrics WHERE owner_kind = 'effort' AND owner_id = e.id AND part_id = '' AND code = 'rpe') AS rpe
FROM efforts e LEFT JOIN sessions s ON s.id = e.session_id;""",
    "plan_vs_actual": """
CREATE VIEW IF NOT EXISTS plan_vs_actual AS
SELECT e.id AS effort_id, e.planned_session_id, e.planned_effort_id, e.activity_id, e.round, e.local_date,
  t.value AS target_reps, t.min AS target_reps_min, t.max AS target_reps_max,
  l.std_value AS target_load_kg, l.std_min AS target_load_kg_min, l.std_max AS target_load_kg_max,
  a.reps AS actual_reps, a.load_kg AS actual_load_kg, a.rir AS actual_rir
FROM efforts e JOIN strength_sets a ON a.id = e.id
LEFT JOIN metrics t ON t.owner_kind = 'planned_session' AND t.owner_id = e.planned_session_id
  AND t.part_id = e.planned_effort_id AND t.code = 'reps'
LEFT JOIN metrics l ON l.owner_kind = 'planned_session' AND l.owner_id = e.planned_session_id
  AND l.part_id = e.planned_effort_id AND l.code = 'load'
WHERE e.planned_effort_id IS NOT NULL;""",
}
DDL += "".join(VIEWS.values()) + "\n"
RULES = 3  # raise when records are derived differently, so stores rebuild on open
SHAPE = hashlib.sha256(f"{DDL}{RULES}".encode()).hexdigest()[:16]
Reducer = Callable[[sqlite3.Connection, Contract, str, dict], None]
TABLES = {
    "activity": ("activities",), "location": ("locations",), "setting": ("settings",), "plan": ("plans",),
    "planned_session": ("planned_sessions", "planned_blocks", "planned_efforts"), "session": ("sessions",),
    "effort": ("efforts",), "observation": ("observations",),
    "source_record": ("source_records", "source_links", "source_sessions", "observations"), "overlay": ("overlays",),
}
OWNER_COLUMN = {("planned_session", "planned_blocks"): "planned_session_id",
                ("planned_session", "planned_efforts"): "planned_session_id",
                ("source_record", "observations"): "source_record_id"}


# (kind, version) -> the step that lifts a payload of that version to the next, so reducers and edits only ever
# see the version this install writes. A version whose old payloads are still valid needs none.
UPCASTS: dict[tuple[str, int], Callable[[dict], dict]] = {}


def upcast(c: Contract, kind: str, version: int | None, payload: dict) -> dict:
    """`payload`, written at kind version `version`, as this install's version of the kind holds it."""
    for v in range(version or 1, c.version(kind) or 1):
        if (kind, v) in UPCASTS:
            payload = UPCASTS[(kind, v)](payload)
    return payload


def lowest(c: Contract, kind: str, payload: dict) -> int:
    """The lowest version of `kind` that holds `payload`: after its last reshape, and at least the version that
    added each field it uses. Writers write this, so installs that know only older versions still read it."""
    spec = c.registry["kinds"].get(kind, {})
    since = spec.get("since", {})
    floor = max((v + 1 for k, v in UPCASTS if k == kind), default=spec.get("reshaped", 1))
    return min(max([floor, *(since.get(f, 1) for f in payload)]), c.version(kind) or 1)


def project(conn: sqlite3.Connection, c: Contract, kind: str, rid: str, payload: dict | None) -> None:
    """Replace one record's rows: from `payload`, or none when it isn't live."""
    for table in TABLES.get(kind, ()):
        conn.execute(f"DELETE FROM {table} WHERE {OWNER_COLUMN.get((kind, table), 'id')} = ?", (rid,))
    conn.execute("DELETE FROM metrics WHERE owner_kind = ? AND owner_id = ?", (kind, rid))
    if payload is not None and kind in REDUCERS:
        REDUCERS[kind](conn, c, rid, payload)
    elif kind == "activity" and rid in _library():
        _activity_row(conn, rid, _library()[rid], "library")


def clear(conn: sqlite3.Connection) -> None:
    for table in {t for ts in TABLES.values() for t in ts} | {"metrics", *occurrences.TABLES}:
        conn.execute(f"DELETE FROM {table}")
    seed_library(conn)


def _library() -> dict[str, dict]:
    return {a["id"]: {k: v for k, v in a.items() if k != "id"} for a in library.activities()}


def seed_library(conn: sqlite3.Connection) -> None:
    """Library activities ship with byobot; one you put replaces it, and retracting yours brings it back."""
    conn.execute("DELETE FROM activities WHERE source = 'library'")
    mine = {r[0] for r in conn.execute("SELECT id FROM activities")}
    for rid, p in _library().items():
        if rid not in mine:
            _activity_row(conn, rid, p, "library")


def drop(conn: sqlite3.Connection) -> None:
    """Remove the derived tables, for a store written by a build whose tables had another shape."""
    for view in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {view}")
    for table in {t for ts in TABLES.values() for t in ts} | {"metrics", *occurrences.TABLES}:
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


def _metrics(conn: sqlite3.Connection, c: Contract, kind: str, rid: str, metrics: dict | None, part: str = "",
             corrected: dict | None = None) -> None:
    """`corrected`: code -> the overlay whose value it is."""
    for code, m in (metrics or {}).items():
        value = m.get("value")
        conn.execute("INSERT INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            kind, rid, part, code, value if not isinstance(value, str) else None, m.get("min"), m.get("max"),
            value if isinstance(value, str) else None, m.get("unit"), *_std(c, code, m), (corrected or {}).get(code)))


def _activity_row(conn, rid, p, source):
    conn.execute("INSERT INTO activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, p["name"], p.get("movement_type"), p.get("fatigue"), _json(p.get("tags")), _json(p.get("equipment")),
        _json(p.get("targets")), _json(p.get("default_metrics")), _json(p.get("resistance_curve")), source))


def _activity(conn, c, rid, p):
    _activity_row(conn, rid, p, "store")


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


def _observation(conn, c, rid, p, source=None, source_record_id=None, overlay_id=None):
    value = p.get("value")
    conn.execute(f"INSERT INTO observations VALUES ({', '.join('?' * 22)})", (
        rid, p["code"], value if not isinstance(value, str) else None, p.get("min"), p.get("max"),
        value if isinstance(value, str) else None, p.get("unit"), *_when(p["time"]), p["modality"],
        *_std(c, p["code"], p), source, source_record_id, overlay_id))


def _source_record(conn, c, rid, p):
    """What the current revision reads as, with your corrections over it."""
    overlays = {e: (i, json.loads(f) if f else {}) for i, e, f in conn.execute(
        "SELECT id, effect, fields FROM overlays WHERE target_kind = 'source_record' AND target_id = ?", (rid,))}
    excluded = "exclude" in overlays
    conn.execute("INSERT INTO source_records VALUES (?, ?, ?, ?, ?, ?, ?)", (
        rid, p["source"], p["source_type"], p["source_id"], mappers.revised(p), int(excluded), _json(p["raw"])))

    def related(source_type, source_id):
        row = conn.execute("SELECT raw FROM source_records WHERE source = ? AND source_type = ? AND source_id = ?",
                           (p["source"], source_type, source_id)).fetchone()
        return json.loads(row[0]) if row else None

    read = mappers.read(p, related)
    for source_type, source_id in read["related"]:
        conn.execute("INSERT OR IGNORE INTO source_links VALUES (?, ?)",
                     (rid, c.derived("source_record", [p["source"], source_type, source_id])))
    if excluded:
        return
    oid, fix = overlays.get("correct", (None, {}))
    fixed = {code: oid for code in fix.get("metrics", {})}
    observations, ids = [o for o in read["observations"] if _storable(o)], collections.Counter()
    for o in observations:
        o = {**o, "modality": "sensed"}
        mine = oid if o["code"] in fixed or ("value" in fix and len(observations) == 1) else None
        if o["code"] in fixed:
            o = {**o, **{k: v for k, v in fix["metrics"][o["code"]].items() if k in ("value", "unit")}}
        elif mine:
            o = {**o, **{k: fix[k] for k in ("value", "unit") if k in fix}}
        ids[o["code"]] += 1
        n = ids[o["code"]]
        _observation(conn, c, f"{rid}:{o['code']}" + (f":{n}" if n > 1 else ""), o, p["source"], rid, mine)
    session = read["session"]
    if session:
        time = {**session["time"], **fix.get("time", {})}
        if "end" in time and instant(time["end"]) < instant(time["start"]):
            time.pop("end")
        conn.execute("INSERT INTO source_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            rid, p["source"], fix.get("name", session.get("name")), *_when(time),
            oid if {"time", "name"} & set(fix) else None))
        given = {k: m for k, m in (session.get("metrics") or {}).items() if _storable(m)}
        _metrics(conn, c, "source_record", rid, {**given, **fix.get("metrics", {})}, corrected=fixed)


def _storable(m: dict) -> bool:
    """Whether a source's value can be stored: text, or a number within 64 bits; other values are left unread."""
    values = [m.get(k) for k in ("value", "min", "max")]
    return isinstance(m.get("unit"), (str, type(None))) and all(
        v is None or isinstance(v, (str, float)) or (isinstance(v, int) and -2 ** 63 <= v < 2 ** 63) for v in values)


def _overlay(conn, c, rid, p):
    target, other = p["target"], p.get("with") or {}
    arrangement = {k: p[k] for k in ("order", "swaps") if k in p}
    conn.execute("INSERT INTO overlays VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        rid, p["effect"], target["kind"], target["id"], other.get("kind"), other.get("id"),
        _json(p.get("fields")), _json(arrangement or None)))


REDUCERS: dict[str, Reducer] = {
    "activity": _activity, "location": _location, "setting": _setting, "plan": _plan,
    "planned_session": _planned_session, "session": _session, "effort": _effort, "observation": _observation,
    "source_record": _source_record, "overlay": _overlay,
}
