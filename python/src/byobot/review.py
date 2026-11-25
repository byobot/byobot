"""A compact digest of recent training and recovery, so an agent can coach without writing SQL."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from byobot.store import Store
from byobot.views import ACTIVITY, BODY, DAILY_TELEMETRY, MOBILITY, RECOVERY, VITALS


def local_date(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()


def readable_seconds(seconds: float) -> str:
    """45 s, 1 min 30 s, 38 min, 1 h 12 min. Seconds only show under ten minutes."""
    if seconds < 60:
        return f"{round(seconds)} s"
    if seconds < 600:
        m, s = divmod(round(seconds), 60)
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m} min" if h and m else f"{h} h" if h else f"{m} min"


def _compact(metrics: dict) -> dict:
    out = {}
    for name, m in metrics.items():
        value = m.get("value", m.get("value_text"))
        if value is None and ("min" in m or "max" in m):
            value = f"{m.get('min', '')}-{m.get('max', '')}"
        if m.get("unit") == "s" and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = readable_seconds(value)
        else:
            out[name] = f"{value} {m['unit']}" if m.get("unit") else value
    return out


def _daily(store: Store, first: str, last: str) -> dict[str, dict]:
    """Each day's values by column, from the daily view."""
    rows = store.rows("SELECT * FROM daily WHERE date >= ? AND date <= ?", [first, last])
    return {r["date"]: {k: r[k] for k in DAILY_TELEMETRY if r[k] is not None} for r in rows}


def _by_day(days: dict[str, dict], keys: set[str], first: str, last: str) -> list[dict]:
    out = []
    for d in sorted(days):
        values = {k: round(v, 1) for k, v in sorted(days[d].items()) if k in keys}
        if values and first <= d <= last:
            out.append({"date": d, **values})
    return out


def review(store: Store, weeks: int, until: str | None = None) -> dict:
    end = date.fromisoformat(until) if until else date.today()
    start = end - timedelta(weeks=weeks)
    first, last = start.isoformat(), end.isoformat()
    # A day's margin on both sides covers time zones; local dates decide below.
    lo, hi = (start - timedelta(days=1)).isoformat(), (end + timedelta(days=2)).isoformat()

    targets = {r["id"]: json.loads(r["targets"]) if r["targets"] else {}
               for r in store.rows("SELECT id, targets FROM activities")}
    efforts = store.rows(
        "SELECT e.id, e.session_id, e.activity_id, e.performed_at, e.side,"
        " coalesce(e.location_id, s.location_id) AS location_id"
        " FROM performed_efforts e LEFT JOIN performed_sessions s ON s.id = e.session_id"
        " WHERE e.performed_at >= ? AND e.performed_at < ? ORDER BY e.performed_at", [lo, hi])
    efforts = [e for e in efforts if first <= local_date(e["performed_at"]) <= last]

    occasions: dict[tuple, dict] = {}
    weekly: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: {"main": 0, "synergist": 0}))
    for e in efforts:
        day = local_date(e["performed_at"])
        sets = occasions.setdefault((e["activity_id"], e["location_id"]), {}).setdefault(day, [])
        entry = _compact(store.metrics_of("performed_effort", e["id"]))
        if e["side"]:
            entry["side"] = e["side"]
        sets.append(entry)
        week = (date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())).isoformat()
        for role, key in (("main", "main"), ("synergists", "synergist")):
            for muscle in targets.get(e["activity_id"], {}).get(role, []):
                weekly[week][muscle][key] += 1

    exercises = []
    for (activity_id, location_id), days in sorted(occasions.items(), key=lambda kv: -len(kv[1])):
        recent = sorted(days)[-2:]
        exercises.append({"activity_id": activity_id, "location_id": location_id, "days_done": len(days),
                          "recent": [{"date": d, "sets": days[d]} for d in recent]})

    sessions = []
    for s in store.rows("SELECT * FROM performed_sessions WHERE started_at >= ? AND started_at < ?"
                        " ORDER BY started_at", [lo, hi]):
        entry = {"date": local_date(s["started_at"]), "name": s["name"], "location_id": s["location_id"],
                 "planned_session_id": s["planned_session_id"],
                 "sets": store.rows("SELECT count(*) AS n FROM performed_efforts WHERE session_id = ?", [s["id"]])[0]["n"]}
        totals = _compact(store.metrics_of("performed_session", s["id"]))
        if totals:
            entry["metrics"] = totals
        if s["actor_kind"] == "connector":
            logged = store.rows("SELECT 1 FROM events WHERE type = 'session.performed' AND actor_kind != 'connector'"
                                " AND json_extract(payload, '$.id') = ? AND id NOT IN"
                                " (SELECT event_id FROM undone_events) LIMIT 1", [s["id"]])
            entry["metrics_from" if logged else "imported_by"] = s["actor_id"]
        sessions.append(entry)
    sessions = [s for s in sessions if first <= s["date"] <= last]

    done = {r["planned_session_id"] for r in store.rows(
        "SELECT DISTINCT planned_session_id FROM performed_sessions WHERE planned_session_id IS NOT NULL")}
    done |= {r["planned_session_id"] for r in store.rows(
        "SELECT DISTINCT pe.planned_session_id FROM performed_efforts pf"
        " JOIN planned_efforts pe ON pe.id = pf.planned_effort_id")}
    planned = store.rows("SELECT id, scheduled_on, name, plan_id, location_id FROM planned_sessions"
                         " WHERE scheduled_on >= ? ORDER BY scheduled_on", [first])
    past = [p for p in planned if p["scheduled_on"] < last]
    daily = _daily(store, first, last)
    return {
        "from": first,
        "to": last,
        "sessions": sessions,
        "plan_adherence": {
            "planned": len(past),
            "done": sum(p["id"] in done for p in past),
            "missed": [{k: p[k] for k in ("id", "scheduled_on", "name")} for p in past if p["id"] not in done],
        },
        "exercises": exercises,
        "weekly_sets_per_muscle": {w: dict(sorted(m.items())) for w, m in sorted(weekly.items())},
        "recovery": _by_day(daily, {*RECOVERY.values(), "asleep_h"}, first, last),
        "body": _by_day(daily, set(BODY.values()), first, last),
        "activity": _by_day(daily, set(ACTIVITY.values()), first, last),
        "mobility": _by_day(daily, set(MOBILITY.values()), first, last),
        "vitals": _by_day(daily, set(VITALS.values()), first, last),
        "upcoming": [p for p in planned if p["scheduled_on"] >= last],
    }
