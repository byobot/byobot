"""Small shared helpers: paths, ids, clocks and errors."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROFILE_NAME = re.compile(r"[a-z0-9_]{1,40}")


def profile() -> str | None:
    """The profile in use (``$BYOBOT_PROFILE``), or None for the default one."""
    name = os.environ.get("BYOBOT_PROFILE") or None
    if name is not None and not PROFILE_NAME.fullmatch(name):
        raise ValidationFailed(f"profile names are lowercase letters, digits and _, not {name!r}")
    return name


def base() -> Path:
    """``$BYOBOT_HOME``, else ``~/.byobot``."""
    override = os.environ.get("BYOBOT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".byobot"


def home() -> Path:
    """The byobot home directory: the base, or ``<base>/profiles/<name>`` for a named profile."""
    name = profile()
    return base() / "profiles" / name if name else base()


def db_path() -> Path:
    return home() / "byobot.db"


SERVERS = ("serve", "remote")
DEFAULT_PORTS = (8765, 8766, 8780)  # serve, WHOOP's sign-in callback, remote


def choose_port(server: str, host: str, default: int, ports: Iterable[int]) -> int:
    """The port this profile's ``serve`` or ``remote`` used last, else ``default`` for the default profile, else the
    first free one of ``ports`` that no other profile has kept for either server."""
    saved = home() / server / "port"
    if saved.is_file():
        return int(saved.read_text().strip())
    if profile() is None:
        return default
    homes = [base(), *(base() / "profiles").glob("*")]
    kept = {f.read_text().strip() for h in homes for s in SERVERS if (f := h / s / "port").is_file()}
    for port in ports:
        if port in DEFAULT_PORTS or str(port) in kept:
            continue
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise ByobotError(f"no free port for byobot {server}; pass --port")


SYNC_FOLDERS = (("Library", "CloudStorage"), ("Library", "Mobile Documents"), ("Dropbox",), ("Google Drive",),
                ("My Drive",), ("OneDrive",), ("iCloud Drive",), ("iCloudDrive",))


def sync_folder_warning(path: Path) -> str | None:
    """A warning when ``path`` sits inside a folder another app syncs, where a SQLite database can be corrupted."""
    given = Path(path).expanduser().absolute()
    try:
        seen = [given, given.resolve()]  # a home linked into a synced folder counts too
    except OSError:
        seen = [given]
    for match in SYNC_FOLDERS:
        n, want = len(match), tuple(m.lower() for m in match)
        if any(tuple(x.lower() for x in p.parts[i:i + n]) == want for p in seen for i in range(len(p.parts) - n + 1)):
            return (f"{path} is inside a folder another app syncs ({'/'.join(match)}); move byobot's home out of it "
                    "(set BYOBOT_HOME), since syncing the database can corrupt it")
    return None


_uuid7_lock = threading.Lock()
_uuid7_last = [0, 0]  # last (milliseconds, counter) handed out


def uuid7() -> str:
    """A time-ordered UUID (RFC 9562 version 7), as a lowercase string.

    Ids made in the same millisecond by this process still sort in the order they
    were made (a 12-bit counter in ``rand_a``, RFC 9562 method 1), because events
    with equal times and devices apply in id order.
    """
    rand = int.from_bytes(os.urandom(10), "big")
    rand_b = rand & ((1 << 62) - 1)
    with _uuid7_lock:
        ms = max(time.time_ns() // 1_000_000, _uuid7_last[0])
        if ms == _uuid7_last[0]:
            rand_a = _uuid7_last[1] + 1
            if rand_a > 0xFFF:  # counter spent: borrow the next millisecond
                ms, rand_a = ms + 1, 0
        else:
            rand_a = (rand >> 62) & 0x7FF  # start low so the counter has room
        _uuid7_last[:] = [ms, rand_a]
    value = (ms & ((1 << 48) - 1)) << 80 | 0x7 << 76 | rand_a << 64 | 0b10 << 62 | rand_b
    return str(uuid.UUID(int=value))


def now() -> str:
    """The current time as RFC 3339 UTC with milliseconds, e.g. ``2026-11-20T18:00:00.000Z``."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utf8_stdio() -> None:
    """Make stdin/stdout UTF-8 everywhere (Windows consoles default to a legacy code page)."""
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):  # pragma: no cover - e.g. a detached stream
                pass


def canonical_json(value: Any) -> str:
    """Stable JSON text, so identical documents compare equal as strings."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ByobotError(Exception):
    """An error meant for the caller (a person or an agent), with optional details."""

    code = "error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


class ValidationFailed(ByobotError):
    code = "invalid"


class NotFound(ByobotError):
    code = "not_found"


class Conflict(ByobotError):
    code = "conflict"


class Forbidden(ByobotError):
    code = "forbidden"


def plain_text(text: str, limit: int = 100) -> str:
    """Text another app wrote, on one line with no control or invisible characters, at most `limit` long."""
    shown = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp") else c for c in text)
    return " ".join(shown.split())[:limit]
