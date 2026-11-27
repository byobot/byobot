"""The reference model: schema 1's decided semantics (core-decision.md) in plain Python, without SQLite.

It deliberately shares no code with the store, so a bug can't hide in both. One replica holds a set of
writes and computes everything an agent can read from them:

* Every write changes one record and carries what its author had seen of that record (`prior`) and a
  hybrid logical clock (`hlc`) above everything the author held, so a write made after seeing another
  sorts after it whatever the wall clocks say.
* A record's heads are its writes no other held write had seen. More than one distinct head is a
  visible conflict; reads show the head with the highest hlc meanwhile. Identical heads don't conflict.
* Undo is a command: it writes the record's previous content back (a retract if the undone write
  created it), and only while the undone write is the record's one head. Remove is a retract. Resolving is a write that has seen every head.
* Logging an id that's already taken is refused, not overwritten.
* Erase wins over everything: the record and every write to it, earlier or later, are gone.
* Source revisions are writes too, keyed by their content, so the same revision imported twice is one
  write. Every revision is kept; the latest is current.
* Workouts seen by several sources are one occurrence: your values first, then sources by rank, each
  field from the best item that has it, with its provenance. Corrections are overlays over a source.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .driver import Unsupported
from .snapshot import canonical, empty, key, num, occurrence

AUTHORED = ("activity", "location", "plan", "planned_session", "session", "effort")
NEW_IN = {"meal": 2}  # kinds a newer install writes, by the version that introduced them
LOGS = {
    "define_activity": "activity", "define_location": "location", "define_plan": "plan",
    "plan_session": "planned_session", "log_session": "session", "log_effort": "effort",
}
RANK = ("you", "whoop", "apple-health")
SAME_OCCURRENCE = 0.5  # share of the shorter workout two items must overlap to be one occurrence
WORKOUT_TYPES = {("whoop", "workout"), ("apple-health", "HKWorkoutTypeIdentifier")}
UNITS = {"s": 1, "min": 60, "h": 3600, "m": 1, "km": 1000, "mi": 1609.344}
MASS = {"kg": 1, "g": 0.001, "lb": 0.45359237}


class Refused(Exception):
    """The model's command layer refuses the intent (an id already taken, a record that isn't there)."""


@dataclass(frozen=True)
class Write:
    id: str
    replica: str
    at: str
    actor: str
    kind: str
    rid: str
    op: str  # put | retract | erase | revision | deletion
    content: Any
    prior: frozenset = frozenset()  # writes to this record the author held
    hlc: tuple = (0, 0)
    reverts: str | None = None
    version: int = 1

    @property
    def record(self) -> str:
        return key(self.kind, self.rid)

    def order(self) -> tuple:
        return (*self.hlc, self.replica, self.id)


def millis(ts: str) -> int:
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)


def _strip(content: dict) -> dict:
    return {k: v for k, v in content.items() if v is not None}


@dataclass
class ModelReplica:
    name: str
    writes: dict[str, Write] = field(default_factory=dict)
    by_record: dict[str, list[str]] = field(default_factory=dict)
    floor: tuple = (0, 0)
    authored: int = 0
    gone: set = field(default_factory=set)
    version: int = 1

    # -- the logbook --------------------------------------------------------

    def deliver(self, writes: list[Write]) -> list[tuple[str, str]]:
        out = []
        for w in writes:
            if w.id in self.writes:
                same = _body(self.writes[w.id]) == _body(w)
                out.append(("duplicate", "") if same else ("rejected", "id reused with other content"))
                continue
            self.writes[w.id] = w
            self.by_record.setdefault(w.record, []).append(w.id)
            self.floor = max(self.floor, w.hlc)
            if w.op == "erase":
                self.gone.add(w.record)
            out.append(("accepted", "") if self.understands(w) else ("stored_unprojected", ""))
        return out

    def understands(self, w: Write) -> bool:
        return w.version <= self.version and NEW_IN.get(w.kind, 1) <= self.version

    def _clock(self, at: str) -> tuple:
        wall = millis(at)
        return (wall, 0) if wall > self.floor[0] else (self.floor[0], self.floor[1] + 1)

    def _write(self, at: str, actor: str, kind: str, rid: str, op: str, content: Any, reverts: str | None = None,
               wid: str | None = None, read: bool = False) -> Write:
        """`read`: the write was made from the record's current version, and names only that one."""
        record = key(kind, rid)
        cur = self.current(record) if read else None
        if read and self._unread(record):
            raise Refused(f"{record} holds a version written by a newer byobot")
        prior = frozenset({cur.id}) if cur else frozenset(self.by_record.get(record, ()))
        if wid is None:
            self.authored += 1
            wid = f"{self.name}:{self.authored}"
        w = Write(wid, self.name, at, actor, kind, rid, op, content, prior, self._clock(at), reverts)
        self.deliver([w])
        return w

    # -- records -----------------------------------------------------------

    def heads(self, record: str, among: set[str] | None = None) -> list[Write]:
        if record in self.gone:
            return []
        ids = [i for i in self.by_record.get(record, ()) if among is None or i in among]
        versions = [self.writes[i] for i in ids
                    if self.writes[i].op in ("put", "retract") and self.understands(self.writes[i])]
        return sorted((v for v in versions if v.id not in self._ancestors(versions)), key=Write.order)

    def _ancestors(self, versions: list[Write]) -> set[str]:
        """Every write the versions were made on top of, however far back, through writes not understood too."""
        seen: set[str] = set()
        stack = [p for v in versions for p in v.prior]
        while stack:
            i = stack.pop()
            if i not in seen:
                seen.add(i)
                stack.extend(self.writes[i].prior if i in self.writes else ())
        return seen

    def _unread(self, record: str) -> bool:
        versions = [self.writes[i] for i in self.by_record.get(record, ()) if self.writes[i].op in ("put", "retract")]
        superseded = set().union(*(v.prior for v in versions)) if versions else set()
        return any(not self.understands(v) for v in versions if v.id not in superseded) and record not in self.gone

    def current(self, record: str, among: set[str] | None = None) -> Write | None:
        heads = self.heads(record, among)
        return heads[-1] if heads else None

    def fields(self, record: str) -> dict | None:
        w = self.current(record)
        return w.content if w and w.op == "put" else None

    def snapshot(self) -> dict:
        out = empty()
        out["erased"] = sorted(r for r in self.gone if r.split("/")[0] in AUTHORED)
        for record in self.by_record:
            if record.split("/")[0] not in AUTHORED and NEW_IN.get(record.split("/")[0], 99) > self.version:
                continue
            heads = self.heads(record)
            if not heads:
                continue
            versions = {canonical(_version(h)): _version(h) for h in heads}
            if len(versions) > 1:
                out["conflicts"][record] = list(versions.values())
            w = heads[-1]
            if w.op == "put":
                out["records"][record] = {"fields": w.content, "by": w.actor, "on": w.replica}
        out["derived"] = {r: {"local_date": d, "metrics": _standard(self.fields(r).get("metrics", {}))}
                          for r, d in self.local_dates().items()}
        out["workouts"] = self.occurrences()
        return out

    # -- authoring: the command layer ------------------------------------------

    def author(self, intent: dict, at: str, actor: str) -> list[Write]:
        name = intent["intent"]
        if name in LOGS:
            return self._log(LOGS[name], intent["record"], at, actor)
        if name == "replan_session":
            return self._replan(intent["record"], at, actor)
        if name == "edit":
            kind, rid = intent["record"]
            current = self.fields(key(kind, rid))
            if current is None:
                raise Refused(f"no {kind} {rid}")
            return [self._write(at, actor, kind, rid, "put", _strip({**current, **intent["fields"]}), read=True)]
        if name == "remove":
            kind, rid = intent["record"]
            if self.fields(key(kind, rid)) is None:
                raise Refused(f"no {kind} {rid}")
            return [self._write(at, actor, kind, rid, "retract", None, read=True)]
        if name == "undo":
            return [self._undo(target, at, actor) for target in intent["events"]]
        if name == "resolve":
            return [self._resolve(intent, at, actor)]
        if name == "erase":
            kind, rid = intent["record"]
            return [self._write(at, actor, kind, rid, "erase", None)]
        if name in ("correct_import", "same_as"):
            return [self._overlay(intent, at, actor)]
        raise Refused(name)

    def _log(self, kind: str, record: dict, at: str, actor: str) -> list[Write]:
        parts = [(kind, record["id"], {k: v for k, v in record.items() if k not in ("id", "efforts")})]
        if kind == "session":
            for e in record.get("efforts", []):
                effort = {"performed_at": record["started_at"], **e, "session_id": record["id"]}
                parts.append(("effort", e["id"], {k: v for k, v in effort.items() if k != "id"}))
        for k, rid, _ in parts:
            if self.fields(key(k, rid)) is not None or key(k, rid) in self.gone:
                raise Refused(f"{k} {rid} already exists or was erased")
        return [self._write(at, actor, k, rid, "put", content, read=True) for k, rid, content in parts]

    def _replan(self, record: dict, at: str, actor: str) -> list[Write]:
        """Re-planning keeps the ids of blocks and planned efforts it leaves out, by position; a planned effort's
        only for the same activity."""
        old = self.fields(key("planned_session", record["id"])) or {"blocks": []}
        used = {e["id"] for b in old["blocks"] for e in b["efforts"]}
        blocks = []
        for bi, block in enumerate(record["blocks"]):
            was = old["blocks"][bi] if bi < len(old["blocks"]) else {"efforts": []}
            efforts = []
            for ei, e in enumerate(block["efforts"]):
                same = ei < len(was["efforts"]) and was["efforts"][ei].get("activity_id") == e.get("activity_id")
                if same:
                    rid = was["efforts"][ei]["id"]
                else:
                    rid, n = f"{record['id']}.{bi}.{ei}", 1
                    while rid in used:
                        n += 1
                        rid = f"{record['id']}.{bi}.{ei}.{n}"
                used.add(rid)
                efforts.append({"id": rid, **e})
            blocks.append({"id": was.get("id", f"{record['id']}.{bi}"), **block, "efforts": efforts})
        content = {k: v for k, v in record.items() if k != "id"} | {"blocks": blocks}
        return [self._write(at, actor, "planned_session", record["id"], "put", content, read=True)]

    def _undo(self, target: Write, at: str, actor: str) -> Write:
        if target.op not in ("put", "retract"):
            raise Refused(f"can't undo a {target.op}")
        if [h.id for h in self.heads(target.record)] != [target.id]:
            raise Refused(f"{target.record} changed after {target.id}")
        before = self.current(target.record, set(target.prior))
        if before is None or before.op == "retract":
            return self._write(at, actor, target.kind, target.rid, "retract", None, reverts=target.id, read=True)
        return self._write(at, actor, target.kind, target.rid, "put", before.content, reverts=target.id, read=True)

    def _resolve(self, intent: dict, at: str, actor: str) -> Write:
        kind, rid = intent["record"]
        heads = self.heads(key(kind, rid))
        versions = sorted({canonical(_version(h)): _version(h) for h in heads}.values(), key=canonical)
        if len(versions) < 2:
            raise Refused(f"{kind} {rid} has no conflict")
        choice = intent["choice"]
        if choice == "remove":
            return self._write(at, actor, kind, rid, "retract", None)
        picked = versions[choice] if isinstance(choice, int) else choice
        if picked.get("_removed"):
            return self._write(at, actor, kind, rid, "retract", None)
        return self._write(at, actor, kind, rid, "put", picked)

    def _overlay(self, intent: dict, at: str, actor: str) -> Write:
        target = f"{intent['source']}:workout:{intent['source_id']}"
        if intent["intent"] == "correct_import":
            rid = f"correct:{target}"
            current = self.fields(key("overlay", rid)) or {"target": target, "fields": {}}
            content = {"target": target, "fields": _strip({**current["fields"], **intent["fields"]})}
        else:
            rid = f"same_as:{target}"
            content = {"target": target, "session": intent["session"]}
        return self._write(at, actor, "overlay", rid, "put", content, read=True)

    # -- sources -------------------------------------------------------------

    def ingest(self, source: str, batch: Any, at: str) -> list[Write]:
        writes = []
        for type_, source_id, payload, revised_at in _source_records(source, batch):
            content = {"payload": payload, "revised_at": revised_at}
            wid = "src:" + hashlib.sha256(canonical([source, type_, source_id, content]).encode()).hexdigest()[:24]
            if wid not in self.writes:
                writes.append(self._write(at, f"connector:{source}", "source_record", f"{source}:{type_}:{source_id}",
                                          "revision", content, wid=wid))
        for type_, source_id in (batch.get("deleted", []) if isinstance(batch, dict) else []):
            rid = f"{source}:{type_}:{source_id}"
            wid = "src:" + hashlib.sha256(canonical([rid, "deleted"]).encode()).hexdigest()[:24]
            if wid not in self.writes:
                writes.append(self._write(at, f"connector:{source}", "source_record", rid, "deletion", None, wid=wid))
        return writes

    def revisions(self, record: str) -> list[Write]:
        if record in self.gone:
            return []
        found = [self.writes[i] for i in self.by_record.get(record, ()) if self.writes[i].op == "revision"]
        return sorted(found, key=lambda w: (*_instant(w.content["revised_at"]), w.hlc, canonical(w.content["payload"])))

    def source_current(self, record: str) -> dict | None:
        if any(self.writes[i].op == "deletion" for i in self.by_record.get(record, ())):
            return None
        revs = self.revisions(record)
        return revs[-1].content["payload"] if revs else None

    def source_history(self, source: str, type_: str, source_id: str) -> list[dict]:
        return [w.content["payload"] for w in self.revisions(key("source_record", f"{source}:{type_}:{source_id}"))]

    # -- merged reads ----------------------------------------------------------

    def occurrences(self) -> list[dict]:
        items = []
        for record in self.by_record:
            kind, rid = record.split("/", 1)
            if kind == "session" and (f := self.fields(record)) is not None:
                items.append(_session_item(rid, f))
            elif kind == "source_record":
                source, type_ = rid.split(":")[:2]
                payload = self.source_current(record)
                if payload is not None and (source, type_) in WORKOUT_TYPES:
                    items.append(_source_item(source, rid, payload))
        overlays = {f["target"]: f for r in self.by_record if r.startswith("overlay/")
                    if (f := self.fields(r)) is not None}
        for item in items:
            corrected = overlays.get(item["id"], {}).get("fields", {})
            item["metrics"].update({k: (float(v), "you") for k, v in corrected.items()})
        links = {f["target"]: f"session/{f['session']}" for f in overlays.values() if "session" in f}
        out = []
        for cluster in _clusters(items, links):
            cluster.sort(key=lambda i: (RANK.index(i["rank"]) if i["rank"] in RANK else len(RANK), i["start"], i["id"]))
            metrics, provenance = {}, {}
            for item in cluster:
                for name, (value, by) in item["metrics"].items():
                    if name not in metrics:
                        metrics[name], provenance[name] = value, by if by == "you" else item["rank"]
            o = occurrence(cluster[0]["start"], cluster[0]["end"], metrics)
            o["provenance"] = provenance
            out.append(o)
        return sorted(out, key=canonical)

    # -- other reads -----------------------------------------------------------

    def local_dates(self) -> dict[str, str]:
        out = {}
        for record in self.by_record:
            kind = record.split("/")[0]
            f = self.fields(record) if kind in ("session", "effort") else None
            if f is not None:
                at = f["started_at"] if kind == "session" else f["performed_at"]
                out[record] = datetime.fromisoformat(at.replace("Z", "+00:00")).date().isoformat()
        return out

    def log_digest(self) -> list[str]:
        return sorted(canonical([w.kind, w.rid, w.op, w.actor, w.content]) for w in self.writes.values())


