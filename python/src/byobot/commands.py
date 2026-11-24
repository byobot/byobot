"""One command registry. The CLI and the MCP server are both generated from it,
so a person at a terminal and an agent over MCP get the same commands, the
same arguments and the same results.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from byobot import __version__, contract, library
from byobot.store import Store, full_ddl, query
from byobot.util import ValidationFailed, db_path, home, now, uuid7


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # "string" | "integer" | "json"
    help: str
    required: bool = False
    choices: tuple[str, ...] = ()
    default: Any = None
    positional: bool = False
    schema: dict | None = None  # JSON Schema for a "json" param, shown to agents


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    handler: Callable[..., Any]
    params: tuple[Param, ...] = ()
    read_only: bool = True
    cli: bool = True
    mcp: bool = True
    description: str = ""


@dataclass
class Context:
    """What a command runs with: who is acting, and where the store lives."""

    actor: dict
    path: Path = field(default_factory=db_path)
    _store: Store | None = None

    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.path)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


REGISTRY: dict[str, Command] = {}


def command(name: str, summary: str, *params: Param, read_only: bool = True, cli: bool = True,
            mcp: bool = True, description: str = "") -> Callable:
    def register(fn: Callable) -> Callable:
        REGISTRY[name] = Command(name, summary, fn, params, read_only, cli, mcp, description)
        return fn

    return register


def run(name: str, ctx: Context, args: dict) -> Any:
    cmd = REGISTRY[name]
    known = {p.name for p in cmd.params}
    clean = {k: v for k, v in args.items() if k in known and v is not None}
    for p in cmd.params:
        if p.required and p.name not in clean:
            raise ValidationFailed(f"missing required argument {p.name!r}")
        if p.choices and p.name in clean and clean[p.name] not in p.choices:
            raise ValidationFailed(f"{p.name} must be one of {', '.join(p.choices)}")
        if p.type == "integer" and p.name in clean and (isinstance(clean[p.name], bool) or not isinstance(clean[p.name], int)):
            raise ValidationFailed(f"{p.name} must be an integer")
    return cmd.handler(ctx, **clean)


# -- commands ----------------------------------------------------------------

@command("init", "Create the local store (safe to run again).", read_only=False, mcp=False)
def _init(ctx: Context) -> dict:
    existed = ctx.path.exists()
    store = ctx.store()
    return {"home": str(home()), "db": str(ctx.path), "device_id": store.device_id, "created": not existed}


LOGGABLE = tuple(t for t in contract.EVENT_TYPES if t != "event.undone")


def _fill_defaults(event_type: str, payload: dict) -> dict:
    """Fill what a caller can reasonably leave out: new ids, and 'now' for something just done."""
    payload = dict(payload)
    if event_type not in ("activity.defined", "location.defined"):
        payload.setdefault("id", uuid7())
    if event_type == "effort.performed":
        payload.setdefault("performed_at", now())
    if event_type == "session.performed":
        payload.setdefault("started_at", now())
        payload["efforts"] = _with_ids(payload.get("efforts"))
        if payload["efforts"] is None:
            payload.pop("efforts")
    if event_type == "session.planned":
        blocks = _with_ids(payload.get("blocks"))
        if blocks is not None:
            payload["blocks"] = [
                {**b, "efforts": _with_ids(b.get("efforts"))} if isinstance(b, dict) and "efforts" in b else b
                for b in blocks
            ]
    return payload


def _with_ids(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    return [{**i, "id": i.get("id") or uuid7()} if isinstance(i, dict) else i for i in items]


@command(
    "log",
    "Record something in the ledger: an activity, a location, a plan, a planned session, a performed session or a performed effort.",
    Param("type", "string", "What to record.", required=True, choices=LOGGABLE, positional=True),
    Param("payload", "json", "The record, as JSON. Ids are generated if left out; performed_at and started_at default "
          "to now.", required=True, positional=True, schema={"type": "object"}),
    read_only=False,
    description=(
        "Payload shapes are in `byobot schema training`, metric names and units in `byobot schema common`. "
        "Metrics are a map such as {\"reps\": {\"value\": 5}, \"load\": {\"value\": 100, \"unit\": \"kg\"}}; "
        "a target can be a range ({\"min\": 3, \"max\": 5}); your own metrics start with x. "
        "Every entry is an attributed, undoable event."
    ),
)
def _log(ctx: Context, type: str, payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValidationFailed("payload must be a JSON object")
    if type == "session.planned":
        _check_references(ctx.store(), payload)
    event = ctx.store().append(type, _fill_defaults(type, payload), ctx.actor)
    return {"event": event}


def _check_references(store: Store, session: dict) -> None:
    """A planned session must name activities, a location and a plan that exist, or the app can't show it."""
    errors = []
    for table, kind, ids in (
        ("activities", "activity", {e.get("activity_id") for b in session.get("blocks") or [] if isinstance(b, dict)
                                    for e in b.get("efforts") or [] if isinstance(e, dict)}),
        ("locations", "location", {session.get("location_id")}),
        ("plans", "plan", {session.get("plan_id")}),
    ):
        ids = sorted(i for i in ids if isinstance(i, str))
        if not ids:
            continue
        known = {r["id"]: r["name"] for r in store.rows(f"SELECT id, name FROM {table}")}
        for missing in (i for i in ids if i not in known):
            error = {"kind": kind, "id": missing}
            contains = sorted((k for k in known if missing in k), key=len)
            close = list(dict.fromkeys([*contains, *difflib.get_close_matches(missing, known, n=3, cutoff=0.5)]))[:3]
            if close:
                error["did_you_mean"] = close
            errors.append(error)
    if errors:
        raise ValidationFailed("the planned session names records that don't exist; define them first "
                               "(activity.defined, location.defined, plan.defined) or use existing ids",
                               details=errors)


