"""Where byobot keeps the secrets a connector needs: the macOS Keychain, else a file only you can read.

Set BYOBOT_CREDENTIALS=file to use the file even on a Mac.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from byobot import lock
from byobot.util import ByobotError, base, home, profile

SERVICE = "byobot"


def _keychain() -> bool:
    return (os.environ.get("BYOBOT_CREDENTIALS") != "file" and sys.platform == "darwin"
            and shutil.which("security") is not None)


def _account(name: str) -> str:
    """The Keychain account: ``whoop`` for the default profile, ``alex/whoop`` for profile ``alex``, and for a home
    other than ~/.byobot, a hash of its path before that, so two homes never share a sign-in."""
    who = profile()
    account = f"{who}/{name}" if who else name
    where = base().expanduser().resolve()
    if where != (Path.home() / ".byobot").resolve():
        account = f"{hashlib.sha256(str(where).encode()).hexdigest()[:16]}/{account}"
    return account


def _file(name: str) -> Path:
    return home() / "credentials" / f"{name}.json"


def load(name: str) -> dict | None:
    if _keychain():
        out = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", _account(name), "-w"],
                             capture_output=True, text=True)
        return json.loads(bytes.fromhex(out.stdout.strip())) if out.returncode == 0 else None
    path = _file(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(name: str, secret: dict) -> None:
    data = json.dumps(secret).encode("utf-8")
    if _keychain():
        # Through stdin, so the secret never shows in the process list.
        out = subprocess.run(["security", "-i"], capture_output=True, text=True,
                             input=f'add-generic-password -U -s {SERVICE} -a {_account(name)} -w {data.hex()}\n')
        if out.returncode != 0 or out.stderr.strip():
            raise ByobotError(f"couldn't save to the Keychain: {out.stderr.strip() or out.returncode}")
        return
    path = _file(name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextmanager
def locked(name: str) -> Iterator[None]:
    """One process at a time, for a read-then-save such as a token refresh."""
    with lock.held(home() / "credentials" / f"{name}.lock"):
        yield


def delete(name: str) -> bool:
    if _keychain():
        return subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", _account(name)],
                              capture_output=True).returncode == 0
    path = _file(name)
    if path.exists():
        path.unlink()
        return True
    return False