def _body(w: Write) -> tuple:
    return (w.kind, w.rid, w.op, canonical(w.content), w.version)


def _version(w: Write) -> dict:
    return w.content if w.op == "put" else {"_removed": True}


def _standard(metrics: dict) -> dict:
    """Each metric's value in its code's standard unit, as reads compare it."""
    out = {}
    for code, m in metrics.items():
        if isinstance(m, dict) and isinstance(m.get("value"), (int, float)):
            unit = m.get("unit")
            out[code] = num(m["value"] * MASS.get(unit, UNITS.get(unit, 1)))
    return out


def _seconds(measure: dict | None) -> float | None:
    if not measure or not isinstance(measure.get("value"), (int, float)):
        return None
    return float(measure["value"]) * UNITS.get(measure.get("unit", "s"), 1)


def _session_item(rid: str, f: dict) -> dict:
    m = f.get("metrics", {})
    metrics = {"duration_s": _seconds(m.get("duration")), "distance_m": _seconds(m.get("distance")),
               "heart_rate": (m.get("heart_rate") or {}).get("value")}
    return {"id": f"session/{rid}", "rank": "you", "start": f["started_at"], "end": f.get("ended_at", f["started_at"]),
            "metrics": {k: (float(v), "you") for k, v in metrics.items() if v is not None}}


