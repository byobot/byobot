"""A compact digest of recent training, so an agent can coach without writing SQL."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from byobot.store import Store


def local_date(ts: str) -> str:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()


def _compact(metrics: dict) -> dict:
    out = {}
    for name, m in metrics.items():
        value = m.get("value", m.get("value_text"))
        if value is None and ("min" in m or "max" in m):
            value = f"{m.get('min', '')}-{m.get('max', '')}"
        out[name] = f"{value} {m['unit']}" if m.get("unit") else value
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

    sessions = [
        {"date": local_date(s["started_at"]), "name": s["name"], "location_id": s["location_id"],
         "planned_session_id": s["planned_session_id"],
         "sets": store.rows("SELECT count(*) AS n FROM performed_efforts WHERE session_id = ?", [s["id"]])[0]["n"]}
        for s in store.rows("SELECT * FROM performed_sessions WHERE started_at >= ? AND started_at < ?"
                            " ORDER BY started_at", [lo, hi])
    ]
    sessions = [s for s in sessions if first <= s["date"] <= last]

    done = {r["planned_session_id"] for r in store.rows(
        "SELECT DISTINCT planned_session_id FROM performed_sessions WHERE planned_session_id IS NOT NULL")}
    done |= {r["planned_session_id"] for r in store.rows(
        "SELECT DISTINCT pe.planned_session_id FROM performed_efforts pf"
        " JOIN planned_efforts pe ON pe.id = pf.planned_effort_id")}
    planned = store.rows("SELECT id, scheduled_on, name, plan_id, location_id FROM planned_sessions"
                         " WHERE scheduled_on >= ? ORDER BY scheduled_on", [first])
    past = [p for p in planned if p["scheduled_on"] < last]
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
        "upcoming": [p for p in planned if p["scheduled_on"] >= last],
    }
