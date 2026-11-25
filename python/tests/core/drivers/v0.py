"""Today's store behind the core driver: Store, the command registry, imports, whoop and apple_health."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from byobot import apple_health, commands, review, whoop
from byobot.store import Store
from byobot.util import ByobotError, Conflict

from ..driver import Unsupported
from ..snapshot import canonical, empty, key, occurrence

LOG_TYPE = {
    "define_activity": "activity.defined", "define_location": "location.defined", "define_plan": "plan.defined",
    "plan_session": "session.planned", "replan_session": "session.planned", "log_session": "session.performed",
    "log_effort": "effort.performed",
}
TABLES = {
    "location": "locations", "plan": "plans", "planned_session": "planned_sessions", "session": "performed_sessions",
    "effort": "performed_efforts",
}
TYPES = {
    "activity": ("activity.defined",), "location": ("location.defined",), "plan": ("plan.defined",),
    "planned_session": ("session.planned",), "session": ("session.performed",),
    "effort": ("effort.performed", "session.performed"),
}
CONNECTOR_PREFIXES = ("whoop:", "apple-health:")
WHOOP_PATHS = {
    "sleep": ("/v2/activity/sleep", "PaginatedSleepResponse"), "recovery": ("/v2/recovery", "RecoveryCollection"),
    "cycle": ("/v2/cycle", "PaginatedCycleResponse"), "workout": ("/v2/activity/workout", "WorkoutCollection"),
}


class _Pages:
    """Stands in for whoop.Client: serves one batch of records through the real page check."""

    def __init__(self, batch: dict):
        self.by_path = {path: batch.get(kind, []) for kind, (path, _) in WHOOP_PATHS.items()}

    def pages(self, path: str, name: str, start: str):
        body = {"records": list(self.by_path[path])}
        whoop.check(body, name)
        yield from body["records"]


class V0:
    name = "v0"

    # -- lifecycle ---------------------------------------------------------

    def open(self, name: str, directory: Path) -> Store:
        store = Store(Path(directory) / f"{name}.db")
        store._set_meta("device_id", name)
        return store

    def close(self, store: Store) -> None:
        store.close()

    @contextmanager
    def _at(self, at: str):
        with mock.patch("byobot.store.now", return_value=at):
            yield

    def _new_events(self, store: Store, since: int) -> list[dict]:
        rows = store.conn.execute("SELECT envelope FROM events WHERE seq > ? ORDER BY seq", (since,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def _seq(self, store: Store) -> int:
        return store.conn.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]

    # -- writes ------------------------------------------------------------

    def author(self, store: Store, intent: dict, at: str, actor: dict) -> list[dict]:
        before = self._seq(store)
        ctx = commands.Context(actor=actor, path=store.path, _store=store)
        name = intent["intent"]
        with self._at(at):
            if name in LOG_TYPE:
                commands.run("log", ctx, {"type": LOG_TYPE[name], "payload": intent["record"]})
            elif name == "edit":
                self._edit(ctx, *intent["record"], intent["fields"])
            elif name == "undo":
                commands.run("undo", ctx, {"event_id": intent["event"]["id"]})
            elif name == "remove":
                commands.run("undo", ctx, {"event_id": self._creator(store, *intent["record"])})
            elif name == "correct_import":
                self._edit(ctx, "session", f"{intent['source']}:workout:{intent['source_id']}", intent["fields"])
            else:
                raise Unsupported(name)
        return self._new_events(store, before)

    def _edit(self, ctx: commands.Context, kind: str, rid: str, fields: dict) -> None:
        if kind == "effort":
            commands.run("correct", ctx, {"effort_id": rid, "changes": fields})
            return
        current = self._payload(ctx.store(), kind, rid)
        merged = {k: v for k, v in {**current, **fields}.items() if v is not None}
        commands.run("log", ctx, {"type": TYPES[kind][0], "payload": merged})

    def _payload(self, store: Store, kind: str, rid: str) -> dict:
        table = "activities" if kind == "activity" else TABLES[kind]
        row = store.conn.execute(f"SELECT event_id FROM {table} WHERE id = ?", (rid,)).fetchone()
        if not row or not row[0]:
            raise Unsupported(f"no {kind} {rid}")
        return json.loads(store.conn.execute("SELECT payload FROM events WHERE id = ?", (row[0],)).fetchone()[0])

    def _creator(self, store: Store, kind: str, rid: str) -> str:
        for event in store._events():
            payload = event["payload"]
            if event["type"] not in TYPES[kind]:
                continue
            ids = [payload.get("id")] if kind != "effort" or event["type"] == "effort.performed" else \
                [e.get("id") for e in payload.get("efforts", [])]
            if rid in ids:
                return event["id"]
        raise Unsupported(f"no event created {kind} {rid}")

    def deliver(self, store: Store, events: list[dict]) -> list[tuple[str, str]]:
        out = []
        for event in events:
            try:
                store.accept(event)
            except Conflict as exc:
                out.append(("rejected", exc.message))
            except ByobotError as exc:
                out.append(("rejected", exc.message))
            else:
                out.append(("accepted", ""))
        return out

    def ingest(self, store: Store, source: str, batch: Any, at: str) -> list[dict]:
        before = self._seq(store)
        with self._at(at), mock.patch("byobot.util.now", return_value=at):
            if source == "whoop":
                found = whoop.collect(_Pages(batch), at)
                if found["samples"]:
                    store.ingest(found["samples"])
                whoop.save_sessions(store, found["sessions"])
            elif source == "apple-health":
                store.ingest(batch)
                apple_health.save_sessions(store)
            elif source == "hume":
                store.ingest(batch)
            else:
                raise Unsupported(source)
        return self._new_events(store, before)

    def log_events(self, store: Store) -> list[dict]:
        return self._new_events(store, 0)

    def rebuild(self, store: Store) -> None:
        store.rebuild()

    # -- reads -------------------------------------------------------------

    def snapshot(self, store: Store) -> dict:
        out = empty()
        records = out["records"]
        for row in store.rows("SELECT * FROM activities WHERE source = 'ledger'"):
            fields = {k: v for k, v in json.loads(row["payload"]).items() if k != "id"}
            records[key("activity", row["id"])] = _record(fields, row)
        for kind in ("location", "plan", "planned_session", "session"):
            for row in store.rows(f"SELECT * FROM {TABLES[kind]}"):
                if row["id"].startswith(CONNECTOR_PREFIXES):
                    continue
                payload = json.loads(store.conn.execute("SELECT payload FROM events WHERE id = ?",
                                                        (row["event_id"],)).fetchone()[0])
                fields = {k: v for k, v in payload.items() if k not in ("id", "efforts")}
                records[key(kind, row["id"])] = _record(fields, row)
        for row in store.rows("SELECT * FROM performed_efforts"):
            fields = {k: v for k, v in store.effort_payload(row).items() if k != "id"}
            records[key("effort", row["id"])] = _record(fields, row)
        return out

    def occurrences(self, store: Store) -> list[dict]:
        out = []
        for row in store.rows("SELECT id, started_at, ended_at FROM performed_sessions ORDER BY started_at, id"):
            metrics = {m["metric"]: m["std_value"] for m in store.rows(
                "SELECT metric, std_value FROM metrics WHERE owner_type = 'performed_session' AND owner_id = ?",
                [row["id"]])}
            out.append(occurrence(row["started_at"], row["ended_at"] or row["started_at"], {
                "duration_s": metrics.get("duration"), "distance_m": metrics.get("distance"),
                "heart_rate": metrics.get("heart_rate")}))
        return sorted(out, key=canonical)

    def source_history(self, store: Store, source: str, source_id: str) -> list[dict]:
        rows = store.rows("SELECT payload FROM raw_records WHERE source = ? AND (source_id = ? OR source_id LIKE ?)"
                          " ORDER BY id", [source, source_id, f"%:{source_id}"])
        seen, out = set(), []
        for row in rows:
            payload = json.loads(row["payload"])
            item = payload["raw"] if source == "whoop" else payload
            if canonical(item) not in seen:
                seen.add(canonical(item))
                out.append(item)
        return out

    def log_digest(self, store: Store) -> list[str]:
        events = {e["id"]: e for e in store._events()}
        memo: dict[str, str] = {}

        def digest(event_id: str) -> str:
            if event_id not in memo:
                e = events[event_id]
                payload = dict(e["payload"])
                if e["type"] == "event.undone":
                    payload["event_id"] = digest(payload["event_id"]) if payload["event_id"] in events else None
                memo[event_id] = canonical({"type": e["type"], "actor": [e["actor"]["kind"], e["actor"]["id"]],
                                            "payload": payload})
            return memo[event_id]

        return sorted(digest(i) for i in events)

    def local_dates(self, store: Store) -> dict[str, str]:
        out = {key("session", r["id"]): review.local_date(r["started_at"])
               for r in store.rows("SELECT id, started_at FROM performed_sessions")}
        out.update({key("effort", r["id"]): review.local_date(r["performed_at"])
                    for r in store.rows("SELECT id, performed_at FROM performed_efforts")})
        return out

    def read(self, store: Store, surface: str, command: str, args: dict) -> Any:
        if surface != "cli":
            raise Unsupported(surface)
        ctx = commands.Context(actor={"kind": "human", "id": "me"}, path=store.path, _store=store)
        return commands.run(command, ctx, args)

    def export_log(self, store: Store) -> Any:
        raise Unsupported("export")

    def restore(self, name: str, directory: Any, log: Any) -> Any:
        raise Unsupported("restore")

    def upgrade(self, store: Store, version: int) -> None:
        raise Unsupported("upgrade")


def _record(fields: dict, row: dict) -> dict:
    return {"fields": fields, "by": f"{row['actor_kind']}:{row['actor_id']}", "on": row["device_id"]}