CORRECTABLE = ("activity_id", "performed_at", "planned_effort_id", "round", "side", "location_id", "session_id", "notes",
               "metrics")


@command(
    "correct",
    "Correct a logged effort: change its metrics, side or notes. It keeps when it was performed, its round and "
    "the planned effort it answers, unless you change those too.",
    Param("effort_id", "string", "The id of the logged effort.", required=True, positional=True),
    Param("changes", "json", "The fields to change, as JSON. `metrics` replaces the whole map; null removes a field.",
          required=True, positional=True, schema={"type": "object"}),
    read_only=False,
    description=(
        "A correction is a new effort.performed event with the same effort id. The newest event wins, so tables "
        "such as plan_vs_actual and strength_sets show the corrected values, and undoing the correction restores "
        "what was there before. Correctable fields: " + ", ".join(CORRECTABLE) + "."
    ),
)
def _correct(ctx: Context, effort_id: str, changes: Any) -> dict:
    if not isinstance(changes, dict):
        raise ValidationFailed("changes must be a JSON object")
    unknown = sorted(set(changes) - set(CORRECTABLE))
    if unknown:
        raise ValidationFailed(f"can't correct {', '.join(unknown)}; correctable fields are {', '.join(CORRECTABLE)}")
    store = ctx.store()
    effort = store.effort(effort_id)
    effort.update(changes)
    effort = {k: v for k, v in effort.items() if v is not None}
    return {"event": store.append("effort.performed", effort, ctx.actor), "effort": effort}


@command(
    "import-activities",
    "Import your own activities, with the muscles they work and the equipment they need, so the app can "
    "suggest swaps. Activities you already defined are kept unless you say replace.",
    Param("activities", "json", "A list of activities, or an object with an `activities` list. Use @path for a file.",
          required=True, positional=True, schema={"type": ["object", "array"]}),
    Param("existing", "string", "What to do with an id you already defined.", choices=("keep", "replace"),
          default="keep"),
    read_only=False,
)
def _import_activities(ctx: Context, activities: Any, existing: str = "keep") -> dict:
    items = activities.get("activities") if isinstance(activities, dict) else activities
    if not isinstance(items, list):
        raise ValidationFailed("expected a list of activities, or an object with an `activities` list")
    for i, item in enumerate(items):
        errors = contract.validate_payload("activity.defined", item)
        if errors:
            raise ValidationFailed(f"activity {i} is not valid", details=errors)
    store = ctx.store()
    defined = {r["id"]: json.loads(r["payload"]) for r in store.rows(
        "SELECT a.id, e.payload FROM activities a JOIN events e ON e.id = a.event_id")}
    added, replaced, kept = [], [], []
    for item in items:
        if item["id"] in defined:
            if existing == "keep" or defined[item["id"]] == item:
                kept.append(item["id"])
                continue
            replaced.append(item["id"])
        else:
            added.append(item["id"])
        store.append("activity.defined", item, ctx.actor)
    return {"added": added, "replaced": replaced, "kept": kept}


