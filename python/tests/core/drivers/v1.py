"""Schema 1's core store behind the core driver.

Intents are design-neutral (`performed_at`, `session_id`, short ids such as `e1`); this driver translates
them into v1 payloads (`time`, `links`, UUIDs) and translates reads back, so the same scenarios and the
reference model judge both stores. Event ids encode the replica's name, so ties at equal clocks break
the way the model breaks them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from byobot.core import commands
from byobot.core.contract import SLUG_KINDS, Contract, instant
from byobot.core.store import Store

from ..driver import Unsupported, parse
from ..model import AUTHORED, NEW_IN
from ..snapshot import canonical, empty, key

NAMESPACE = uuid.UUID("6f1d3c52-8a27-4f0e-9b61-2d7c0e5a9f14")
LOGS = {
    "define_activity": "activity", "define_location": "location", "define_plan": "plan",
    "plan_session": "planned_session", "log_session": "session", "log_effort": "effort",
}
READ_AS = {"meal": "effort"}  # a newer install's kind, written by this driver from an effort
SELF = "self_reported"


@dataclass
class Replica:
    name: str
    store: Store
    at: list = field(default_factory=lambda: [None])


class V1:
    name = "v1"

    def __init__(self):
        self.intent_ids: dict[str, str] = {}  # stored id -> intent id
        self.part_owner: dict[str, str] = {}  # planned effort id -> planned session intent id

    # -- ids -----------------------------------------------------------------

    def rid(self, kind: str, intent_id: str) -> str:
        if kind in SLUG_KINDS:
            return intent_id
        stored = str(uuid.uuid5(NAMESPACE, f"{kind}:{intent_id}"))
        self.intent_ids[stored] = intent_id
        return stored

    def back(self, stored: str) -> str:
        return self.intent_ids.get(stored, stored)

    def ref(self, kind: str, intent_id: str | None, **extra) -> dict | None:
        return None if intent_id is None else {"kind": kind, "id": self.rid(kind, intent_id), **extra}

    # -- lifecycle ---------------------------------------------------------

    def open(self, name: str, directory: Path) -> Replica:
        prefix = name.encode().ljust(10, b"\0")
        if len(prefix) > 10:
            raise ValueError(f"replica name too long for the id scheme: {name}")
        counter = iter(range(1, 1 << 48))

        def new_id() -> str:
            return str(uuid.UUID(bytes=prefix + next(counter).to_bytes(6, "big")))

        rep = Replica(name, None)
        rep.store = Store(Path(directory) / f"{name}.v1.db", device_id=name, new_id=new_id,
                          wall=lambda: parse(rep.at[0]) if rep.at[0] else parse("2026-03-02T09:00:00Z"))
        return rep

    def close(self, rep: Replica) -> None:
        rep.store.close()

    # -- translation ---------------------------------------------------------

    def to_v1(self, kind: str, fields: dict) -> dict:
        p = dict(fields)
        links = {}
        if kind in ("session", "effort", "meal"):
            start = p.pop("started_at" if kind == "session" else "performed_at", None)
            end = p.pop("ended_at", None)
            p["time"] = {"start": start, **({"end": end} if end else {})}
            p.setdefault("modality", SELF)
        if kind in ("planned_session", "session") and "plan_id" in p:
            links["part_of"] = self.ref("plan", p.pop("plan_id"))
        if kind == "session" and "planned_session_id" in p:
            links["answers"] = self.ref("planned_session", p.pop("planned_session_id"))
        if kind in ("effort", "meal"):
            if "session_id" in p:
                links["part_of"] = self.ref("session", p.pop("session_id"))
            if "planned_effort_id" in p:
                part = p.pop("planned_effort_id")
                owner = self.part_owner.get(part)
                if owner is None:
                    raise Unsupported(f"planned effort {part} of an unknown planned session")
                links["answers"] = self.ref("planned_session", owner, part=part)
        if kind == "planned_session":
            for block in p.get("blocks", []):
                for e in block.get("efforts", []):
                    if "id" in e:
                        self.part_owner[e["id"]] = self.back(p.get("_id", ""))
        p.pop("_id", None)
        if links:
            p["links"] = links
        return p

    def from_v1(self, kind: str, rid: str, payload: dict) -> dict:
        kind = READ_AS.get(kind, kind)
        p = dict(payload)
        links = p.pop("links", {})
        if kind in ("session", "effort"):
            t = p.pop("time")
            p["started_at" if kind == "session" else "performed_at"] = t["start"]
            if "end" in t:
                p["ended_at"] = t["end"]
            if p.get("modality") == SELF:
                p.pop("modality")
        if "part_of" in links:
            p[{"plan": "plan_id", "session": "session_id"}[links["part_of"]["kind"]]] = self.back(links["part_of"]["id"])
        if "answers" in links:
            if kind == "effort":
                p["planned_effort_id"] = links["answers"]["part"]
            else:
                p["planned_session_id"] = self.back(links["answers"]["id"])
        if kind == "planned_session":
            minted = f"{rid}."
            p["blocks"] = [{**b, "id": _unmint(b["id"], minted, self.back(rid)), "efforts": [
                {**e, "id": _unmint(e["id"], minted, self.back(rid))} for e in b["efforts"]]} for b in p["blocks"]]
        return p

    # -- writes ------------------------------------------------------------

    def author(self, rep: Replica, intent: dict, at: str, actor: dict) -> list[dict]:
        rep.at[0] = at
        store, name = rep.store, intent["intent"]
        if name in LOGS:
            return commands.log(store, actor, self._records(LOGS[name], intent["record"]))
        if name == "replan_session":
            record = intent["record"]
            rid = self.rid("planned_session", record["id"])
            payload = self.to_v1("planned_session", {**{k: v for k, v in record.items() if k != "id"},
                                                     "_id": rid})
            return [commands.replan(store, actor, rid, payload)]
        if name == "edit":
            kind, intent_id = intent["record"]
            rid = self.rid(kind, intent_id)
            found = store.get(kind, rid)
            if found is None:
                return [commands.edit(store, actor, kind, rid, {})]  # raises NotFound
            current = self.from_v1(kind, rid, found["payload"])
            wanted = {k: v for k, v in {**current, **intent["fields"]}.items() if v is not None}
            if kind == "planned_session":
                wanted["_id"] = rid
            return [commands.edit(store, actor, kind, rid, _diff(found["payload"], self.to_v1(kind, wanted)))]
        if name == "remove":
            kind, intent_id = intent["record"]
            return [commands.remove(store, actor, kind, self.rid(kind, intent_id))]
        if name == "undo":
            return commands.undo(store, actor, intent["event"]["id"])
        if name == "resolve":
            return [self._resolve(store, actor, *intent["record"], intent["choice"])]
        raise Unsupported(f"{name} comes with a later core step")

    def _records(self, kind: str, record: dict) -> list[dict]:
        rid = self.rid(kind, record["id"])
        fields = {k: v for k, v in record.items() if k not in ("id", "efforts")}
        if kind == "planned_session":
            fields["_id"] = rid
        out = [{"kind": kind, "id": rid, "payload": self.to_v1(kind, fields)}]
        for e in record.get("efforts", []) if kind == "session" else []:
            effort = {"performed_at": record["started_at"], **e, "session_id": record["id"]}
            out.append({"kind": "effort", "id": self.rid("effort", e["id"]),
                        "payload": self.to_v1("effort", {k: v for k, v in effort.items() if k != "id"})})
        return out

    def _resolve(self, store: Store, actor: dict, kind: str, intent_id: str, choice: Any) -> dict:
        rid = self.rid(kind, intent_id)
        found = store.conflicts(kind, rid)
        if not found:
            return commands.resolve(store, actor, kind, rid, "remove")  # raises NotFound
        versions = {}
        for v in found[0]["versions"]:
            versions.setdefault(canonical(self._version(v)), v["id"])
        ordered = [versions[k] for k in sorted(versions)]
        if choice == "remove":
            return commands.resolve(store, actor, kind, rid, "remove")
        if isinstance(choice, dict):
            if choice.get("_removed"):
                return commands.resolve(store, actor, kind, rid, "remove")
            return commands.resolve(store, actor, kind, rid, self.to_v1(kind, choice))
        return commands.resolve(store, actor, kind, rid, ordered[choice])

    def _version(self, event: dict) -> dict:
        if event["op"] != "put":
            return {"_removed": True}
        return self.from_v1(event["record"]["kind"], event["record"]["id"], event["payload"])

    def deliver(self, rep: Replica, events: list[dict]) -> list[tuple[str, str]]:
        return rep.store.accept_many(events)

    def ingest(self, rep: Replica, source: str, batch: Any, at: str) -> list[dict]:
        raise Unsupported("source records come with C4")

    def log_events(self, rep: Replica) -> list[dict]:
        return rep.store.events()

    def rebuild(self, rep: Replica) -> None:
        rep.store.rebuild()

    # -- reads -------------------------------------------------------------

    def _shown(self, kind: str, rep: Replica) -> bool:
        return kind in AUTHORED or (kind in NEW_IN and rep.store.contract.version(kind) is not None)

    def snapshot(self, rep: Replica) -> dict:
        out = empty()
        store = rep.store
        for r in store.records():
            if self._shown(r["kind"], rep):
                e = r["event"]
                out["records"][key(r["kind"], self.back(r["id"]))] = {
                    "fields": self.from_v1(r["kind"], r["id"], r["payload"]),
                    "by": f"{e['actor']['kind']}:{e['actor']['id']}", "on": e["device_id"]}
        for c in store.conflicts():
            if self._shown(c["kind"], rep):
                versions = {canonical(self._version(v)): self._version(v) for v in c["versions"]}
                out["conflicts"][key(c["kind"], self.back(c["id"]))] = list(versions.values())
        out["erased"] = sorted(key(k, self.back(i)) for k, i in store.erased() if k in AUTHORED)
        return out

    def occurrences(self, rep: Replica) -> list[dict]:
        raise Unsupported("merged workouts come with C4")

    def source_history(self, rep: Replica, source: str, source_id: str) -> list[dict]:
        raise Unsupported("source records come with C4")

    def log_digest(self, rep: Replica) -> list[str]:
        return sorted(canonical([e["record"]["kind"], self.back(e["record"]["id"]), e["op"],
                                 f"{e['actor']['kind']}:{e['actor']['id']}", e["payload"]])
                      for e in rep.store.events())

    def local_dates(self, rep: Replica) -> dict[str, str]:
        out = {}
        for kind, column, table in (("session", "started_at", "sessions"), ("effort", "performed_at", "efforts")):
            for rid, at in rep.store.conn.execute(f"SELECT id, {column} FROM {table}"):
                out[key(kind, self.back(rid))] = instant(at).date().isoformat()
        return out

    def read(self, rep: Replica, surface: str, command: str, args: dict) -> Any:
        raise Unsupported("surfaces move to schema 1 with the cutover")

    def export_log(self, rep: Replica) -> Any:
        raise Unsupported("export comes with C7")

    def restore(self, name: str, directory: Any, log: Any) -> Any:
        raise Unsupported("restore comes with C7")

    def upgrade(self, rep: Replica, version: int) -> None:
        rep.store.use(Contract.shipped().newer(
            version, {k: {"category": "act", "version": v, "description": k} for k, v in NEW_IN.items()
                      if v <= version}))

    def newer(self, events: list[dict], variant: str) -> list[dict]:
        out = []
        for e in events:
            e = {**e, "id": e["id"][:-4] + {"unknown_kind": "aaaa", "newer_version": "bbbb"}.get(variant, "cccc")}
            if e["op"] == "put":
                e["kind_version"] = 2
            if variant == "unknown_kind":
                e["record"] = {**e["record"], "kind": "meal"}
            elif variant == "extra_field":
                e["payload"] = {**e["payload"], "x_new_field": 1}
            out.append(e)
        return out


def _unmint(part: str, minted: str, intent_id: str) -> str:
    return f"{intent_id}.{part[len(minted):]}" if part.startswith(minted) else part


def _diff(old: Any, new: Any) -> Any:
    """The JSON merge patch that turns `old` into `new`."""
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return new
    patch = {k: None for k in old if k not in new}
    for k, v in new.items():
        if k not in old or old[k] != v:
            patch[k] = _diff(old.get(k), v) if isinstance(old.get(k), dict) and isinstance(v, dict) else v
    return patch
