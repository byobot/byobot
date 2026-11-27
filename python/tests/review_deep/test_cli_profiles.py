"""Deep review (cli): several people on one computer, each with a profile, never step on each other."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from _cli_helpers import via_cli  # noqa: E402

from byobot import remote, serve  # noqa: E402


@pytest.mark.parametrize("module,saved_in", [(serve, serve.serve_dir), (remote, remote.remote_dir)],
                         ids=["serve", "remote"])
def test_cli_two_profiles_never_keep_the_same_port(module, saved_in, monkeypatch):
    """CLI-5: alex runs `byobot serve` (or `remote`) first and keeps port 8767 (8781). Later, while alex's server is
    stopped, sam runs it for the first time: the port is free, so sam keeps the same one. From then on the two can't
    serve at once ("could not listen"), and alex's tunnel (`tailscale funnel ... 8781`) reaches sam's server."""
    monkeypatch.setenv("BYOBOT_PROFILE", "alex")
    alex = module.choose_port("127.0.0.1")
    saved_in().mkdir(parents=True, exist_ok=True)
    (saved_in() / "port").write_text(f"{alex}\n")  # what run() writes once it listens
    monkeypatch.setenv("BYOBOT_PROFILE", "sam")
    sam = module.choose_port("127.0.0.1")
    assert sam != alex


def test_cli_a_mistyped_profile_is_refused_not_created(byobot_home):
    """CLI-6: `byobot --profile alxe ...` (or an MCP config with that typo) quietly makes a new empty store: reads
    say there is nothing, and writes land in a store nobody looks at. Restore only fills an empty store, so they
    can't be moved back."""
    assert via_cli(["--profile", "alex", "init"])[0] == "ok"
    assert via_cli(["--profile", "alex", "log", "location", '{"name": "Example gym"}', "--id", "example_gym"])[0] == "ok"
    read = via_cli(["--profile", "alxe", "list", "locations"])
    write = via_cli(["--profile", "alxe", "log", "observation", '{"code": "body_mass", "value": 80, "unit": "kg"}'])
    assert (read[0], write[0]) == ("error", "error")
    assert not (byobot_home / "profiles" / "alxe").exists()
