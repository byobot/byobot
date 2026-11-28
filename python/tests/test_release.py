"""One release number: byobot/__init__.py holds it, and the iPhone app ships the same release."""

import json
import re

from byobot import __version__

from helpers import REPO


def test_the_app_ships_the_packages_release():
    app = re.search(r'MARKETING_VERSION: "([^"]+)"', (REPO / "apps" / "ios" / "project.yml").read_text())[1]
    assert re.match(r"\d+\.\d+\.\d+", __version__)[0] == app


def test_schema_ids_name_a_release_not_a_branch():
    for path in sorted((REPO / "schema" / "v1").glob("*.schema.json")):
        found = json.loads(path.read_text(encoding="utf-8"))["$id"]
        assert re.fullmatch(rf"https://raw\.githubusercontent\.com/byobot/byobot/v\d+\.\d+\.\d+/schema/v1/{path.name}",
                            found), found
