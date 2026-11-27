"""The activity library that ships with byobot.

``library/`` at the repository root holds one ``activities/<id>.json`` per
activity, its payload named by its id, and ``vocabulary.json``, the muscle, load type, equipment and movement
type names library files use. Built packages carry a copy in ``byobot/_library``. ``BYOBOT_LIBRARY``
points at another library directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from byobot.util import canonical_json


def library_dir() -> Path:
    override = os.environ.get("BYOBOT_LIBRARY")
    if override:
        return Path(override).expanduser()
    here = Path(__file__).resolve().parent
    # Installed package, then a git checkout (python/src/byobot -> repo root), then an unpacked sdist.
    for candidate in (here / "_library", here.parents[2] / "library", here.parents[1] / "library"):
        if (candidate / "vocabulary.json").is_file():
            return candidate
    raise RuntimeError("byobot library files not found; reinstall the package")


@lru_cache(maxsize=8)
def _read(root: Path) -> tuple[tuple[dict, ...], dict]:
    files = sorted((root / "activities").glob("*.json")) if (root / "activities").is_dir() else []
    activities = tuple({"id": f.stem, **json.loads(f.read_text(encoding="utf-8"))} for f in files)
    vocab_file = root / "vocabulary.json"
    vocabulary = json.loads(vocab_file.read_text(encoding="utf-8")) if vocab_file.is_file() else {}
    return activities, vocabulary


def activities(root: Path | None = None) -> list[dict]:
    """Every library activity, sorted by id."""
    return sorted(_read(root or library_dir())[0], key=lambda a: a["id"])


def vocabulary(root: Path | None = None) -> dict:
    """The muscle, load type, equipment and movement type names library activities use."""
    return _read(root or library_dir())[1]


def fingerprint(root: Path | None = None) -> str:
    """Changes whenever any library activity changes."""
    return hashlib.sha256(canonical_json(activities(root)).encode()).hexdigest()