@command(
    "undo",
    "Undo an event. The undo is itself an event, so it can be undone too.",
    Param("event_id", "string", "The id of the event to undo.", required=True, positional=True),
    Param("reason", "string", "Why, in a few words."),
    read_only=False,
)
def _undo(ctx: Context, event_id: str, reason: str | None = None) -> dict:
    payload: dict = {"event_id": event_id}
    if reason:
        payload["reason"] = reason
    return {"event": ctx.store().append("event.undone", payload, ctx.actor)}


@command(
    "ingest",
    "Add telemetry samples (weight, HRV, resting heart rate, sleep). Re-ingesting the same samples changes nothing.",
    Param("samples", "json", "One sample or a list of samples, as JSON (see `byobot schema telemetry`).",
          required=True, positional=True, schema={"type": ["object", "array"]}),
    read_only=False,
)
def _ingest(ctx: Context, samples: Any) -> dict:
    batch = samples if isinstance(samples, list) else [samples]
    return ctx.store().ingest(batch)


@command(
    "import-open-wearables",
    "Import recovery, sleep and strain from Open Wearables, which connects WHOOP and other wearables. "
    "Re-importing changes nothing.",
    Param("user", "string", "Your Open Wearables user id. Defaults to $OPEN_WEARABLES_USER_ID."),
    Param("url", "string", "Where Open Wearables runs. Defaults to $OPEN_WEARABLES_URL, else http://localhost:8000."),
    Param("days", "integer", "How many days back to import.", default=30),
    Param("provider", "string", "Only this provider, such as whoop."),
    read_only=False,
    description="The API key is read from $OPEN_WEARABLES_API_KEY.",
)
def _import_open_wearables(ctx: Context, user: str | None = None, url: str | None = None, days: int = 30,
                           provider: str | None = None) -> dict:
    import os
    from datetime import date, timedelta

    from byobot import open_wearables

    user = user or os.environ.get("OPEN_WEARABLES_USER_ID")
    url = url or os.environ.get("OPEN_WEARABLES_URL") or "http://localhost:8000"
    key = os.environ.get("OPEN_WEARABLES_API_KEY")
    if not user:
        raise ValidationFailed("pass --user or set OPEN_WEARABLES_USER_ID")
    if not key:
        raise ValidationFailed("set OPEN_WEARABLES_API_KEY to an Open Wearables API key")
    if days < 1:
        raise ValidationFailed("days must be at least 1")
    end = date.today() + timedelta(days=1)
    client = open_wearables.Client(url, user, key)
    found = open_wearables.collect(client, (end - timedelta(days=days + 1)).isoformat(), end.isoformat(), provider)
    counts = ctx.store().ingest(found["samples"]) if found["samples"] else {"inserted": 0, "updated": 0, "unchanged": 0}
    return {"telemetry": counts, "skipped": found["skipped"], "open_wearables_api": open_wearables.version()}


BRIEF_ACTIVITY = ("id", "name", "targets", "equipment", "movement_type", "fatigue", "default_metrics", "source")
LIST_KINDS = ("events", "activities", "locations", "plans", "planned", "sessions", "efforts", "telemetry")


