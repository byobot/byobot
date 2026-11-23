"""The local store: one SQLite file holding everything byobot knows.

Two kinds of data, kept apart on purpose:

* **Ledger.** Things someone authored (an activity, a plan, a logged effort).
  Every change is an immutable event in ``events``; the canonical tables are
  a projection of that log and can be rebuilt from it at any time. Undo is
  an event too, so nothing is ever lost.
* **Telemetry.** Measurements from devices and services. Each source record
  is kept as-is in ``raw_records`` and upserted into ``telemetry_samples``
  keyed on (source, source_id), so re-importing the same data is a no-op.

Every canonical row carries its provenance: the event (and so the actor and
device) behind a ledger row, or the raw record behind a telemetry row.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from byobot import contract, library
from byobot.util import Conflict, NotFound, ValidationFailed, canonical_json, db_path, now, uuid7

STORE_VERSION = 4
TELEMETRY_FORMAT = "byobot.telemetry.v0"

BASE_DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- The ledger's source of truth. Append-only: rows are never updated or deleted.
CREATE TABLE IF NOT EXISTS events (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,
  id             TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL,
  type           TEXT NOT NULL,
  ts             TEXT NOT NULL,
  device_id      TEXT NOT NULL,
  actor_kind     TEXT NOT NULL,
  actor_id       TEXT NOT NULL,
  actor_model    TEXT,
  payload        TEXT NOT NULL,  -- JSON
  envelope       TEXT NOT NULL,  -- the whole event, exactly as accepted
  recorded_at    TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Telemetry's source of truth: each record exactly as a source delivered it.
CREATE TABLE IF NOT EXISTS raw_records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  source      TEXT NOT NULL,
  source_id   TEXT NOT NULL,
  format      TEXT NOT NULL,
  payload     TEXT NOT NULL,  -- JSON
  ingested_at TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE (source, source_id, format)
);
"""

# Devices paired with `byobot serve`. Not health data and not derived: kept as is across rebuilds.
SYNC_DDL = """
-- Phones and other devices paired with `byobot serve`. Tokens are kept only as SHA-256 hashes.
CREATE TABLE IF NOT EXISTS sync_peers (
  device_id    TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE,
  paired_at    TEXT NOT NULL,
  last_seen_at TEXT,
  revoked_at   TEXT
);
"""

# Everything below is derived and can be dropped and rebuilt from the two logs above.
PROVENANCE = """
  event_id     TEXT NOT NULL,
  actor_kind   TEXT NOT NULL,
  actor_id     TEXT NOT NULL,
  actor_model  TEXT,
  device_id    TEXT NOT NULL,
  authored_at  TEXT NOT NULL"""
# Library activities have no event, so their provenance columns are empty.
ACTIVITY_PROVENANCE = PROVENANCE.replace(" NOT NULL", "")

# Units byobot converts to a standard unit, so values compare across sources: metric -> unit -> (standard unit, factor).
# A metric or unit not listed here keeps its value and unit as entered in the std_* columns.
STANDARD_UNITS: dict[str, dict[str, tuple[str, float]]] = {
    "load": {"kg": ("kg", 1.0), "lb": ("kg", 0.45359237)},
    "duration": {"s": ("s", 1.0), "min": ("s", 60.0), "h": ("s", 3600.0)},
    "rest": {"s": ("s", 1.0), "min": ("s", 60.0)},
    "distance": {"m": ("m", 1.0), "km": ("m", 1000.0), "mi": ("m", 1609.344), "yd": ("m", 0.9144), "ft": ("m", 0.3048)},
    "pace": {"s/km": ("s/km", 1.0), "s/mi": ("s/km", 1 / 1.609344), "s/100m": ("s/km", 10.0), "s/500m": ("s/km", 2.0)},
}

# Columns that plan_vs_actual and strength_sets pull out of the metrics table: (column, metric, standard unit or None).
VIEW_METRICS = [
    ("reps", "reps", None),
    ("load_kg", "load", "kg"),
    ("duration_s", "duration", "s"),
    ("distance_m", "distance", "m"),
    ("pace_s_per_km", "pace", "s/km"),
    ("rpe", "rpe", None),
]


def _metric_join(alias: str, owner_type: str, owner: str, metric: str, unit: str | None) -> str:
    unit_clause = f" AND {alias}.std_unit = '{unit}'" if unit else ""
    return (f"LEFT JOIN metrics {alias} ON {alias}.owner_type = '{owner_type}' AND {alias}.owner_id = {owner}"
            f" AND {alias}.metric = '{metric}'{unit_clause}")


