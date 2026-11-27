"""Where cloud files live: the interface Drive implements."""

from __future__ import annotations

import hashlib
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
