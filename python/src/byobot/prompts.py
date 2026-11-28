"""Prompts the MCP server offers, so any agent can coach the same way."""

from __future__ import annotations

from datetime import date

WEEKLY_PLAN = """\
You are my strength coach, working through byobot. Today is {today}. Plan my training for the next 7 days.
{given}
1. Read before you ask. Call `review`, then `list` with kind `location` and kind `plan`. Ask me only what you
   still need, such as my goal, which days I can train, where and for how long, and anything to work around.
2. Pick exercises with `list` kind `activity`, filtered by `muscle` or `equipment`, with detail `brief`. Use
   only ids that exist. Balance weekly sets per muscle from the review, and keep fatigue in mind. Weigh the
   review's recovery, sleep, body, activity and imported workouts: ease off after poor recovery, a hard cardio
   day or an unusually active one.
3. Progress each exercise from its recent sets at the same location_id (`effort_history` with that location);
   loads don't carry over between gyms. For a swap, take one from `swap_list`.
   With no history there, prescribe reps and rir and leave out load.
4. Show me the week in a short table and wait for my yes before you write anything.
5. Write it with one `log` of kind `batch`: a plan for the week, then a planned_session per training day with
   scheduled_on, location_id, blocks and links {{"part_of": {{"kind": "plan", "id": <the plan's id>}}}}. Give the
   plan an id you make up as a UUID so the sessions can name it. A block repeats its efforts for `rounds`, with rest
   in its metrics, for example {{"rounds": 3, "metrics": {{"rest": {{"value": 120, "unit": "s"}}}}, "efforts":
   [{{"activity_id": "barbell_back_squat", "metrics": {{"reps": {{"min": 6, "max": 8}}, "load": {{"value": 100,
   "unit": "kg"}}, "rir": {{"value": 2}}}}}}]}}. Change a planned session later with `edit`, take one out with
   `remove`, and take back any write with `undo`.
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
    names = {a["name"] for a in PROMPTS[name]["arguments"]}
    given = "".join(f"My {k}: {v}\n" for k, v in arguments.items() if v and k in names)
    return TEMPLATES[name].format(today=date.today().isoformat(), given=given)
