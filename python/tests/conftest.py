import copy

import pytest

from byobot.core.contract import Contract


def newer_contract(contract: Contract, version: int, kinds: dict[str, dict] | None = None) -> Contract:
    """A contract as an install `version` would know it: every kind at that version, plus `kinds`."""
    registry = copy.deepcopy(contract.registry)
    for spec in registry["kinds"].values():
        spec["version"] = max(spec["version"], version)
    registry["kinds"].update(kinds or {})
    return Contract(registry, contract.schemas)


@pytest.fixture(autouse=True)
def byobot_home(tmp_path, monkeypatch):
    """Every test gets its own empty byobot home, credentials file and activity library."""
    home = tmp_path / "byobot-home"
    monkeypatch.setenv("BYOBOT_HOME", str(home))
    monkeypatch.setenv("BYOBOT_PROFILE", "")
    monkeypatch.setenv("BYOBOT_CREDENTIALS", "file")  # never the Keychain of the Mac running the tests
    empty = tmp_path / "library"
    empty.mkdir()
    (empty / "vocabulary.json").write_text("{}")
    monkeypatch.setenv("BYOBOT_LIBRARY", str(empty))
    monkeypatch.delenv("BYOBOT_ACTOR", raising=False)
    monkeypatch.delenv("BYOBOT_AGENT_MODEL", raising=False)
    return home


@pytest.fixture
def store(byobot_home):
    from byobot.core.store import Store

    s = Store(byobot_home / "byobot.db")
    yield s
    s.close()

