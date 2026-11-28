"""What several test modules share."""

import json
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "schema" / "v1").is_dir())
FIXTURES = REPO / "schema" / "fixtures" / "v1"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())
