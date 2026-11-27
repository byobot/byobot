"""Deep review (cli): what the help tells a person or an agent to type works where it is shown."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import via_cli, via_mcp  # noqa: E402

from byobot import mcp  # noqa: E402


def test_cli_mcp_does_not_offer_at_path_files_it_cannot_read(tmp_path):
    """CLI-11: import-activities tells agents "Use @path for a file", but only the CLI reads @path; over MCP and the
    API the string is refused as "expected a list of activities"."""
    path = tmp_path / "activities.json"
    path.write_text(json.dumps([{"id": "x_example_carry", "name": "Example carry"}]))
    tool = next(t for t in mcp.tools() if t["name"] == "import-activities")
    if "@path" in json.dumps(tool):
        assert via_mcp("import-activities", {"activities": f"@{path}"})[0] == "ok"


def test_cli_a_text_value_is_taken_as_the_help_says(capsys):
    """CLI-11: `observe` says its value is "a number, or text for a category" and `resolve` takes an event id or
    "remove", but both are JSON arguments on the CLI, so `byobot observe x.mood good` and
    `byobot resolve session ID remove` stop at "could not read JSON" unless the word is typed with JSON quotes."""
    answer = via_cli(["observe", "x.mood", "good"])
    assert answer[0] == "ok", answer
    via_cli(["resolve", "session", "example-id", "remove"])
    assert "could not read JSON" not in capsys.readouterr().err
