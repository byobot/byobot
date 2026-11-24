"""Prompts the MCP server offers, so any agent can coach the same way."""

from __future__ import annotations

from datetime import date

WEEKLY_PLAN = """\
You are my strength coach, working through byobot. Today is {today}. Plan my training for the next 7 days.
{given}
1. Read before you ask. Call `review`, then `list` with kind `locations` and kind `plans`. Ask me only what you
   still need, such as my goal, which days I can train, where and for how long, and anything to work around.
2. Pick exercises with `list` kind `activities`, filtered by `muscle` or `equipment`, with detail `brief`. Use
   only ids that exist. Balance weekly sets per muscle from the review, and keep fatigue in mind. Weigh the
   review's recovery, sleep and imported workouts: ease off after poor recovery or a hard cardio day.
3. Progress each exercise from its recent sets at the same location_id; loads don't carry over between gyms.
   With no history there, prescribe reps and rir and leave out load.
4. Show me the week in a short table and wait for my yes before you write anything.
5. Write it with `log`: one plan.defined for the week, then one session.planned per training day with plan_id,
   scheduled_on, location_id and blocks. A block repeats its efforts for `rounds`, with rest in its metrics,
   for example {{"rounds": 3, "metrics": {{"rest": {{"value": 120, "unit": "s"}}}}, "efforts": [{{"activity_id":
   "barbell_back_squat", "metrics": {{"reps": {{"min": 6, "max": 8}}, "load": {{"value": 100, "unit": "kg"}},
   "rir": {{"value": 2}}}}}}]}}. Planning a session again with the same id replaces it; `undo` removes one.
6. Leave out sessions already planned in `upcoming` unless I ask you to change them.
7. When it's written, tell me to run `byobot serve` and open the app to get the week on my phone.
"""

TEMPLATES = {"weekly_plan": WEEKLY_PLAN}

PROMPTS = {
    "weekly_plan": {
        "title": "Plan my week",
        "description": "Review recent training and write next week's sessions, which the app picks up on sync.",
        "arguments": [
            {"name": "goal", "description": "What the training is for, such as strength or a race."},
            {"name": "days", "description": "Which days you can train, and for how long."},
            {"name": "notes", "description": "Anything else, such as an injury or a trip."},
        ],
    },
}


def render(name: str, arguments: dict) -> str:
    given = "".join(f"My {k}: {v}\n" for k, v in arguments.items() if v and k in ("goal", "days", "notes"))
    return TEMPLATES[name].format(today=date.today().isoformat(), given=given)