def _source_item(source: str, rid: str, p: dict) -> dict:
    if source == "whoop":
        start, end = p["start"], p["end"]
        score = (p.get("score") or {}) if p["score_state"] == "SCORED" else {}
        metrics = {"distance_m": score.get("distance_meter"), "heart_rate": score.get("average_heart_rate")}
    else:
        start, end = p["start"], p["end"]
        raw = p.get("raw") or {}
        metrics = {"distance_m": raw.get("distance_m"), "heart_rate": raw.get("heart_rate_avg_bpm")}
    metrics["duration_s"] = (millis(end) - millis(start)) / 1000
    return {"id": rid, "rank": source, "start": start, "end": end,
            "metrics": {k: (float(v), source) for k, v in metrics.items() if v is not None}}


def _overlap(a: dict, b: dict) -> bool:
    lo, hi = max(millis(a["start"]), millis(b["start"])), min(millis(a["end"]), millis(b["end"]))
    shorter = min(millis(a["end"]) - millis(a["start"]), millis(b["end"]) - millis(b["start"]))
    return hi - lo > 0 and hi - lo >= SAME_OCCURRENCE * shorter


def _clusters(items: list[dict], links: dict[str, str]) -> list[list[dict]]:
    items = sorted(items, key=lambda i: (millis(i["start"]), i["id"]))
    clusters: list[list[dict]] = []
    for item in items:
        home = None
        for c in clusters:
            linked = links.get(item["id"]) in {i["id"] for i in c} or any(links.get(i["id"]) == item["id"] for i in c)
            yours = item["rank"] == "you" and any(i["rank"] == "you" for i in c)
            if linked or (not yours and any(_overlap(item, i) for i in c)):
                home = c
                break
        if home is None:
            clusters.append([item])
        else:
            home.append(item)
    return clusters


