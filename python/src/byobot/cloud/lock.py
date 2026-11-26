"""One keeper at a time: `serve`, the MCP server and the CLI can each sync, so they take a lock on a small file
first. The system drops it if a process dies."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def held(path: Path, wait: bool = True) -> Iterator[bool]:
    """Yields True once the lock is held, or False at once when ``wait`` is off and another process holds it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if not _take(f, wait):
            yield False
            return
        try:
            yield True
        finally:
            _drop(f)


if os.name == "nt":  # pragma: no cover - Windows
    import msvcrt

    def _take(f, wait: bool) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        except OSError:
            if wait:
                raise
            return False
        return True

    def _drop(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _take(f, wait: bool) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            return False
        return True

    def _drop(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)
