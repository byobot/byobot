"""Workouts that connectors import as performed sessions, merged with the sessions you log.

A workout that overlaps in time with a session you logged adds its metrics to that session instead of
becoming a second one; your own values win. Where two connectors record the same workout, the one earlier
in RANK wins: a source byobot reads directly beats Apple Health, which often holds a copy of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

RANK = ("whoop", "apple-health")


def _rank(actor_id: str) -> int:
    return RANK.index(actor_id) if actor_id in RANK else len(RANK)


def utc(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> timedelta:
    return min(_at(a_end), _at(b_end)) - max(_at(a_start), _at(b_start))


def versions(store, session_id: str) -> list[dict]:
    """The session.performed events that still stand for one session, in the order they apply."""
    rows = store.rows("SELECT id, actor_kind, actor_id, payload FROM events WHERE type = 'session.performed'"
                      " AND json_extract(payload, '$.id') = ? AND id NOT IN (SELECT event_id FROM undone_events)"
                      " ORDER BY ts, device_id, id", [session_id])
    return [{**r, "payload": json.loads(r["payload"])} for r in rows]


def _yours(version: dict) -> bool:
    return version["actor_kind"] != "connector"


class _Import:
    def __init__(self, store, actor: dict):
        self.store, self.actor = store, actor
        self.prefix = f"{actor['id']}:"
        self.rank = _rank(actor["id"])

    def mine(self, version: dict) -> bool:
        return (version["actor_kind"], version["actor_id"]) == (self.actor["kind"], self.actor["id"])

    def undo(self, version: dict, reason: str) -> None:
        self.store.append("event.undone", {"event_id": version["id"], "reason": reason}, self.actor)

    def windows(self, lo: str, hi: str) -> dict[str, dict]:
        """Sessions that started in [lo, hi), with their time windows and the versions that stand."""
        out = {}
        for s in self.store.rows("SELECT id, started_at, ended_at, (SELECT max(performed_at) FROM performed_efforts"
                                 " WHERE session_id = s.id) AS last_set FROM performed_sessions s"
                                 " WHERE started_at >= ? AND started_at < ? AND id NOT LIKE ?",
                                 [lo, hi, self.prefix + "%"]):
            end = s["ended_at"] or s["last_set"] or s["started_at"]
            out[s["id"]] = {"start": s["started_at"], "end": end, "versions": versions(self.store, s["id"])}
        return out


def save_sessions(store, sessions: list[dict], actor: dict, complete: bool = False) -> dict:
    """Write a connector's workouts as performed sessions.

    Your edits and undos stand until the connector changes the workout. With `complete`, `sessions` is
    everything the connector has, so a session it imported earlier and no longer lists is undone.
    """
    imp = _Import(store, actor)
    fetched = {s["id"]: s for s in sessions}
    # A session you logged and then undid takes the connector's metrics with it.
    for r in store.rows("SELECT id FROM performed_sessions WHERE actor_kind = ? AND actor_id = ? AND id NOT LIKE ?",
                        [actor["kind"], actor["id"], imp.prefix + "%"]):
        found = versions(store, r["id"])
        if all(imp.mine(v) for v in found):
            for v in found:
                imp.undo(v, "the session was undone")
    # Workouts already imported as sessions of their own, so earlier duplicates merge too.
    workouts = {}
    for r in store.rows("SELECT id FROM performed_sessions WHERE actor_kind = ? AND actor_id = ? AND id LIKE ?",
                        [actor["kind"], actor["id"], imp.prefix + "%"]):
        found = versions(store, r["id"])
        if complete and r["id"] not in fetched:
            for v in found:
                if imp.mine(v):
                    imp.undo(v, f"{actor['id']} no longer has this workout")
        elif found:
            workouts[r["id"]] = found[-1]["payload"]
    workouts.update(fetched)
    counts = {"added": 0, "updated": 0, "unchanged": 0, "merged": 0}
    if not workouts:
        return counts
    lo = utc((min(_at(w["started_at"]) for w in workouts.values()) - timedelta(days=1)).isoformat())
    hi = max(w["ended_at"] for w in workouts.values())
    others = imp.windows(lo, hi)

    # Sessions a connector ranked above this one holds: its own, or yours with its metrics.
    held = {sid: s for sid, s in others.items()
            if any(not _yours(v) and _rank(v["actor_id"]) < imp.rank for v in s["versions"])}
    # Sessions only a connector ranked below this one imported.
    below = {sid: s for sid, s in others.items() if s["versions"]
             and all(not _yours(v) and _rank(v["actor_id"]) > imp.rank for v in s["versions"])}
    logged = {sid: s for sid, s in others.items() if sid not in held and any(_yours(v) for v in s["versions"])
              # Sessions logged with their efforts inline are left alone, so merging never rewrites your sets.
              and not [v for v in s["versions"] if _yours(v)][-1]["payload"].get("efforts")}
    for s in held.values():
        for v in s["versions"]:
            if imp.mine(v):
                imp.undo(v, "another source has this workout")

    covered = set()
    for wid, w in workouts.items():
        for sid, s in held.items():
            if overlap(w["started_at"], w["ended_at"], s["start"], s["end"]) > timedelta(0):
                covered.add(wid)
        if wid in covered:
            continue
        for sid, s in below.items():
            if overlap(w["started_at"], w["ended_at"], s["start"], s["end"]) > timedelta(0):
                for v in s["versions"]:
                    imp.undo(v, f"replaced by {actor['id']}'s workout")
                s["versions"] = []
    if covered:
        counts["covered"] = 0
    for wid in covered:
        counts["covered"] += 1
        for v in versions(store, wid):
            if imp.mine(v):
                imp.undo(v, "another source has this workout")
        del workouts[wid]

    # One workout per logged session, the most overlapping first.
    pairs = sorted(((overlap(w["started_at"], w["ended_at"], s["start"], s["end"]), wid, sid)
                    for wid, w in workouts.items() for sid, s in logged.items()), reverse=True)
    match: dict[str, str] = {}
    for gap, wid, sid in pairs:
        if gap > timedelta(0) and wid not in match and sid not in match.values():
            match[wid] = sid

    for wid, workout in workouts.items():
        if wid in match:
            sid = match[wid]
            found = logged[sid]["versions"]
            yours = [v for v in found if _yours(v)][-1]["payload"]
            last = max((i for i, v in enumerate(found) if imp.mine(v)), default=0)
            given = found[last]["payload"].get("metrics", {}) if imp.mine(found[last]) else {}
            had = next((v["payload"].get("metrics", {}) for v in reversed(found[:last]) if _yours(v)), {})
            # A value the connector gave last time, carried along in a later save of yours, is still its own.
            own = {k: m for k, m in yours.get("metrics", {}).items() if not (given.get(k) == m and had.get(k) != m)}
            merged = {**yours, "metrics": {**workout["metrics"], **own}}
            if found[-1]["payload"] == merged:
                counts["unchanged"] += 1
            else:
                store.append("session.performed", merged, actor)
                counts["merged"] += 1
            for v in versions(store, wid):
                if imp.mine(v):
                    imp.undo(v, f"merged into session {sid}")
        elif wid in fetched:
            last = store.rows("SELECT id, payload FROM events WHERE type = 'session.performed' AND actor_kind = ?"
                              " AND actor_id = ? AND json_extract(payload, '$.id') = ? ORDER BY seq DESC LIMIT 1",
                              [actor["kind"], actor["id"], wid])
            # An import's own undo (a merge or a replacement since undone) doesn't count as yours.
            retired = last and store.rows(
                "SELECT 1 FROM undone_events u JOIN events e ON e.id = u.undo_event_id"
                " WHERE u.event_id = ? AND e.actor_kind = 'connector'", [last[0]["id"]])
            if last and json.loads(last[0]["payload"]) == workout and not retired:
                counts["unchanged"] += 1
                continue
            store.append("session.performed", workout, actor)
            counts["updated" if last and not retired else "added"] += 1
    return counts