def _source_records(source: str, batch: Any):
    """(type, source_id, payload, revised_at) for each record in a connector's wire batch."""
    if source == "whoop":
        for kind in ("sleep", "recovery", "cycle", "workout"):
            for r in batch.get(kind, []):
                yield kind, str(r["cycle_id"] if kind == "recovery" else r["id"]), r, r["updated_at"]
    else:
        for s in batch.get("samples", []) if isinstance(batch, dict) else batch:
            yield s["type"], s["source_id"], s, (s.get("raw") or {}).get("modified_at", "")


class Reference:
    """The reference model behind the core driver interface, so every scenario can run against it too."""

    name = "model"

    def open(self, name: str, directory: Any) -> ModelReplica:
        return ModelReplica(name)

    def close(self, replica: ModelReplica) -> None:
        pass

    def author(self, replica: ModelReplica, intent: dict, at: str, actor: dict) -> list[Write]:
        return replica.author(intent, at, f"{actor['kind']}:{actor['id']}")

    def deliver(self, replica: ModelReplica, events: list[Write]) -> list[tuple[str, str]]:
        return replica.deliver(events)

    def ingest(self, replica: ModelReplica, source: str, batch: Any, at: str) -> list[Write]:
        return replica.ingest(source, batch, at)

    def log_events(self, replica: ModelReplica) -> list[Write]:
        return list(replica.writes.values())

    def rebuild(self, replica: ModelReplica) -> None:
        pass

    def snapshot(self, replica: ModelReplica) -> dict:
        return replica.snapshot()

    def occurrences(self, replica: ModelReplica) -> list[dict]:
        return [{k: v for k, v in o.items() if k != "provenance"} for o in replica.occurrences()]

    def source_history(self, replica: ModelReplica, source: str, source_id: str) -> list[dict]:
        return [p for r in replica.by_record if r.startswith(f"source_record/{source}:") and r.endswith(f":{source_id}")
                for p in replica.source_history(*r.split("/", 1)[1].split(":", 2))]

    def log_digest(self, replica: ModelReplica) -> list[str]:
        return replica.log_digest()

    def local_dates(self, replica: ModelReplica) -> dict[str, str]:
        return replica.local_dates()

    def read(self, replica: ModelReplica, surface: str, command: str, args: dict) -> Any:
        raise Unsupported("the model has no surfaces")

    def export_log(self, replica: ModelReplica) -> list[Write]:
        return list(replica.writes.values())

    def restore(self, name: str, directory: Any, log: list[Write]) -> ModelReplica:
        replica = ModelReplica(name)
        replica.deliver(log)
        ours = [w.id.split(":", 1)[1] for w in log if w.id.startswith(f"{name}:")]
        replica.authored = max((int(i) for i in ours if i.isdigit()), default=0)  # the same device, restored
        return replica

    def upgrade(self, replica: ModelReplica, version: int) -> None:
        replica.version = version

    def newer(self, events: list[Write], variant: str) -> list[Write]:
        return [newer(w, variant) for w in events]



def newer(w: Write, variant: str) -> Write:
    """The write as an install one version newer would have made it."""
    if variant == "unknown_kind":
        return replace(w, id=f"{w.id}~meal", kind="meal", version=2)
    if variant == "newer_version":
        return replace(w, id=f"{w.id}~v2", version=2)
    return replace(w, id=f"{w.id}~extra", content={**w.content, "x_new_field": 1})


def _instant(at: str | None) -> tuple:
    """A revision time as a point in time; one without a time sorts first, then by when it was written."""
    try:
        parsed = datetime.fromisoformat((at or "").replace("Z", "+00:00"))
    except ValueError:
        return (False, 0.0)
    return (True, parsed.timestamp()) if parsed.tzinfo else (False, 0.0)