def _plan_vs_actual_sql() -> str:
    cols, joins = [], []
    for i, (col, metric, unit) in enumerate(VIEW_METRICS):
        t, a = f"t{i}", f"a{i}"
        cols.append(f"  coalesce({t}.std_value, {t}.std_min) AS target_{col}_min,\n"
                    f"  coalesce({t}.std_value, {t}.std_max) AS target_{col}_max,\n"
                    f"  {a}.std_value AS actual_{col}")
        joins.append(_metric_join(t, "planned_effort", "pe.id", metric, unit))
        joins.append(_metric_join(a, "performed_effort", "pf.id", metric, unit))
    return (
        "-- Plan versus actual: every planned effort, with each logged effort that answers it.\n"
        "-- Targets show as a min/max pair (equal for an exact target); values are in kg, s, m and s/km.\n"
        "-- swapped is 1 when a different activity answered the planned one.\n"
        "-- location_id is where the set happened (its own, else its session's); planned_location_id where it was planned for.\n"
        "CREATE VIEW plan_vs_actual AS\nSELECT\n"
        "  s.plan_id, s.id AS planned_session_id, s.scheduled_on, s.location_id AS planned_location_id,\n"
        "  b.id AS block_id, b.position AS block_position, b.rounds,\n"
        "  pe.id AS planned_effort_id, pe.position AS effort_position, pe.activity_id, pe.side,\n"
        "  pf.id AS performed_effort_id, pf.round, pf.performed_at, pf.activity_id AS actual_activity_id,\n"
        "  pf.activity_id <> pe.activity_id AS swapped, coalesce(pf.location_id, ps.location_id) AS location_id,\n"
        + ",\n".join(cols)
        + "\nFROM planned_efforts pe\n"
        "JOIN planned_blocks b ON b.id = pe.block_id\n"
        "JOIN planned_sessions s ON s.id = pe.planned_session_id\n"
        "LEFT JOIN performed_efforts pf ON pf.planned_effort_id = pe.id\n"
        "LEFT JOIN performed_sessions ps ON ps.id = pf.session_id\n"
        + "\n".join(joins) + ";\n"
    )


def _strength_sets_sql() -> str:
    return (
        "-- Logged efforts with reps or load, one row per set, with plain columns. Loads compare within a location_id.\n"
        "CREATE VIEW strength_sets AS\nSELECT\n"
        "  e.id AS effort_id, e.session_id, e.performed_at, e.activity_id, e.side, e.planned_effort_id, e.round,\n"
        "  coalesce(e.location_id, s.location_id) AS location_id,\n"
        "  r.value AS reps, l.value AS load_value, l.unit AS load_unit,\n"
        "  CASE WHEN l.std_unit = 'kg' THEN l.std_value END AS load_kg,\n"
        "  p.value AS rpe, q.value AS rir\n"
        "FROM performed_efforts e\n"
        "LEFT JOIN performed_sessions s ON s.id = e.session_id\n"
        + _metric_join("r", "performed_effort", "e.id", "reps", None) + "\n"
        + _metric_join("l", "performed_effort", "e.id", "load", None) + "\n"
        + _metric_join("p", "performed_effort", "e.id", "rpe", None) + "\n"
        + _metric_join("q", "performed_effort", "e.id", "rir", None) + "\n"
        "WHERE r.owner_id IS NOT NULL OR l.owner_id IS NOT NULL;\n"
    )


