"""One writer at a time across processes: a lock on a small file, which the system drops if a process dies."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def held(path: Path) -> Iterator[None]:
    """Holds the lock for the block, waiting while another process holds it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        _take(f)
        try:
            yield
        finally:
            _drop(f)


if os.name == "nt":  # pragma: no cover - Windows
    import msvcrt

    def _take(f) -> None:
        while True:  # LK_LOCK gives up after 10 s, and a holder may keep the lock longer
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.2)

    def _drop(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _take(f) -> None:
        fcntl.flock(f, fcntl.LOCK_EX)

    def _drop(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)
