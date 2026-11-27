"""Review: on a Mac, connector secrets go to the Keychain under an account named by the profile only, so two byobot
homes (`--home` / BYOBOT_HOME) share them, though they share nothing else."""

import subprocess
import sys

from byobot import credentials


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
