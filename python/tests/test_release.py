"""One release number: byobot/__init__.py holds it, and the iPhone app ships the same release."""

import re
from pathlib import Path

from byobot import __version__

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "schema" / "v1").is_dir())


def test_the_app_ships_the_packages_release():
    app = re.search(r'MARKETING_VERSION: "([^"]+)"', (ROOT / "apps" / "ios" / "project.yml").read_text())[1]
    assert re.match(r"\d+\.\d+\.\d+", __version__)[0] == app

