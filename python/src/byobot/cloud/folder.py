"""Where cloud files live. Drive comes later behind the same interface; a local folder is for tests only, never a
folder another app syncs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol

from .segment import ID


class Exists(Exception):
    """A file is written once; another with the same path is already there."""


class Folder(Protocol):
    def list(self) -> dict[str, str]:
        """Every file under the store's folder: path -> checksum."""

    def read(self, path: str) -> bytes | None:
        """A file's bytes, or None if it's gone."""

    def create(self, path: str, data: bytes) -> None:
        """Write a new file; raise Exists if the path is taken."""

    def delete(self, path: str) -> None:
        """Delete outright. A file already gone is fine."""


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(path: str) -> str:
    parts = path.split("/")
    if not parts or any(not ID.fullmatch(p) for p in parts):
        raise ValueError(f"not a cloud path: {path!r}")
    return path


class MemoryFolder:
    """A folder in memory, for tests and the stress harness."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def list(self) -> dict[str, str]:
        return {p: checksum(d) for p, d in sorted(self.files.items())}

    def size(self) -> int:
        return sum(len(d) for d in self.files.values())

    def read(self, path: str) -> bytes | None:
        return self.files.get(path)

    def create(self, path: str, data: bytes) -> None:
        if _safe(path) in self.files:
            raise Exists(path)
        self.files[path] = bytes(data)

    def delete(self, path: str) -> None:
        self.files.pop(path, None)


class LocalFolder:
    """The same layout on a local disk, under ``root/<store_id>``."""

    def __init__(self, root: Path | str, store_id: str):
        self.root = Path(root) / _safe(store_id)

    def list(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.root.rglob("*")) if self.root.is_dir() else []:
            if p.is_file() and not p.name.startswith(".tmp-"):
                out[p.relative_to(self.root).as_posix()] = checksum(p.read_bytes())
        return out

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file()) if self.root.is_dir() else 0

    def read(self, path: str) -> bytes | None:
        try:
            return (self.root / _safe(path)).read_bytes()
        except FileNotFoundError:
            return None

    def create(self, path: str, data: bytes) -> None:
        target = self.root / _safe(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".tmp-{os.getpid()}-{target.name}")
        tmp.write_bytes(data)
        try:
            os.link(tmp, target)  # fails if the target exists, so a file is never replaced
        except FileExistsError:
            raise Exists(path) from None
        finally:
            tmp.unlink()

    def delete(self, path: str) -> None:
        (self.root / _safe(path)).unlink(missing_ok=True)
