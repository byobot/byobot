# The activity library

The exercises byobot ships. Every store sees them as activities; an activity a user defines with the same id replaces the library's for them.

## Adding or fixing an exercise

One file per activity in `activities/`, named `<id>.json`, holding an `activity.defined` payload (see `schema/v0/training.schema.json`):

```json
{
  "id": "example_curl",
  "name": "Example Curl",
  "equipment": ["dumbbell"],
  "movement_type": "isolation",
  "fatigue": 1,
  "resistance_curve": { "stretch": 1, "middle": 3, "contract": 1 },
  "targets": { "main": ["biceps"], "synergists": [] }
}
```

- Ids are lowercase with underscores, and never change or go away: plans and logged sets point at them.
- Muscles, equipment and `movement_type` (`compound` or `isolation`) come from `vocabulary.json`. Add a new name there in sorted order.
- No muscle is both main and synergist, and no two exercises share a name.
- Only add ratings and descriptions that are your own work.

`pytest tests/test_library.py` (from `python/`) checks all of this.
