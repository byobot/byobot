"""One command registry. The CLI, the MCP server and the API are all generated from it,
so a person at a terminal and an agent get the same commands, the same arguments and the same results.
"""

from __future__ import annotations

import difflib
import getpass
import json
import os
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from byobot import __version__, library, whoop
from byobot.core import commands as core
from byobot.core import sources
from byobot.core.contract import SCHEMA_NAMES, SCHEMA_VERSION, Contract, Rejected, documents
from byobot.core.store import Store, ddl, query
from byobot.util import Conflict, NotFound, ValidationFailed, db_path, home, profile


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

    @property
    def tool(self) -> str:
        """The MCP tool name: `import activities` becomes `import-activities`."""
        return self.name.replace(" ", "-")


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
        if p.type == "string" and p.name in clean and not isinstance(clean[p.name], str):
            raise ValidationFailed(f"{p.name} must be a string")
    return cmd.handler(ctx, **clean)


def _object(name: str, value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValidationFailed(f"{name} must be a JSON object")
    return value


# -- the store ------------------------------------------------------------------

@command("init", "Create the local store (safe to run again).", read_only=False, mcp=False)
def _init(ctx: Context) -> dict:
    existed = ctx.path.exists()
    store = ctx.store()
    return {"home": str(home()), "profile": profile(), "db": str(ctx.path), "device_id": store.device_id,
            "created": not existed}


@command("status", "Show where the store is and how much it holds.")
def _status(ctx: Context) -> dict:
    store = ctx.store()
    return {"version": __version__, "schema_version": SCHEMA_VERSION, "db": str(ctx.path),
            "device_id": store.device_id, "counts": store.counts(),
            **({"clock_warnings": warnings} if (warnings := store.clock_warnings()) else {})}


@command("rebuild", "Rebuild every derived table from the byobot logbook.", read_only=False, mcp=False)
def _rebuild(ctx: Context) -> dict:
    ctx.store().rebuild()
    return {"counts": ctx.store().counts()}


@command(
    "export",
    "Write the whole byobot logbook to a file, to back it up or move it to a new computer.",
    Param("path", "string", "Where to write it, such as byobot-export.jsonl.", required=True, positional=True),
    mcp=False,
    description="The file holds everything, including the store's key; keep it private. Erasing a record later "
                "doesn't reach copies already exported.",
)
def _export(ctx: Context, path: str) -> dict:
    target = Path(path).expanduser()
    if target.exists():
        raise ValidationFailed(f"{target} already exists")
    partial = target.with_name(target.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(ctx.store().export())
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(target), "events": ctx.store().position()}


@command(
    "restore",
    "Restore the byobot logbook from an export into a new store.",
    Param("path", "string", "The export file.", required=True, positional=True),
    read_only=False,
    mcp=False,
    description="Only a store with nothing in it can be restored into. Phones pair again afterwards.",
)
def _restore(ctx: Context, path: str) -> dict:
    source = Path(path).expanduser()
    if not source.is_file():
        raise NotFound(f"no file {source}")
    store = ctx.store()
    if store.position():
        raise Conflict(f"{ctx.path} already holds a logbook; restore into a new profile or move it aside")
    with source.open(encoding="utf-8") as f:
        return {"db": str(ctx.path), **store.restore(f)}


# -- writing --------------------------------------------------------------------

LOGGABLE = (*core.AUTHORED, "batch")
WRITE_HELP = ("Payload shapes are in `byobot schema records`, codes and their units in `byobot schema registry`. "
              "Metrics are a map such as {\"reps\": {\"value\": 5}, \"load\": {\"value\": 100, \"unit\": \"kg\"}}; "
              "a target can be a range ({\"min\": 3, \"max\": 5}); your own codes start with x. "
              "Every write is an attributed event that `undo` can take back.")


@command(
    "log",
    "Create a record: an activity, a location, a setting, a plan, a planned session, a session, an effort or an "
    "observation. `batch` creates several at once, all or none.",
    Param("kind", "string", "What to create.", required=True, choices=LOGGABLE, positional=True),
    Param("payload", "json", "The record, as JSON; for batch, a list of {kind, id?, payload}. A session, effort or "
          "observation without a time is now.", required=True, positional=True,
          schema={"type": ["object", "array"]}),
    Param("id", "string", "The record's id. Generated if left out; activities, locations and settings need one."),
    read_only=False,
    description=WRITE_HELP + " An effort links to its session with links.part_of, and to the planned effort it "
                "answers with links.answers ({kind: planned_session, id, part}).",
)
def _log(ctx: Context, kind: str, payload: Any, id: str | None = None) -> dict:
    if kind == "batch":
        if not isinstance(payload, list) or not all(isinstance(r, dict) and "kind" in r for r in payload):
            raise ValidationFailed("a batch is a list of {kind, id?, payload}")
        _check_references(ctx.store(), payload)
        return {"events": core.log(ctx.store(), ctx.actor, payload)}
    record = {"kind": kind, "id": id, "payload": _object("payload", payload)}
    _check_references(ctx.store(), [record])
    [event] = core.log(ctx.store(), ctx.actor, [record])
    return {"event": event}


def _check_references(store: Store, records: list[dict]) -> None:
    """A planned session must name activities, a location and a plan that exist, or the app can't show it."""
    pending = {(r["kind"], r.get("id")) for r in records}
    wanted: dict[str, set] = {"activity": set(), "location": set(), "plan": set()}
    for r in records:
        p = r.get("payload")
        if r["kind"] != "planned_session" or not isinstance(p, dict):
            continue
        wanted["activity"] |= {e.get("activity_id") for b in p.get("blocks") or [] if isinstance(b, dict)
                               for e in b.get("efforts") or [] if isinstance(e, dict)}
        wanted["location"].add(p.get("location_id"))
        part_of = (p.get("links") or {}).get("part_of") if isinstance(p.get("links"), dict) else None
        wanted["plan"].add(part_of.get("id") if isinstance(part_of, dict) else None)
    errors = []
    for kind, table in (("activity", "activities"), ("location", "locations"), ("plan", "plans")):
        ids = sorted(i for i in wanted[kind] if isinstance(i, str) and (kind, i) not in pending)
        if not ids:
            continue
        known = [r["id"] for r in store.rows(f"SELECT id FROM {table}")]
        for missing in (i for i in ids if i not in known):
            error: dict = {"kind": kind, "id": missing}
            contains = sorted((k for k in known if missing in k), key=len)
            close = list(dict.fromkeys([*contains, *difflib.get_close_matches(missing, known, n=3, cutoff=0.5)]))[:3]
            if close:
                error["did_you_mean"] = close
            errors.append(error)
    if errors:
        raise ValidationFailed("the planned session names records that don't exist", details=errors)


@command(
    "observe",
    "Record one measurement you took, such as your weight.",
    Param("code", "string", "What was measured, as a registry code such as body_mass.", required=True,
          positional=True),
    Param("value", "json", "The value: a number, or text for a category.", required=True, positional=True,
          schema={"type": ["number", "string"]}),
    Param("unit", "string", "The unit, as the registry spells it, such as kg."),
    Param("start", "string", "When, as a timestamp. Defaults to now; without an offset it's local time."),
    Param("end", "string", "When it ended, for something measured over time."),
    Param("notes", "string", "Anything worth keeping with it."),
    read_only=False,
    description="Codes and units are in `byobot schema registry`.",
)
def _observe(ctx: Context, code: str, value: Any, unit: str | None = None, start: str | None = None,
             end: str | None = None, notes: str | None = None) -> dict:
    return {"event": core.observe(ctx.store(), ctx.actor, code, value, unit, start, end, notes=notes)}


@command(
    "edit",
    "Change a record you or your agent wrote, by a JSON merge patch over what it says now.",
    Param("kind", "string", "The record's kind.", required=True, choices=core.AUTHORED, positional=True),
    Param("id", "string", "The record's id.", required=True, positional=True),
    Param("patch", "json", "The fields to change. An object merges into an object; null removes a field.",
          required=True, positional=True, schema={"type": "object"}),
    read_only=False,
    description="A logged set is corrected this way: `edit effort <id> '{\"metrics\": {\"reps\": {\"value\": 6}}}'`. "
                "For a planned session's blocks use `replan`. To change what a source imported, use `correct`.",
)
def _edit(ctx: Context, kind: str, id: str, patch: Any) -> dict:
    return {"event": core.edit(ctx.store(), ctx.actor, kind, id, _object("patch", patch))}


@command(
    "replan",
    "Write a planned session's new version. Blocks and efforts left without ids keep the ids at their positions, "
    "so sets already logged against them stay linked.",
    Param("id", "string", "The planned session's id.", required=True, positional=True),
    Param("payload", "json", "The whole planned session, as JSON.", required=True, positional=True,
          schema={"type": "object"}),
    read_only=False,
)
def _replan(ctx: Context, id: str, payload: Any) -> dict:
    _check_references(ctx.store(), [{"kind": "planned_session", "id": id, "payload": _object("payload", payload)}])
    return {"event": core.replan(ctx.store(), ctx.actor, id, payload)}


@command(
    "remove",
    "Remove a record you or your agent wrote. `undo` brings it back.",
    Param("kind", "string", "The record's kind.", required=True, choices=core.AUTHORED, positional=True),
    Param("id", "string", "The record's id.", required=True, positional=True),
    Param("reason", "string", "Why, in a few words."),
    read_only=False,
)
def _remove(ctx: Context, kind: str, id: str, reason: str | None = None) -> dict:
    return {"event": core.remove(ctx.store(), ctx.actor, kind, id, reason)}


@command(
    "erase",
    "Destroy a record's content for good, here and on every synced device. Can't be undone.",
    Param("kind", "string", "The record's kind.", required=True, choices=(*core.AUTHORED, "source_record"),
          positional=True),
    Param("id", "string", "The record's id.", required=True, positional=True),
    read_only=False,
    mcp=False,
    description="Overlays on the record go with it. Exports made before now still hold it.",
)
def _erase(ctx: Context, kind: str, id: str) -> dict:
    return {"events": core.erase(ctx.store(), ctx.actor, kind, id),
            "note": "Exports made before now still hold this record."}


@command(
    "undo",
    "Take back a write: its record goes back to the version before it. The undo is a write too.",
    Param("event_id", "string", "The id of the event to undo.", required=True, positional=True),
    read_only=False,
    description="An event written in a batch is undone with the rest of its batch.",
)
def _undo(ctx: Context, event_id: str) -> dict:
    return {"events": core.undo(ctx.store(), ctx.actor, event_id)}


@command(
    "conflicts",
    "List records two devices or authors changed at once, each version with who wrote it and when.",
    Param("kind", "string", "Only this kind.", choices=core.AUTHORED),
    Param("id", "string", "Only this record."),
)
def _conflicts(ctx: Context, kind: str | None = None, id: str | None = None) -> dict:
    found = core.conflicts(ctx.store(), kind, id)
    return {"count": len(found), "items": found}


@command(
    "resolve",
    "End a conflict: keep one version, remove the record, or write a combined version.",
    Param("kind", "string", "The record's kind.", required=True, choices=core.AUTHORED, positional=True),
    Param("id", "string", "The record's id.", required=True, positional=True),
    Param("choice", "json", "The event id of the version to keep, \"remove\", or a combined payload.",
          required=True, positional=True, schema={"type": ["string", "object"]}),
    Param("seen", "json", "The event ids of the versions you were shown (from `conflicts`). A version that "
          "arrived since stays a conflict. Leave it out to end every current version.",
          schema={"type": "array", "items": {"type": "string"}}),
    read_only=False,
)
def _resolve(ctx: Context, kind: str, id: str, choice: Any, seen: Any = None) -> dict:
    if not isinstance(choice, (str, dict)):
        raise ValidationFailed("choice must be an event id, \"remove\" or a payload object")
    return {"event": core.resolve(ctx.store(), ctx.actor, kind, id, choice, seen)}


# -- what sources imported --------------------------------------------------------

@command(
    "correct",
    "Use your values over what a source imported, whatever the source later says.",
    Param("record_id", "string", "The source record's id (see `list sources` or `list workouts`).", required=True,
          positional=True),
    Param("fields", "json", "What to use instead: name, time, metrics, or value and unit for a single "
          "measurement. Merges into earlier corrections; null takes one back.", required=True, positional=True,
          schema={"type": "object"}),
    read_only=False,
)
def _correct(ctx: Context, record_id: str, fields: Any) -> dict:
    _source_record(ctx.store(), record_id)
    return {"event": core.correct(ctx.store(), ctx.actor, record_id, _object("fields", fields))}


@command(
    "exclude",
    "Leave something a source imported out of every read, such as a wrong weigh-in. `undo` brings it back.",
    Param("record_id", "string", "The source record's id.", required=True, positional=True),
    Param("notes", "string", "Why, in a few words."),
    read_only=False,
)
def _exclude(ctx: Context, record_id: str, notes: str | None = None) -> dict:
    _source_record(ctx.store(), record_id)
    return {"event": core.exclude(ctx.store(), ctx.actor, record_id, notes)}


@command(
    "same as",
    "Say an imported workout is the same as another workout, so it's counted once.",
    Param("record_id", "string", "The source record's id.", required=True, positional=True),
    Param("other_id", "string", "The session or source record it is the same as.", required=True, positional=True),
    Param("other_kind", "string", "What the other is.", choices=("session", "source_record"),
          default="session"),
    read_only=False,
)
def _same_as(ctx: Context, record_id: str, other_id: str, other_kind: str = "session") -> dict:
    store = ctx.store()
    _source_record(store, record_id)
    if store.get(other_kind, other_id) is None:
        raise NotFound(f"no {other_kind} {other_id}")
    return {"event": core.same_as(store, ctx.actor, record_id, other_kind, other_id)}


def _source_record(store: Store, rid: str) -> None:
    if store.get("source_record", rid) is None:
        raise NotFound(f"no source record {rid}")


@command(
    "history",
    "Show every version of a record, oldest first, with who wrote each and when.",
    Param("kind", "string", "The record's kind.", required=True, positional=True,
          choices=(*core.AUTHORED, "source_record", "overlay")),
    Param("id", "string", "The record's id.", required=True, positional=True),
)
def _history(ctx: Context, kind: str, id: str) -> dict:
    store = ctx.store()
    found = sources.history(store, id) if kind == "source_record" else store.history(kind, id)
    if not found:
        raise NotFound(f"no {kind} {id}")
    return {"kind": kind, "id": id, "record": store.record(kind, id), "versions": found}


@command(
    "import activities",
    "Import your own activities, with the muscles they work and the equipment they need, so the app can "
    "suggest swaps. Activities you already have are kept unless you say replace.",
    Param("activities", "json", "A list of activities, each with its id, or an object with an `activities` list. "
          "Use @path for a file.", required=True, positional=True, schema={"type": ["object", "array"]}),
    Param("existing", "string", "What to do with an id you already have.", choices=("keep", "replace"),
          default="keep"),
    read_only=False,
    description="One with the id of an activity that ships with byobot replaces it for you.",
)
def _import_activities(ctx: Context, activities: Any, existing: str = "keep") -> dict:
    items = activities.get("activities") if isinstance(activities, dict) else activities
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValidationFailed("expected a list of activities, or an object with an `activities` list")
    store = ctx.store()
    for i, item in enumerate(items):
        rid, payload = item.get("id"), {k: v for k, v in item.items() if k != "id"}
        errors = store.contract.payload_errors("activity", payload, rid if isinstance(rid, str) else None)
        if not isinstance(rid, str) or errors:
            raise ValidationFailed(f"activity {i} is not valid", details=errors or ["it needs an id"])
    added, replaced, kept, changes = [], [], [], []
    for item in items:
        rid, payload = item["id"], {k: v for k, v in item.items() if k != "id"}
        found = store.get("activity", rid)
        if found and (existing == "keep" or found["payload"] == payload):
            kept.append(rid)
            continue
        (replaced if found else added).append(rid)
        changes.append({"kind": "activity", "id": rid, "op": "put", "payload": payload})
    if changes:
        store.write(ctx.actor, changes)
    return {"added": added, "replaced": replaced, "kept": kept}


@command(
    "import whoop",
    "Import recovery, sleep, strain and workouts from WHOOP. The first import goes back 90 days; later ones "
    "pick up where the last left off. Importing again changes nothing.",
    Param("days", "integer", "Import this many days back instead."),
    read_only=False,
    description="Needs `byobot connect whoop` once, in a terminal. Every record WHOOP sends is kept as it sent it, "
                "with each rescore as a new version. A workout that overlaps one you logged is read as one workout; "
                "`correct` and `exclude` change how an import reads.",
)
def _import_whoop(ctx: Context, days: int | None = None) -> dict:
    if days is not None and days < 1:
        raise ValidationFailed("days must be at least 1")
    return whoop.import_into(ctx.store(), whoop.Client(), days)


@command(
    "connect whoop",
    "Connect byobot to your WHOOP account, with your own WHOOP developer app. Run it once, in a terminal.",
    Param("client_id", "string", "Your WHOOP app's client id. Asked for if left out."),
    read_only=False,
    mcp=False,
    description=f"Add {whoop.REDIRECT} as a redirect URL in your WHOOP app first. The client secret is asked "
                "for without showing it, and kept with the tokens in your Keychain.",
)
def _connect_whoop(ctx: Context, client_id: str | None = None) -> dict:
    if not sys.stdin.isatty():
        raise ValidationFailed("run `byobot connect whoop` in a terminal, so it can ask for your client secret")
    if not client_id:
        sys.stderr.write("WHOOP client id: ")
        client_id = input().strip()
    client_secret = getpass.getpass("WHOOP client secret (not shown): ").strip()
    if not client_id or not client_secret:
        raise ValidationFailed("a client id and a client secret are needed")
    return whoop.connect(client_id, client_secret, open_browser=_open_sign_in)


def _open_sign_in(url: str) -> None:
    sys.stderr.write(f"Opening WHOOP's sign-in in your browser. If it doesn't open, visit:\n{url}\n")
    webbrowser.open(url)


@command("disconnect whoop", "Disconnect byobot from WHOOP and forget the tokens. Your imported data stays.",
         read_only=False, mcp=False)
def _disconnect_whoop(ctx: Context) -> dict:
    return whoop.disconnect()


@command(
    "ingest",
    "Add samples read from a health store, such as Apple Health or a Hume scale: weight, heart rate, sleep, "
    "workouts. Sending the same samples again changes nothing.",
    Param("samples", "json", "One sample or a list, each {source, source_id, type, start, end, value, unit}, or an "
          "object {samples, deleted} where deleted lists {source, source_id} the health store removed.",
          required=True, positional=True, schema={"type": ["object", "array"]}),
    read_only=False,
    mcp=False,  # it writes as any source, so only the person runs it
)
def _ingest(ctx: Context, samples: Any) -> dict:
    deleted: list = []
    if isinstance(samples, dict) and ("samples" in samples or "deleted" in samples):
        samples, deleted = samples.get("samples") or [], samples.get("deleted") or []
    batch = samples if isinstance(samples, list) else [samples]
    if not isinstance(deleted, list):
        raise ValidationFailed("deleted must be a list")
    written = sources.ingest_samples(ctx.store(), batch, deleted)
    return {"written": {s: len(e) for s, e in written.items()}, "unchanged": len(batch) - sum(
        e["op"] == "put" for es in written.values() for e in es)}


# -- reading --------------------------------------------------------------------

LIST_KINDS = ("events", "activities", "locations", "settings", "plans", "planned", "sessions", "efforts",
              "observations", "workouts", "sources", "overlays")
# kind -> (table, time column for since/until, order, record kind)
LISTS = {
    "activities": ("activities", None, "name, id", "activity"),
    "locations": ("locations", None, "name, id", "location"),
    "settings": ("settings", None, "id", "setting"),
    "plans": ("plans", "starts_on", "starts_on DESC, id", "plan"),
    "planned": ("planned_sessions", "scheduled_on", "scheduled_on DESC, id", "planned_session"),
    "sessions": ("sessions", "start_ms", "start_ms DESC, id", "session"),
    "efforts": ("efforts", "start_ms", "start_ms DESC, id", "effort"),
    "observations": ("observations", "start_ms", "start_ms DESC, id", "observation"),
    "workouts": ("occurrences", "start_ms", "start_ms DESC, id", None),
    "sources": ("source_records", None, "revised_at DESC, id", "source_record"),
    "overlays": ("overlays", None, "id", "overlay"),
}
JSON_COLUMNS = ("tags", "equipment", "targets", "default_metrics", "resistance_curve", "value", "raw", "fields",
                "arrangement")
BRIEF_ACTIVITY = ("id", "name", "targets", "equipment", "movement_type", "fatigue", "default_metrics", "source")


def _bound(at: str) -> Any:
    """A date bounds a local date; a timestamp bounds an instant."""
    if len(at) == 10:
        return at
    try:
        return int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        raise ValidationFailed(f"{at!r} is not a date or a timestamp") from None


def metrics_of(store: Store, kind: str, rid: str, part: str = "") -> dict:
    out = {}
    for r in store.rows("SELECT * FROM metrics WHERE owner_kind = ? AND owner_id = ? AND part_id = ? ORDER BY code",
                        (kind, rid, part)):
        m = {k: r[k] for k in ("value", "min", "max", "unit") if r[k] is not None}
        if r["text"] is not None:
            m["value"] = r["text"]
        if r["overlay_id"]:
            m["corrected_by"] = r["overlay_id"]
        out[r["code"]] = m
    return out


@command(
    "list",
    "List records of one kind, newest first: sessions you logged (sessions), sets (efforts), measurements "
    "(observations), workouts as one per occasion across you and your sources (workouts), and more.",
    Param("kind", "string", "What to list.", required=True, choices=LIST_KINDS, positional=True),
    Param("type", "string", "Only this record kind (events), code (observations) or source type (sources)."),
    Param("activity", "string", "Only this activity id (efforts)."),
    Param("source", "string", "Only this source, such as whoop (observations, sources, workouts), or for "
          "activities `library` (shipped with byobot) or `store` (yours)."),
    Param("muscle", "string", "Only activities that work this muscle, as main or synergist (activities)."),
    Param("equipment", "string", "Only activities that use this equipment (activities)."),
    Param("detail", "string", "brief keeps only what picking an activity needs (activities).",
          choices=("full", "brief"), default="full"),
    Param("since", "string", "Only records on or after this date, or at or after this timestamp."),
    Param("until", "string", "Only records before this date or timestamp."),
    Param("limit", "integer", "At most this many records.", default=50),
    description="Muscle and equipment names are in `byobot schema vocabulary`. Dates are local dates, as the "
                "record was written.",
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
        sql, order, day = "SELECT seq, id, kind, record_id, op, hlc, ts, device_id, actor_kind, actor_id, " \
                          "actor_model, batch, reverts, payload FROM events", "seq DESC", "substr(ts, 1, 10)"
        add("kind = ?", type)
        for value, op in ((since, ">="), (until, "<")):
            if value is not None:
                add(f"{'ts' if len(value) > 10 else day} {op} ?", value)
    else:
        table, time, order, _ = LISTS[kind]
        sql = f"SELECT * FROM {table}"
        add("code = ?", type if kind == "observations" else None)
        add("source_type = ?", type if kind == "sources" else None)
        add("activity_id = ?", activity if kind == "efforts" else None)
        if kind in ("observations", "sources", "activities"):
            add("source = ?", source)
        if kind == "workouts" and source is not None:
            add("id IN (SELECT occurrence_id FROM occurrence_items i JOIN source_records s ON s.id = i.id "
                "WHERE s.source = ?)", source)
        if kind == "activities":
            if muscle is not None:
                where.append("EXISTS (SELECT 1 FROM json_each(targets, '$.main') WHERE value = ?"
                             " UNION ALL SELECT 1 FROM json_each(targets, '$.synergists') WHERE value = ?)")
                params.extend([muscle, muscle])
            add("EXISTS (SELECT 1 FROM json_each(equipment) WHERE value = ?)", equipment)
        if time is not None:
            for value, op in ((since, ">="), (until, "<")):
                if value is not None:
                    bound = _bound(value) if time == "start_ms" else value[:10]
                    add(f"{'local_date' if isinstance(bound, str) and time == 'start_ms' else time} {op} ?", bound)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = store.rows(f"{sql} ORDER BY {order} LIMIT ?", [*params, max(0, limit)])
    for row in rows:
        for col in JSON_COLUMNS:
            if isinstance(row.get(col), str) and (kind != "observations" or col != "value"):
                row[col] = json.loads(row[col])
        if kind == "events":
            row["payload"] = json.loads(row["payload"])
        if kind == "activities" and detail == "brief":
            for col in [c for c in row if c not in BRIEF_ACTIVITY or row[c] is None]:
                del row[col]
        if kind in ("sessions", "efforts"):
            row["metrics"] = metrics_of(store, LISTS[kind][3], row["id"])
        if kind == "sessions":
            row["efforts"] = [e["id"] for e in store.rows(
                "SELECT id FROM efforts WHERE session_id = ? ORDER BY start_ms, id", (row["id"],))]
        if kind == "planned":
            _planned(store, row)
        if kind == "workouts":
            _workout(store, row)
    return {"kind": kind, "count": len(rows), "items": rows}


def _planned(store: Store, row: dict) -> None:
    rid = row["id"]
    row["metrics"] = metrics_of(store, "planned_session", rid)
    row["blocks"] = store.rows("SELECT id, name, rounds FROM planned_blocks WHERE planned_session_id = ? "
                               "ORDER BY position", (rid,))
    for block in row["blocks"]:
        block["metrics"] = metrics_of(store, "planned_session", rid, block["id"])
        block["efforts"] = store.rows("SELECT id, activity_id, side FROM planned_efforts WHERE planned_session_id = ? "
                                      "AND block_id = ? ORDER BY position", (rid, block["id"]))
        for effort in block["efforts"]:
            effort["metrics"] = metrics_of(store, "planned_session", rid, effort["id"])


def _workout(store: Store, row: dict) -> None:
    oid = row.pop("id")
    row["items"] = store.rows("SELECT i.kind, i.id, s.source FROM occurrence_items i LEFT JOIN source_records s "
                              "ON i.kind = 'source_record' AND s.id = i.id WHERE occurrence_id = ? "
                              "ORDER BY i.kind, i.id", (oid,))
    row["metrics"] = {}
    for m in store.rows("SELECT * FROM occurrence_metrics WHERE occurrence_id = ? ORDER BY code", (oid,)):
        value = {k: m[k] for k in ("value", "unit") if m[k] is not None}
        if m["text"] is not None:
            value["value"] = m["text"]
        row["metrics"][m["code"]] = {**value, "from": {"kind": m["from_kind"], "id": m["from_id"]}}


@command(
    "query",
    "Run one read-only SQL query against the store.",
    Param("sql", "string", "A single SELECT (or WITH ... SELECT) statement.", required=True, positional=True),
    Param("limit", "integer", "At most this many rows.", default=1000),
    description="Tables are listed by `byobot schema sql`. Every metric is one row in `metrics`, with std_value "
                "in its code's own unit; `occurrences` holds each workout once, across you and your sources.",
)
def _query(ctx: Context, sql: str, limit: int = 1000) -> dict:
    ctx.store()  # make sure the store exists before opening it read-only
    return query(ctx.path, sql, limit=max(0, limit))


SCHEMA_CHOICES = (*SCHEMA_NAMES, "sql", "vocabulary")


@command(
    "schema",
    "Show the byobot contract (JSON Schema and the code registry), the store's SQL tables, or the muscle and "
    "equipment names (vocabulary).",
    Param("name", "string", "Which one. Leave out to list them.", choices=SCHEMA_CHOICES, positional=True),
)
def _schema(ctx: Context, name: str | None = None) -> Any:
    if name is None:
        return {"schema_version": SCHEMA_VERSION, "schemas": list(SCHEMA_CHOICES)}
    if name == "sql":
        return {"name": "sql", "ddl": ddl()}
    if name == "vocabulary":
        return {"name": "vocabulary", **library.vocabulary()}
    schemas, registry = documents()
    return registry if name == "registry" else schemas[name]


VALIDATE_AS = ("event", *sorted(Contract.shipped().registry["kinds"]))


@command(
    "validate",
    "Check a document against the contract without writing anything.",
    Param("document", "json", "The JSON document to check.", required=True, positional=True, schema={}),
    Param("as", "string", "What the document is: a whole event, or the payload of a record kind.",
          choices=VALIDATE_AS, default="event"),
)
def _validate(ctx: Context, document: Any, **kwargs: Any) -> dict:
    kind = kwargs.get("as", "event")
    contract = Contract.shipped()
    if kind != "event":
        errors = contract.payload_errors(kind, document)
    else:
        try:
            contract.check(document)
            errors = []
        except Rejected as exc:
            errors = [exc.reason]
    return {"valid": not errors, "as": kind, "errors": errors}


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
    return {"device": ctx.store().unpair(device_id)}


@command(
    "review",
    "Summarize recent training: sessions and workouts done, planned sessions done and missed, the last two days "
    "of sets for each exercise at each location, weekly sets per muscle, daily recovery, sleep, body, activity, "
    "mobility and vitals, and what is planned from today on.",
    Param("weeks", "integer", "How many weeks back to look.", default=4),
    Param("until", "string", "The last day to include, as a date. Defaults to today."),
    description="Sets count once for each muscle an activity works, split into main and synergist. Loads compare "
                "only within one location. Each day's value comes from one source: yours, then sources in "
                "source_priority order.",
)
def _review(ctx: Context, weeks: int = 4, until: str | None = None) -> dict:
    from byobot.review import review

    if weeks < 1:
        raise ValidationFailed("weeks must be at least 1")
    try:
        return review(ctx.store(), weeks, until)
    except ValueError as exc:
        raise ValidationFailed(f"until must be a date such as 2026-11-23: {exc}") from None
