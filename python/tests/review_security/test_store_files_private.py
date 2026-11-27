"""Review: a new store's folder and files take the umask, so on a Mac with a second account they're readable by it.
The store holds health data and the Drive keeper's keys, recovery key and join secret."""

import os
import stat
import sys

import pytest

from byobot.core.store import Store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def opened(path):
    old = os.umask(0o022)
    try:
        s = Store(path)
        s.conn.execute("PRAGMA user_version")
        s.close()
    finally:
        os.umask(old)


def test_a_new_store_is_readable_only_by_its_owner(tmp_path):
    home = tmp_path / "home"
    opened(home / "byobot.db")
    assert mode(home) == 0o700
    assert {f.name: mode(f) & 0o077 for f in home.iterdir() if f.name.startswith("byobot.db")} == {
        f.name: 0 for f in home.iterdir() if f.name.startswith("byobot.db")}


def test_an_existing_stores_files_are_made_private_when_opened(tmp_path):
    home = tmp_path / "home"
    opened(home / "byobot.db")
    home.chmod(0o755)
    (home / "byobot.db").chmod(0o644)
    opened(home / "byobot.db")
    assert {f.name: mode(f) for f in home.iterdir() if f.name.startswith("byobot.db")} == {
        f.name: 0o600 for f in home.iterdir() if f.name.startswith("byobot.db")}