LEDGER_DDL = f"""
CREATE TABLE undone_events (
  event_id      TEXT PRIMARY KEY,
  undo_event_id TEXT NOT NULL
);
CREATE TABLE activities (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  tags             TEXT,  -- JSON array
  default_metrics  TEXT,  -- JSON array
  targets          TEXT,  -- JSON object: main and synergists arrays
  equipment        TEXT,  -- JSON array
  movement_type    TEXT,
  fatigue          INTEGER,
  resistance_curve TEXT,  -- JSON object: stretch, middle, contract
  notes            TEXT,
  source           TEXT NOT NULL,  -- library (shipped with byobot) or ledger (defined by you or your agent)
  payload          TEXT NOT NULL,  -- JSON: the activity as defined{ACTIVITY_PROVENANCE}
);
CREATE TABLE locations (
  id    TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  notes TEXT,{PROVENANCE}
);
CREATE TABLE plans (
  id        TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  goal      TEXT,
  starts_on TEXT NOT NULL,
  ends_on   TEXT,
  notes     TEXT,{PROVENANCE}
);
CREATE TABLE planned_sessions (
  id           TEXT PRIMARY KEY,
  plan_id      TEXT,
  scheduled_on TEXT NOT NULL,
  name         TEXT NOT NULL,
  location_id  TEXT,
  notes        TEXT,{PROVENANCE}
);
CREATE TABLE planned_blocks (
  id                 TEXT PRIMARY KEY,
  planned_session_id TEXT NOT NULL,
  position           INTEGER NOT NULL,
  name               TEXT,
  rounds             INTEGER NOT NULL,
  notes              TEXT,{PROVENANCE}
);
CREATE TABLE planned_efforts (
  id                 TEXT PRIMARY KEY,
  planned_session_id TEXT NOT NULL,
  block_id           TEXT NOT NULL,
  position           INTEGER NOT NULL,
  activity_id        TEXT NOT NULL,
  side               TEXT,
  notes              TEXT,{PROVENANCE}
);
CREATE TABLE performed_sessions (
  id                 TEXT PRIMARY KEY,
  planned_session_id TEXT,
  plan_id            TEXT,
  name               TEXT,
  started_at         TEXT NOT NULL,
  ended_at           TEXT,
  location_id        TEXT,
  notes              TEXT,{PROVENANCE}
);
CREATE TABLE performed_efforts (
  id                TEXT PRIMARY KEY,
  session_id        TEXT,
  activity_id       TEXT NOT NULL,
  performed_at      TEXT NOT NULL,
  planned_effort_id TEXT,
  round             INTEGER,
  side              TEXT,
  location_id       TEXT,  -- as written; NULL takes the session's
  notes             TEXT,{PROVENANCE}
);
-- One row per metric on anything that carries metrics.
CREATE TABLE metrics (
  owner_type TEXT NOT NULL,  -- planned_session, planned_block, planned_effort, performed_session, performed_effort
  owner_id   TEXT NOT NULL,
  metric     TEXT NOT NULL,  -- reps, load, duration, ... or your own x.name
  value      NUMERIC,        -- an exact number (true/false as 1/0)
  value_text TEXT,           -- an exact text value, such as a climbing grade
  min        NUMERIC,
  max        NUMERIC,
  unit       TEXT,           -- as entered
  std_unit   TEXT,           -- kg, s, m or s/km where byobot converts; otherwise as entered
  std_value  NUMERIC,
  std_min    NUMERIC,
  std_max    NUMERIC,
  measure    TEXT NOT NULL,  -- the measure exactly as written, JSON
  event_id   TEXT NOT NULL,
  PRIMARY KEY (owner_type, owner_id, metric)
);
CREATE INDEX metrics_by_metric ON metrics (metric, owner_type);
CREATE INDEX planned_efforts_by_session ON planned_efforts (planned_session_id, position);
CREATE INDEX performed_efforts_by_time ON performed_efforts (performed_at);
CREATE INDEX performed_efforts_by_plan ON performed_efforts (planned_effort_id);
CREATE INDEX performed_efforts_by_session ON performed_efforts (session_id);

{_plan_vs_actual_sql()}
{_strength_sets_sql()}
"""

TELEMETRY_DDL = """
CREATE TABLE telemetry_samples (
  source              TEXT NOT NULL,
  source_id           TEXT NOT NULL,
  type                TEXT NOT NULL,
  start_at            TEXT NOT NULL,
  end_at              TEXT NOT NULL,
  value               REAL,  -- quantity samples
  category            TEXT,  -- category samples, e.g. sleep stage
  unit                TEXT,
  device_id           TEXT,
  device_name         TEXT,
  device_manufacturer TEXT,
  device_model        TEXT,
  raw_record_id       INTEGER NOT NULL,
  PRIMARY KEY (source, source_id)
);
CREATE INDEX telemetry_by_type_time ON telemetry_samples (type, start_at);
"""

LEDGER_OBJECTS = [
    ("view", "plan_vs_actual"),
    ("view", "strength_sets"),
    ("table", "undone_events"),
    ("table", "activities"),
    ("table", "locations"),
    ("table", "plans"),
    ("table", "planned_sessions"),
    ("table", "planned_blocks"),
    ("table", "planned_efforts"),
    ("table", "performed_sessions"),
    ("table", "performed_efforts"),
    ("table", "metrics"),
]
TELEMETRY_OBJECTS = [("table", "telemetry_samples")]


