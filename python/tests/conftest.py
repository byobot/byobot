import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
# The shared fixtures live at the repo root (or the sdist root, when testing an sdist).
FIXTURES = next(p / "schema" / "fixtures" / "v0" / "cases.json" for p in HERE.parents if (p / "schema" / "fixtures").is_dir())
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))["cases"]


@pytest.fixture(autouse=True)
def byobot_home(tmp_path, monkeypatch):
    """Every test gets its own empty byobot home and an empty activity library."""
    home = tmp_path / "byobot-home"
    monkeypatch.setenv("BYOBOT_HOME", str(home))
    monkeypatch.setenv("BYOBOT_PROFILE", "")
    empty = tmp_path / "library"
    empty.mkdir()
    (empty / "vocabulary.json").write_text("{}")
    monkeypatch.setenv("BYOBOT_LIBRARY", str(empty))
    monkeypatch.delenv("BYOBOT_ACTOR", raising=False)
    monkeypatch.delenv("BYOBOT_AGENT_MODEL", raising=False)
    return home


@pytest.fixture
def store(byobot_home):
    from byobot.store import Store

    s = Store(byobot_home / "byobot.db")
    yield s
    s.close()


def case(name):
    return next(c for c in CASES if c["name"] == name)["data"]
