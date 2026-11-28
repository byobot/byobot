import os
import subprocess
import sys

import pytest

from byobot import credentials


def test_credentials_file_is_yours_alone(byobot_home):
    credentials.save("example", {"a": 1})
    assert credentials.load("example") == {"a": 1}
    if os.name == "posix":
        assert os.stat(byobot_home / "credentials" / "example.json").st_mode & 0o777 == 0o600
    assert credentials.delete("example") and credentials.load("example") is None


def test_profiles_keep_separate_credentials(monkeypatch):
    credentials.save("example", {"who": "default"})
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    assert credentials.load("example") is None
    credentials.save("example", {"who": "alex"})
    assert credentials.load("example") == {"who": "alex"}
    monkeypatch.setenv("BYOBOT_PROFILE", "")
    assert credentials.load("example") == {"who": "default"}


def test_keychain_account_names_the_profile(monkeypatch):
    monkeypatch.delenv("BYOBOT_HOME")  # the default home keeps the names it always had
    calls = []
    monkeypatch.setattr(credentials, "_keychain", lambda: True)
    monkeypatch.setattr(credentials.subprocess, "run",
                        lambda args, **kw: calls.append(args) or type("R", (), {"returncode": 44, "stdout": ""})())
    credentials.load("example")
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    credentials.load("example")
    assert [c[c.index("-a") + 1] for c in calls] == ["example", "alex/example"]


def test_each_save_writes_its_own_temporary_file(byobot_home, monkeypatch):
    temps, replace = [], os.replace
    monkeypatch.setattr(credentials.os, "replace", lambda a, b: temps.append(a) or replace(a, b))
    credentials.save("example", {"n": 1})
    credentials.save("example", {"n": 2})
    assert len(set(temps)) == 2
    monkeypatch.setattr(credentials.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("busy")))
    with pytest.raises(OSError):
        credentials.save("example", {"n": 3})
    assert [p.name for p in (byobot_home / "credentials").iterdir()] == ["example.json"]
    assert credentials.load("example") == {"n": 2}


class FakeKeychain:
    """`security` as credentials.py drives it: add-generic-password through `-i`, find and delete as arguments."""

    def __init__(self):
        self.items = {}

    def run(self, cmd, capture_output=False, text=False, input=None, **kw):
        if cmd[:2] == ["security", "-i"]:
            words = input.split()
            service, account, secret = words[words.index("-s") + 1], words[words.index("-a") + 1], words[-1]
            self.items[(service, account)] = secret
            return subprocess.CompletedProcess(cmd, 0, "", "")
        key = (cmd[cmd.index("-s") + 1], cmd[cmd.index("-a") + 1])
        if cmd[1] == "find-generic-password":
            found = self.items.get(key)
            return subprocess.CompletedProcess(cmd, 0 if found else 44, (found or "") + "\n", "")
        return subprocess.CompletedProcess(cmd, 0 if self.items.pop(key, None) else 44, "", "")


def test_two_homes_do_not_share_keychain_secrets(tmp_path, monkeypatch):
    keychain = FakeKeychain()
    monkeypatch.delenv("BYOBOT_CREDENTIALS", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(credentials.shutil, "which", lambda name: "/usr/bin/security")
    monkeypatch.setattr(credentials.subprocess, "run", keychain.run)

    monkeypatch.setenv("BYOBOT_HOME", str(tmp_path / "home-a"))
    credentials.save("whoop", {"refresh_token": "example-token-of-person-a"})
    assert credentials.load("whoop") == {"refresh_token": "example-token-of-person-a"}

    monkeypatch.setenv("BYOBOT_HOME", str(tmp_path / "home-b"))  # `byobot --home <home-b> import whoop`
    assert credentials.load("whoop") is None, "the second home signs in to WHOOP as the first home's person"