def full_ddl() -> str:
    """The store's SQL schema, for people and agents writing queries."""
    return "\n".join(part.strip() for part in (BASE_DDL, LEDGER_DDL, TELEMETRY_DDL, SYNC_DDL)) + "\n"


def _metric_row(metric: str, measure: dict) -> dict:
    """Flatten one measure into metrics columns, with a standard-unit copy where byobot knows the conversion."""
    value = measure.get("value")
    unit = measure.get("unit")
    std_unit, factor = STANDARD_UNITS.get(metric, {}).get(unit, (unit, 1.0)) if unit else (None, 1.0)
    number = float(value) if isinstance(value, (int, float)) else None  # bool is an int: true/false -> 1/0
    scale = lambda x: None if x is None else (x * factor if factor != 1.0 else x)
    return {
        "metric": metric,
        "value": number,
        "value_text": value if isinstance(value, str) else None,
        "min": measure.get("min"),
        "max": measure.get("max"),
        "unit": unit,
        "std_unit": std_unit,
        "std_value": scale(number if not isinstance(value, bool) else None),
        "std_min": scale(measure.get("min")),
        "std_max": scale(measure.get("max")),
        "measure": canonical_json(measure),
    }


def _map_telemetry_v0(sample: dict) -> dict:
    device = sample.get("device") or {}
    quantity = isinstance(sample["value"], (int, float)) and not isinstance(sample["value"], bool)
    return {
        "source": sample["source"],
        "source_id": sample["source_id"],
        "type": sample["type"],
        "start_at": sample["start"],
        "end_at": sample["end"],
        "value": sample["value"] if quantity else None,
        "category": None if quantity else sample["value"],
        "unit": sample.get("unit"),
        "device_id": device.get("id"),
        "device_name": device.get("name"),
        "device_manufacturer": device.get("manufacturer"),
        "device_model": device.get("model"),
    }


# Raw record format -> function that maps one raw payload to a canonical sample row.
# Importers for WHOOP, Hume, Apple Health and others register their own formats here.
TELEMETRY_MAPPERS: dict[str, Callable[[dict], dict]] = {TELEMETRY_FORMAT: _map_telemetry_v0}


def _activity_row(p: dict, source: str) -> dict:
    def as_json(key: str) -> str | None:
        return json.dumps(p[key]) if key in p else None

    return {
        "id": p["id"], "name": p["name"], "tags": as_json("tags"), "default_metrics": as_json("default_metrics"),
        "targets": as_json("targets"), "equipment": as_json("equipment"), "movement_type": p.get("movement_type"),
        "fatigue": p.get("fatigue"), "resistance_curve": as_json("resistance_curve"), "notes": p.get("notes"),
        "source": source, "payload": canonical_json(p),
    }


def _order_key_text(event: dict) -> str:
    # A space sorts below every character ids and timestamps may hold, so text order matches tuple order.
    return " ".join(contract.event_order_key(event))