@command(
    "list",
    "List recent records of one kind, newest first: planned sessions (planned), performed sessions (sessions), "
    "performed efforts (efforts) and more.",
    Param("kind", "string", "What to list.", required=True, choices=LIST_KINDS, positional=True),
    Param("type", "string", "Only this event type (events) or telemetry type (telemetry)."),
    Param("activity", "string", "Only this activity id (efforts)."),
    Param("source", "string", "Only this source: a telemetry source such as whoop, or for activities `library` "
          "(shipped with byobot) or `ledger` (defined by you or your agent)."),
    Param("muscle", "string", "Only activities that work this muscle, as main or synergist (activities)."),
    Param("equipment", "string", "Only activities that use this equipment (activities)."),
    Param("detail", "string", "brief keeps only what picking an activity needs (activities).",
          choices=("full", "brief"), default="full"),
    Param("since", "string", "Only records at or after this date or timestamp."),
    Param("until", "string", "Only records before this date or timestamp."),
    Param("limit", "integer", "At most this many records.", default=50),
    description="Muscle and equipment names are in `byobot schema vocabulary`.",
)
def _list(ctx: Context, kind: str, type: str | None = None, activity: str | None = None, source: str | None = None,
          muscle: str | None = None, equipment: str | None = None, detail: str = "full",
          since: str | None = None, until: str | None = None, limit: int = 50) -> dict:
    store = ctx.store()
    where: list[str] = []
    params: list[Any] = []

    def add(clause: str, value: Any) -> None:
        if value is not None:
            where.append(clause)
            params.append(value)

    if kind == "events":
        time_col, sql = "e.ts", (
            "SELECT e.seq, e.id, e.type, e.ts, e.device_id, e.actor_kind, e.actor_id, e.actor_model, e.payload,"
            " u.undo_event_id AS undone_by FROM events e LEFT JOIN undone_events u ON u.event_id = e.id"
        )
        add("e.type = ?", type)
        order = "e.seq DESC"
    elif kind == "activities":
        time_col, sql, order = "authored_at", "SELECT * FROM activities", "name"
        add("source = ?", source)
        if muscle is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(targets, '$.main') WHERE value = ?"
                         " UNION ALL SELECT 1 FROM json_each(targets, '$.synergists') WHERE value = ?)")
            params.extend([muscle, muscle])
        add("EXISTS (SELECT 1 FROM json_each(equipment) WHERE value = ?)", equipment)
    elif kind == "locations":
        time_col, sql, order = "authored_at", "SELECT * FROM locations", "name"
    elif kind == "plans":
        time_col, sql, order = "starts_on", "SELECT * FROM plans", "starts_on DESC"
    elif kind == "planned":
        time_col, sql, order = "scheduled_on", "SELECT * FROM planned_sessions", "scheduled_on DESC"
    elif kind == "sessions":
        time_col, sql, order = "started_at", "SELECT * FROM performed_sessions", "started_at DESC"
    elif kind == "efforts":
        time_col, sql, order = "performed_at", "SELECT * FROM performed_efforts", "performed_at DESC"
        add("activity_id = ?", activity)
    else:
        time_col, sql, order = "start_at", "SELECT * FROM telemetry_samples", "start_at DESC"
        add("type = ?", type)
        add("source = ?", source)
    add(f"{time_col} >= ?", since)
    add(f"{time_col} < ?", until)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    rows = store.rows(sql, [*params, max(0, limit)])
    for row in rows:
        if kind == "activities":
            del row["payload"]
            if detail == "brief":
                for col in [c for c in row if c not in BRIEF_ACTIVITY or row[c] is None]:
                    del row[col]
        for col in ("tags", "default_metrics", "targets", "equipment", "resistance_curve", "payload"):
            if isinstance(row.get(col), str):
                row[col] = json.loads(row[col])
    if kind == "planned":
        for row in rows:
            row["metrics"] = store.metrics_of("planned_session", row["id"])
            row["blocks"] = store.rows("SELECT * FROM planned_blocks WHERE planned_session_id = ? ORDER BY position",
                                       [row["id"]])
            for block in row["blocks"]:
                block["metrics"] = store.metrics_of("planned_block", block["id"])
                block["efforts"] = store.rows("SELECT * FROM planned_efforts WHERE block_id = ? ORDER BY position",
                                              [block["id"]])
                for effort in block["efforts"]:
                    effort["metrics"] = store.metrics_of("planned_effort", effort["id"])
    elif kind == "sessions":
        for row in rows:
            row["metrics"] = store.metrics_of("performed_session", row["id"])
            row["efforts"] = store.rows("SELECT * FROM performed_efforts WHERE session_id = ? ORDER BY performed_at",
                                        [row["id"]])
            for effort in row["efforts"]:
                effort["metrics"] = store.metrics_of("performed_effort", effort["id"])
    elif kind == "efforts":
        for row in rows:
            row["metrics"] = store.metrics_of("performed_effort", row["id"])
    return {"kind": kind, "count": len(rows), "items": rows}


