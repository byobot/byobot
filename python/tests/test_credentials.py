import os

import pytest

from byobot import credentials


@pytest.fixture(autouse=True)
def file_credentials(monkeypatch):
    monkeypatch.setenv("BYOBOT_CREDENTIALS", "file")


def test_credentials_file_is_yours_alone(byobot_home):
    credentials.save("example", {"a": 1})
    assert credentials.load("example") == {"a": 1}
    if os.name == "posix":
        assert os.stat(byobot_home / "credentials" / "example.json").st_mode & 0o777 == 0o600
    assert credentials.delete("example") and credentials.load("example") is None
