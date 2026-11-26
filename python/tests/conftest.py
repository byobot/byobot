import pytest


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

