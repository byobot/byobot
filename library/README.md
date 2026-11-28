# The activity library

The exercises byobot ships. Every store sees them as activities; an activity a user defines with the same id replaces the library's for them.

## Adding or fixing an exercise

One file per activity in `activities/`, named `<id>.json`, holding its activity payload (see `schema/v1/records.schema.json`). `example_curl.json`:

```json
{
  "name": "Example Curl",
  "load_type": "dumbbell",
  "movement_type": "isolation",
  "fatigue": 1,
  "resistance_curve": { "stretch": 1, "middle": 3, "contract": 1 },
  "targets": { "main": ["biceps"] }
}
```

- Ids are lowercase with underscores, and never change or go away: plans and logged sets point at them.
- Muscles, load types, equipment and `movement_type` (`compound` or `isolation`) come from `vocabulary.json`. Add a new name there in sorted order.
- No muscle is both main and synergist, no two exercises share a name, and empty lists are left out.
- Only add ratings and notes that are your own work.

`pytest tests/test_library.py` (from `python/`) checks all of this. Then run `python tools/gen_swift.py` from the repository root, so the iPhone app ships the change.
