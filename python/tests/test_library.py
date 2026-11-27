"""Checks every file in library/, the activity library that ships with byobot."""

import json
import re
from pathlib import Path

import pytest

from byobot.core.contract import Contract

CONTRACT = Contract.shipped()

HERE = Path(__file__).resolve()
ROOT = next(p / "library" for p in HERE.parents if (p / "library" / "vocabulary.json").is_file())
FILES = sorted((ROOT / "activities").glob("*.json")) if (ROOT / "activities").is_dir() else []
VOCABULARY = json.loads((ROOT / "vocabulary.json").read_text(encoding="utf-8"))


def test_vocabulary_is_sorted_unique_names():
    assert set(VOCABULARY) == {"muscles", "load_types", "equipment", "movement_types"}
    for kind, names in VOCABULARY.items():
        assert names == sorted(set(names)), f"{kind} must be sorted, without repeats"
        for name in names:
            assert CONTRACT.payload_errors("activity", {"name": name}, name) == [], name


def test_movement_types_are_compound_or_isolation():
    assert VOCABULARY["movement_types"] == ["compound", "isolation"]


@pytest.mark.parametrize("path", FILES, ids=[f.stem for f in FILES])
def test_library_activity(path):
    activity = json.loads(path.read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", path.stem), "name the file by its id: lowercase words joined by underscores"
    assert CONTRACT.payload_errors("activity", activity, path.stem) == []
    assert "id" not in activity, "the file name is the id"
    assert not any(v in ([], {}) for v in [*activity.values(), *activity.get("targets", {}).values()]), \
        "leave out empty lists"
    targets = activity.get("targets", {})
    main, synergists = targets.get("main", []), targets.get("synergists", [])
    assert set(main) | set(synergists) <= set(VOCABULARY["muscles"]), "use muscles from vocabulary.json"
    assert not set(main) & set(synergists), "a muscle is main or a synergist, not both"
    assert activity["load_type"] in VOCABULARY["load_types"], "use a load type from vocabulary.json"
    assert set(activity.get("equipment", [])) <= set(VOCABULARY["equipment"]), "use equipment from vocabulary.json"
    if "movement_type" in activity:
        assert activity["movement_type"] in VOCABULARY["movement_types"], "use a movement type from vocabulary.json"


def test_library_names_are_unique():
    names = [json.loads(f.read_text(encoding="utf-8"))["name"].casefold() for f in FILES]
    assert len(names) == len(set(names))
