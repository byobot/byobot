"""Deep review (cli): a person's or an agent's mistake gets a JSON error and exit 1, never a Python traceback.

cli.py promises "Errors print JSON on stderr and exit with status 1", but main() only catches ByobotError, so any
other exception from an ordinary input escapes as a traceback; MCP answers the same inputs with JSON-RPC -32603.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import via_cli, via_mcp  # noqa: E402

from byobot import cli  # noqa: E402


@pytest.mark.parametrize("make,argv", [
    (lambda tmp: None, ["log", "session", "@{tmp}/no-such-file.json"]),
    (lambda tmp: (tmp / "a-folder").mkdir(), ["log", "session", "@{tmp}/a-folder"]),
    (lambda tmp: (tmp / "latin1.json").write_bytes(b'{"notes": "caf\xe9"}'), ["log", "session", "@{tmp}/latin1.json"]),
    (lambda tmp: None, ["export", "{tmp}/no-such-folder/backup.jsonl"]),
    (lambda tmp: None, ["review", "--until", "9999-12-31"]),
    (lambda tmp: None, ["review", "--until", "0001-01-07"]),
    (lambda tmp: None, ["review", "--weeks", "1000000"]),
    (lambda tmp: None, ["observe", "body_mass", "80", "--unit", "kg", "--start", "9999-12-31T23:30:00"]),
    (lambda tmp: None, ["validate", "[1, 2]"]),
    (lambda tmp: (tmp / "a-file").write_text("x"), ["--home", "{tmp}/a-file", "status"]),
    (lambda tmp: (tmp / "cut.jsonl").write_text('{"byobot_export": 1}\n'), ["restore", "{tmp}/cut.jsonl"]),
], ids=["@missing", "@folder", "@not-utf8", "export-missing-folder", "review-until-9999", "review-until-0001",
        "review-weeks-huge", "observe-year-9999", "validate-a-list", "home-is-a-file",
        "restore-header-without-id"])
def test_cli_a_mistake_is_a_json_error_not_a_traceback(make, argv, tmp_path):
    """CLI-3."""
    make(tmp_path)
    answer = via_cli([a.format(tmp=tmp_path) for a in argv])
    assert answer[0] in ("error", "ok"), answer
    if argv[0] == "validate":
        assert answer[0] == "ok" or answer == ("error", "invalid")


@pytest.mark.parametrize("tool,args", [
    ("review", {"until": "9999-12-31"}),
    ("review", {"weeks": 1000000}),
    ("validate", {"document": [1, 2]}),
    ("observe", {"code": "body_mass", "value": 80, "unit": "kg", "start": "9999-12-31T23:30:00"}),
])
def test_cli_mcp_answers_a_mistake_as_a_tool_error_not_an_internal_error(tool, args):
    """CLI-3: the same inputs over MCP are JSON-RPC -32603 "internal error", which agents treat as a server fault."""
    answer = via_mcp(tool, args)
    assert answer[0] != "broken", answer


def test_cli_a_closed_pipe_ends_quietly(monkeypatch):
    """CLI-9: `byobot list ... | head` that closes early must not print a BrokenPipeError traceback."""

    class Closed(io.StringIO):
        def write(self, text):
            raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(sys, "stdout", Closed())
    try:
        cli.main(["schema"])
    except SystemExit:
        pass


def test_cli_ctrl_c_ends_with_130_and_no_traceback(monkeypatch, capsys):
    """CLI-9: Ctrl-C during a command (a long query, an import) should exit 130 quietly."""

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupted)
    try:
        code = cli.main(["status"])
    except SystemExit as exc:
        code = exc.code
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped main(), so Python prints a traceback")
    assert code == 130
    assert "Traceback" not in capsys.readouterr().err


def test_cli_serve_with_a_port_out_of_range_is_a_json_error(capsys):
    """CLI-3: `byobot serve --port 70000` raises OverflowError, which run() doesn't catch (it catches OSError)."""
    try:
        code = cli.main(["serve", "--port", "70000", "--no-pair"])
    except SystemExit as exc:
        code = exc.code
    except OverflowError as exc:
        pytest.fail(f"traceback: {exc!r}")
    err = capsys.readouterr().err
    assert code == 1 and "error" in json.loads(err)