@command(
    "query",
    "Run one read-only SQL query against the store.",
    Param("sql", "string", "A single SELECT (or WITH ... SELECT) statement.", required=True, positional=True),
    Param("limit", "integer", "At most this many rows.", default=1000),
    description="Tables and views are listed by `byobot schema sql`. Metrics are one row each in `metrics`; the views "
                "plan_vs_actual and strength_sets give plain columns.",
)
def _query(ctx: Context, sql: str, limit: int = 1000) -> dict:
    ctx.store()  # make sure the store exists before opening it read-only
    return query(ctx.path, sql, limit=max(0, limit))


SCHEMA_CHOICES = (*contract.SCHEMA_NAMES, "sql", "vocabulary")


@command(
    "schema",
    "Show the byobot contract (JSON Schema), the store's SQL tables, or the muscle and equipment names (vocabulary).",
    Param("name", "string", "Which schema. Leave out to list them.", choices=SCHEMA_CHOICES, positional=True),
)
def _schema(ctx: Context, name: str | None = None) -> Any:
    if name is None:
        return {"schema_version": contract.SCHEMA_VERSION, "schemas": list(SCHEMA_CHOICES)}
    if name == "sql":
        return {"name": "sql", "ddl": full_ddl()}
    if name == "vocabulary":
        return {"name": "vocabulary", **library.vocabulary()}
    return contract.schema(name)


VALIDATE_AS = ("event", "telemetry", *contract.EVENT_TYPES)


@command(
    "validate",
    "Check a document against the contract without writing anything.",
    Param("document", "json", "The JSON document to check.", required=True, positional=True, schema={}),
    Param("as", "string", "What the document is: a whole event, a telemetry sample, or the payload for an event type.",
          choices=VALIDATE_AS, default="event"),
)
def _validate(ctx: Context, document: Any, **kwargs: Any) -> dict:
    kind = kwargs.get("as", "event")
    if kind in ("event", "telemetry"):
        errors = contract.validate(kind, document)
    else:
        errors = contract.validate_payload(kind, document)
    return {"valid": not errors, "as": kind, "errors": errors}


@command("rebuild", "Rebuild every derived table from the event log and raw records.", read_only=False, mcp=False)
def _rebuild(ctx: Context) -> dict:
    return ctx.store().rebuild()


@command("status", "Show where the store is and how much it holds.")
def _status(ctx: Context) -> dict:
    store = ctx.store()
    return {"version": __version__, "db": str(ctx.path), "device_id": store.device_id, "counts": store.counts()}


@command("devices", "List the phones and other devices paired with `byobot serve`.")
def _devices(ctx: Context) -> dict:
    peers = ctx.store().peers()
    return {"count": len(peers), "items": peers}


@command(
    "unpair",
    "Stop syncing with a paired device. Its token stops working at once; pairing again starts fresh.",
    Param("device_id", "string", "The device to unpair (see `byobot devices`).", required=True, positional=True),
    read_only=False,
    mcp=False,
)
def _unpair(ctx: Context, device_id: str) -> dict:
    return {"device": ctx.store().unpair_peer(device_id)}


@command(
    "review",
    "Summarize recent training: sessions done, planned sessions done and missed, the last two days of sets for each "
    "exercise at each location, weekly sets per muscle, and what is already planned from today on.",
    Param("weeks", "integer", "How many weeks back to look.", default=4),
    Param("until", "string", "The last day to include, as a date. Defaults to today."),
    description="Sets count once for each muscle an activity works, split into main and synergist. Loads compare "
                "only within one location_id.",
)
def _review(ctx: Context, weeks: int = 4, until: str | None = None) -> dict:
    from byobot.review import review

    if weeks < 1:
        raise ValidationFailed("weeks must be at least 1")
    try:
        return review(ctx.store(), weeks, until)
    except ValueError as exc:
        raise ValidationFailed(f"until must be a date such as 2026-11-23: {exc}") from None