class Store:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.created = not self.path.exists()
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > STORE_VERSION:
            raise RuntimeError(f"{self.path} was written by a newer byobot (store version {version})")
        with self._tx():
            self._create(BASE_DDL)
            self._create(SYNC_DDL)
            if version == 0:
                self._create(LEDGER_DDL)
                self._create(TELEMETRY_DDL)
                self._load_library()
                self._set_meta("device_id", uuid7())
                self._set_meta("created_at", now())
                self.conn.execute(f"PRAGMA user_version={STORE_VERSION}")
            elif version < STORE_VERSION:
                self._rebuild_ledger()
                self.conn.execute(f"PRAGMA user_version={STORE_VERSION}")
            if self.meta("ledger_head") is None and self.conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
                # Stores written before events were ordered by event time: replay once in the new order.
                self._rebuild_ledger()
            if self.meta("library") != library.fingerprint():
                self._rebuild_ledger()

    def _tx(self):
        conn = self.conn

        class _Tx:
            def __enter__(self_inner):
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                    self_inner.owner = True
                else:
                    self_inner.owner = False

            def __exit__(self_inner, exc_type, *_):
                if not self_inner.owner:
                    return
                conn.execute("ROLLBACK" if exc_type else "COMMIT")

        return _Tx()

    def _create(self, ddl: str) -> None:
        # Run statements one at a time inside the open transaction (executescript would commit it).
        pending = ""
        for line in ddl.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                self.conn.execute(pending)
                pending = ""
        assert not pending.strip(), f"incomplete SQL: {pending!r}"

    def _drop(self, objects: Iterable[tuple[str, str]]) -> None:
        for kind, name in objects:
            self.conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @property
    def device_id(self) -> str:
        value = self.meta("device_id")
        assert value, "store has no device id"
        return value

    # -- ledger ----------------------------------------------------------

    def append(self, event_type: str, payload: dict, actor: dict) -> dict:
        """Author a new event on this device and apply it. Returns the accepted event."""
        envelope = {
            "id": uuid7(),
            "schema_version": contract.SCHEMA_VERSION,
            "type": event_type,
            "ts": now(),
            "device_id": self.device_id,
            "actor": actor,
            "payload": payload,
        }
        return self.accept(envelope)

    def accept(self, envelope: dict) -> dict:
        """Accept a complete event (authored here or on another device)."""
        errors = contract.validate("event", envelope)
        if errors:
            raise ValidationFailed("event does not match the byobot v0 contract", errors)
        with self._tx():
            if self.conn.execute("SELECT 1 FROM events WHERE id = ?", (envelope["id"],)).fetchone():
                raise Conflict(f"event {envelope['id']} already exists")
            if envelope["type"] == "event.undone":
                self._check_undo(envelope["payload"]["event_id"])
            actor = envelope["actor"]
            self.conn.execute(
                "INSERT INTO events (id, schema_version, type, ts, device_id, actor_kind, actor_id, actor_model,"
                " payload, envelope, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope["id"],
                    envelope["schema_version"],
                    envelope["type"],
                    envelope["ts"],
                    envelope["device_id"],
                    actor["kind"],
                    actor["id"],
                    actor.get("model"),
                    canonical_json(envelope["payload"]),
                    canonical_json(envelope),
                    now(),
                ),
            )
            key = _order_key_text(envelope)
            if envelope["type"] == "event.undone" or key < (self.meta("ledger_head") or ""):
                # An undo, or an event older than one already applied (a phone syncing a workout it logged
                # offline), can change what later events built on, so replay the log in event-time order.
                self._rebuild_ledger()
            else:
                self._apply(envelope)
                self._set_meta("ledger_head", key)
        return envelope

    def _check_undo(self, target: str) -> None:
        if not self.conn.execute("SELECT 1 FROM events WHERE id = ?", (target,)).fetchone():
            raise NotFound(f"no event with id {target}")
        if self.conn.execute("SELECT 1 FROM undone_events WHERE event_id = ?", (target,)).fetchone():
            raise Conflict(f"event {target} is already undone")

    def _events(self) -> list[dict]:
        """Every event, in the order they apply: event time, then device id, then event id (see the contract)."""
        rows = self.conn.execute("SELECT envelope FROM events").fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=contract.event_order_key)

    @staticmethod
    def _effective_undos(events: list[dict]) -> dict[str, str]:
        """Map each undone event id to the undo that undid it. An undo that is itself undone does nothing."""
        undo_of = {e["id"]: e["payload"]["event_id"] for e in events if e["type"] == "event.undone"}
        targeted_by: dict[str, list[str]] = {}
        for undo_id, target in undo_of.items():
            targeted_by.setdefault(target, []).append(undo_id)
        memo: dict[str, bool] = {}

        def undone(event_id: str) -> bool:
            if event_id not in memo:
                memo[event_id] = False  # undo chains point backwards in time, so this never loops
                memo[event_id] = any(not undone(u) for u in targeted_by.get(event_id, []))
            return memo[event_id]

        out = {}
        for target, undos in targeted_by.items():
            if undone(target):
                out[target] = next(u for u in undos if not undone(u))
        return out

    def _rebuild_ledger(self) -> dict:
        self._drop(LEDGER_OBJECTS)
        self._create(LEDGER_DDL)
        self._load_library()
        events = self._events()
        undone = self._effective_undos(events)
        self.conn.executemany("INSERT INTO undone_events (event_id, undo_event_id) VALUES (?, ?)", undone.items())
        applied = 0
        for event in events:
            if event["type"] != "event.undone" and event["id"] not in undone:
                self._apply(event)
                applied += 1
        self._set_meta("ledger_head", _order_key_text(events[-1]) if events else "")
        return {"events": len(events), "applied": applied, "undone": len(undone)}

    def _load_library(self) -> None:
        """Library activities go in first, so an activity you define with the same id replaces one."""
        for activity in library.activities():
            self._put("activities", _activity_row(activity, "library"))
        self._set_meta("library", library.fingerprint())

    def _apply(self, event: dict) -> None:
        actor = event["actor"]
        prov = {
            "event_id": event["id"],
            "actor_kind": actor["kind"],
            "actor_id": actor["id"],
            "actor_model": actor.get("model"),
            "device_id": event["device_id"],
            "authored_at": event["ts"],
        }
        p = event["payload"]
        kind = event["type"]
        if kind == "activity.defined":
            self._put("activities", {**_activity_row(p, "ledger"), **prov})
        elif kind == "location.defined":
            self._put("locations", {"id": p["id"], "name": p["name"], "notes": p.get("notes"), **prov})
        elif kind == "plan.defined":
            self._put("plans", {
                "id": p["id"], "name": p["name"], "goal": p.get("goal"), "starts_on": p["starts_on"],
                "ends_on": p.get("ends_on"), "notes": p.get("notes"), **prov,
            })
        elif kind == "session.planned":
            # Planning a session again with the same id replaces its whole prescription.
            for owner_type, table in (("planned_effort", "planned_efforts"), ("planned_block", "planned_blocks")):
                self.conn.execute(
                    f"DELETE FROM metrics WHERE owner_type = ? AND owner_id IN"
                    f" (SELECT id FROM {table} WHERE planned_session_id = ?)", (owner_type, p["id"]))
                self.conn.execute(f"DELETE FROM {table} WHERE planned_session_id = ?", (p["id"],))
            self._put("planned_sessions", {
                "id": p["id"], "plan_id": p.get("plan_id"), "scheduled_on": p["scheduled_on"], "name": p["name"],
                "location_id": p.get("location_id"), "notes": p.get("notes"), **prov,
            })
            self._put_metrics("planned_session", p["id"], p.get("metrics"), event["id"])
            for bi, block in enumerate(p["blocks"], start=1):
                self._put("planned_blocks", {
                    "id": block["id"], "planned_session_id": p["id"], "position": bi, "name": block.get("name"),
                    "rounds": block.get("rounds", 1), "notes": block.get("notes"), **prov,
                })
                self._put_metrics("planned_block", block["id"], block.get("metrics"), event["id"])
                for ei, effort in enumerate(block["efforts"], start=1):
                    self._put("planned_efforts", {
                        "id": effort["id"], "planned_session_id": p["id"], "block_id": block["id"], "position": ei,
                        "activity_id": effort["activity_id"], "side": effort.get("side"), "notes": effort.get("notes"),
                        **prov,
                    })
                    self._put_metrics("planned_effort", effort["id"], effort.get("metrics"), event["id"])
        elif kind == "session.performed":
            # Logging the session again replaces it and the efforts it carried (not efforts logged on their own).
            previous = self.conn.execute("SELECT event_id FROM performed_sessions WHERE id = ?", (p["id"],)).fetchone()
            if previous:
                self.conn.execute(
                    "DELETE FROM metrics WHERE owner_type = 'performed_effort' AND owner_id IN"
                    " (SELECT id FROM performed_efforts WHERE session_id = ? AND event_id = ?)", (p["id"], previous[0]))
                self.conn.execute("DELETE FROM performed_efforts WHERE session_id = ? AND event_id = ?",
                                  (p["id"], previous[0]))
            self._put("performed_sessions", {
                "id": p["id"], "planned_session_id": p.get("planned_session_id"), "plan_id": p.get("plan_id"),
                "name": p.get("name"), "started_at": p["started_at"], "ended_at": p.get("ended_at"),
                "location_id": p.get("location_id"), "notes": p.get("notes"), **prov,
            })
            self._put_metrics("performed_session", p["id"], p.get("metrics"), event["id"])
            for effort in p.get("efforts", []):
                self._put_effort({**effort, "session_id": p["id"],
                                  "performed_at": effort.get("performed_at", p["started_at"])}, prov)
        elif kind == "effort.performed":
            self._put_effort(p, prov)
        else:  # pragma: no cover - the contract rejects other types before we get here
            raise ValueError(kind)

    def _put_effort(self, e: dict, prov: dict) -> None:
        self._put("performed_efforts", {
            "id": e["id"], "session_id": e.get("session_id"), "activity_id": e["activity_id"],
            "performed_at": e["performed_at"], "planned_effort_id": e.get("planned_effort_id"),
            "round": e.get("round"), "side": e.get("side"), "location_id": e.get("location_id"),
            "notes": e.get("notes"), **prov,
        })
        self._put_metrics("performed_effort", e["id"], e.get("metrics"), prov["event_id"])

    def _put_metrics(self, owner_type: str, owner_id: str, metrics: dict | None, event_id: str) -> None:
        self.conn.execute("DELETE FROM metrics WHERE owner_type = ? AND owner_id = ?", (owner_type, owner_id))
        for name, measure in (metrics or {}).items():
            self._put("metrics", {"owner_type": owner_type, "owner_id": owner_id, **_metric_row(name, measure),
                                  "event_id": event_id})

    def metrics_of(self, owner_type: str, owner_id: str) -> dict:
        """The metrics map for one record, exactly as it was written."""
        rows = self.conn.execute(
            "SELECT metric, measure FROM metrics WHERE owner_type = ? AND owner_id = ? ORDER BY metric",
            (owner_type, owner_id)).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def _put(self, table: str, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

    # -- telemetry -------------------------------------------------------

    def ingest(self, samples: list[dict], fmt: str = TELEMETRY_FORMAT) -> dict:
        """Upsert telemetry samples. All or nothing: one invalid sample rejects the batch."""
        if fmt not in TELEMETRY_MAPPERS:
            raise ValidationFailed(f"unknown telemetry format {fmt!r}")
        problems = []
        for i, sample in enumerate(samples):
            for err in contract.validate("telemetry", sample):
                problems.append({"index": i, **err})
        if problems:
            raise ValidationFailed(f"{len({p['index'] for p in problems})} of {len(samples)} samples are invalid", problems)
        counts = {"inserted": 0, "updated": 0, "unchanged": 0}
        with self._tx():
            for sample in samples:
                counts[self._ingest_one(sample, fmt)] += 1
        return counts

    def _ingest_one(self, sample: dict, fmt: str) -> str:
        text = canonical_json(sample)
        stamp = now()
        row = self.conn.execute(
            "SELECT id, payload FROM raw_records WHERE source = ? AND source_id = ? AND format = ?",
            (sample["source"], sample["source_id"], fmt),
        ).fetchone()
        if row and row["payload"] == text:
            return "unchanged"
        if row:
            raw_id = row["id"]
            self.conn.execute("UPDATE raw_records SET payload = ?, updated_at = ? WHERE id = ?", (text, stamp, raw_id))
            outcome = "updated"
        else:
            cur = self.conn.execute(
                "INSERT INTO raw_records (source, source_id, format, payload, ingested_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (sample["source"], sample["source_id"], fmt, text, stamp, stamp),
            )
            raw_id = cur.lastrowid
            outcome = "inserted"
        self._put("telemetry_samples", {**TELEMETRY_MAPPERS[fmt](sample), "raw_record_id": raw_id})
        return outcome

    def _rebuild_telemetry(self) -> dict:
        self._drop(TELEMETRY_OBJECTS)
        self._create(TELEMETRY_DDL)
        rows = self.conn.execute("SELECT id, format, payload FROM raw_records ORDER BY id").fetchall()
        for row in rows:
            mapped = TELEMETRY_MAPPERS[row["format"]](json.loads(row["payload"]))
            self._put("telemetry_samples", {**mapped, "raw_record_id": row["id"]})
        return {"raw_records": len(rows)}

    # -- paired devices --------------------------------------------------

    def pair_peer(self, device_id: str, name: str, token_sha256: str) -> dict:
        """Pair a device, or pair it again with a new token (which also lifts an earlier unpair)."""
        stamp = now()
        self.conn.execute(
            "INSERT INTO sync_peers (device_id, name, token_sha256, paired_at, last_seen_at, revoked_at)"
            " VALUES (?, ?, ?, ?, ?, NULL) ON CONFLICT(device_id) DO UPDATE SET name = excluded.name,"
            " token_sha256 = excluded.token_sha256, paired_at = excluded.paired_at,"
            " last_seen_at = excluded.last_seen_at, revoked_at = NULL",
            (device_id, name, token_sha256, stamp, stamp),
        )
        return self.peer(device_id)  # type: ignore[return-value]

    def peer(self, device_id: str) -> dict | None:
        rows = self.rows("SELECT device_id, name, paired_at, last_seen_at, revoked_at FROM sync_peers"
                         " WHERE device_id = ?", [device_id])
        return rows[0] if rows else None

    def peer_for_token(self, token_sha256: str) -> dict | None:
        """The paired device a token belongs to, if it is still paired. Marks it as seen.

        Every live hash is compared in constant time, including after a match, so the
        check does not stop on the first byte that differs or the first peer that fits.
        """
        rows = self.conn.execute(
            "SELECT device_id, token_sha256 FROM sync_peers WHERE revoked_at IS NULL"
        ).fetchall()
        offered = token_sha256 if isinstance(token_sha256, str) else ""
        found = None
        for row in rows:
            stored = row["token_sha256"]
            if isinstance(stored, str) and len(stored) == len(offered) and secrets.compare_digest(stored, offered):
                found = row["device_id"]
        if found is None:
            return None
        self.conn.execute("UPDATE sync_peers SET last_seen_at = ? WHERE device_id = ?", (now(), found))
        return self.peer(found)

    def peers(self) -> list[dict]:
        return self.rows("SELECT device_id, name, paired_at, last_seen_at, revoked_at FROM sync_peers"
                         " ORDER BY paired_at DESC")

    def unpair_peer(self, device_id: str) -> dict:
        if not self.peer(device_id):
            raise NotFound(f"no paired device {device_id}")
        self.conn.execute("UPDATE sync_peers SET revoked_at = coalesce(revoked_at, ?) WHERE device_id = ?",
                          (now(), device_id))
        return self.peer(device_id)  # type: ignore[return-value]

    def effort(self, effort_id: str) -> dict:
        """A logged effort as it stands now, in the contract's performed-effort shape."""
        rows = self.rows("SELECT * FROM performed_efforts WHERE id = ?", [effort_id])
        if not rows:
            raise NotFound(f"no logged effort with id {effort_id}")
        return self.effort_payload(rows[0])

    def effort_payload(self, row: dict) -> dict:
        """A `performed_efforts` row and its metrics, as a performed effort."""
        effort = {k: row[k] for k in ("id", "activity_id", "performed_at", "planned_effort_id", "round", "side",
                                      "location_id", "session_id", "notes") if row[k] is not None}
        metrics = self.metrics_of("performed_effort", row["id"])
        if metrics:
            effort["metrics"] = metrics
        return effort

    def stored_event(self, event_id: str) -> dict | None:
        """The stored envelope for an event id, if there is one."""
        row = self.conn.execute("SELECT envelope FROM events WHERE id = ?", (event_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # -- whole store -----------------------------------------------------

    def rebuild(self) -> dict:
        """Drop every derived table and rebuild it from the event log and raw records."""
        with self._tx():
            return {"ledger": self._rebuild_ledger(), "telemetry": self._rebuild_telemetry()}

    def counts(self) -> dict:
        tables = ["events", "activities", "locations", "plans", "planned_sessions", "planned_blocks", "planned_efforts",
                  "performed_sessions", "performed_efforts", "metrics", "telemetry_samples", "raw_records"]
        return {t: self.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]


# -- read-only SQL ---------------------------------------------------------

_READ_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
if hasattr(sqlite3, "SQLITE_RECURSIVE"):
    _READ_ACTIONS.add(sqlite3.SQLITE_RECURSIVE)


def _authorize(action: int, *_: Any) -> int:
    return sqlite3.SQLITE_OK if action in _READ_ACTIONS else sqlite3.SQLITE_DENY


def query(path: Path, sql: str, params: Iterable[Any] = (), limit: int = 1000) -> dict:
    """Run one read-only SQL statement. Writes, ATTACH and PRAGMA are refused."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.set_authorizer(_authorize)
        try:
            cur = conn.execute(sql, tuple(params))
        except (sqlite3.DatabaseError, sqlite3.Warning) as exc:
            raise ValidationFailed(f"query refused: {exc}") from None
        columns = [d[0] for d in cur.description or []]
        fetched = cur.fetchmany(limit + 1)
        return {
            "columns": columns,
            "rows": [dict(zip(columns, r)) for r in fetched[:limit]],
            "truncated": len(fetched) > limit,
        }
    finally:
        conn.close()
