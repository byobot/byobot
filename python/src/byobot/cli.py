"""The ``byobot`` command line, generated from the command registry.

Every command prints JSON on stdout, so scripts and agents can read it.
Errors print JSON on stderr and exit with status 1.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from byobot import __version__, mascot
from byobot.commands import REGISTRY, Context, Param, run
from byobot.util import ByobotError, ValidationFailed, db_path, profile, utf8_stdio

DEFAULT_ACTOR = "human:me"


def parse_actor(text: str) -> dict:
    """``KIND:ID[:MODEL]``, for example ``human:me`` or ``agent:claude-code:some-model``."""
    parts = text.split(":", 2)
    if len(parts) < 2 or parts[0] not in ("human", "agent", "connector") or not parts[1]:
        raise ValidationFailed(f"actor must look like KIND:ID[:MODEL] with KIND human, agent or connector, not {text!r}")
    actor = {"kind": parts[0], "id": parts[1]}
    if len(parts) == 3 and parts[2]:
        actor["model"] = parts[2]
    return actor


def read_json(text: str) -> Any:
    """A JSON argument: literal JSON, ``@path`` for a file, or ``-`` for stdin. Files may be JSON lines."""
    if text == "-":
        raw, where = sys.stdin.read(), "stdin"
    elif text.startswith("@"):
        raw, where = Path(text[1:]).expanduser().read_text(encoding="utf-8"), text[1:]
    else:
        raw, where = text, "argument"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first:
        if where != "argument":
            try:
                return [json.loads(line) for line in raw.splitlines() if line.strip()]
            except json.JSONDecodeError:
                pass
        raise ValidationFailed(f"could not read JSON from {where}: {first}") from None


def _add_param(parser: argparse.ArgumentParser, p: Param) -> None:
    kwargs: dict[str, Any] = {"help": p.help}
    if p.type == "integer":
        kwargs["type"] = int
    if p.choices:
        kwargs["choices"] = p.choices
    if p.positional:
        if not p.required:
            kwargs["nargs"] = "?"
        parser.add_argument(p.name, **kwargs)
    else:
        if p.default is not None:
            kwargs["default"] = p.default
        parser.add_argument(f"--{p.name.replace('_', '-')}", dest=p.name, **kwargs)


def _members(group: str) -> list[str]:
    return [c.name.split(" ", 1)[1] for c in REGISTRY.values() if c.cli and c.name.startswith(group + " ")]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="byobot",
        description="byobot: bring your own bot. Your health data in one local store, for the agent of your choice.",
    )
    parser.add_argument("--version", action="version", version=f"byobot {__version__}")
    parser.add_argument("--home", help="Use this byobot home instead of $BYOBOT_HOME or ~/.byobot.")
    parser.add_argument("--profile", help="Use this person's profile, with its own store and sign-ins. "
                                          "Default: $BYOBOT_PROFILE, else the default profile.")
    parser.add_argument("--actor", help=f"Who is acting, as KIND:ID[:MODEL]. Default: $BYOBOT_ACTOR or {DEFAULT_ACTOR}.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    groups: dict[str, Any] = {}
    for cmd in REGISTRY.values():
        if not cmd.cli:
            continue
        where, name = sub, cmd.name
        if " " in cmd.name:
            group, name = cmd.name.split(" ", 1)
            if group not in groups:
                gp = sub.add_parser(group, help=f"{group.capitalize()}: {', '.join(_members(group))}.")
                groups[group] = gp.add_subparsers(dest="subcommand", metavar="<source>")
            where = groups[group]
        p = where.add_parser(name, help=cmd.summary, description=" ".join(x for x in (cmd.summary, cmd.description) if x))
        for param in cmd.params:
            _add_param(p, param)
    sub.add_parser("mcp", help="Serve these commands to an agent over MCP (stdio).",
                   description="Run an MCP server on stdin/stdout. Point your agent's MCP config at `byobot mcp`.")
    serve = sub.add_parser(
        "serve", help="Sync with the byobot app on your phone over your own network.",
        description="Serve the plan to the byobot app and take back what it logged, over HTTPS on your local network. "
                    "Shows a QR code to pair a phone when none is paired yet (or with --pair).")
    serve.add_argument("--host", default="0.0.0.0", help="Address to listen on. Default: all interfaces.")
    serve.add_argument("--port", type=int, help="Port to listen on. Default: the one this profile used last, "
                                                "else 8765 for the default profile.")
    serve.add_argument("--pair", action="store_true", default=None, help="Show a pairing code even if a device is paired.")
    serve.add_argument("--no-pair", dest="pair", action="store_false", help="Do not show a pairing code.")
    remote = sub.add_parser(
        "remote", help="Serve these commands over the internet, for assistants on your phone.",
        description="Serve the MCP tools at /mcp, with sign-in, for assistants that run elsewhere. Listens on this "
                    "computer only; run a tunnel (such as Tailscale Funnel) that gives it an https address.")
    remote.add_argument("--url", help="The https address your tunnel gives this profile. Saved after the first run.")
    remote.add_argument("--host", default="127.0.0.1", help="Address to listen on. Default: this computer only.")
    remote.add_argument("--port", type=int, help="Port to listen on. Default: the one this profile used last, "
                                                 "else 8780 for the default profile.")
    remote.add_argument("--pair", action="store_true", default=None,
                        help="Show a code to connect an assistant even if one is connected.")
    remote.add_argument("--no-pair", dest="pair", action="store_false", help="Do not show a code.")
    remote.add_argument("--revoke", action="store_true", help="Disconnect every assistant from this profile, then exit.")
    remote.add_argument("--api-key", metavar="NAME", help="Make an API key for an assistant that calls an API, "
                                                          "such as `my_assistant`, print it once, then exit.")
    return parser


def _banner() -> None:
    if not sys.stderr.isatty():
        return
    try:
        where = [f"profile  {profile() or 'default'}", f"db  {db_path()}"]
    except ByobotError as exc:
        where = [exc.message]
    info = [f"byobot {__version__} · bring your own bot", *where, "run `byobot --help` for commands"]
    sys.stderr.write("\n".join(mascot.beside(mascot.render(), info)) + "\n\n")


def main(argv: Sequence[str] | None = None) -> int:
    utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.home:
        os.environ["BYOBOT_HOME"] = args.home
    if args.profile:
        os.environ["BYOBOT_PROFILE"] = args.profile
    if args.command is None:
        _banner()
        parser.print_help()
        return 0
    try:
        profile()
        actor = parse_actor(args.actor or os.environ.get("BYOBOT_ACTOR") or DEFAULT_ACTOR)
        if args.command == "mcp":
            from byobot.mcp import serve

            return serve()
        if args.command == "serve":
            from byobot import serve as lan

            return lan.run(args.host, args.port, args.pair)
        if args.command == "remote":
            from byobot import remote

            return remote.run(args.url, args.host, args.port, args.pair, args.revoke, args.api_key)
        name = f"{args.command} {args.subcommand}" if getattr(args, "subcommand", None) else args.command
        if name not in REGISTRY:
            parser.parse_args([args.command, "--help"])
        cmd = REGISTRY[name]
        values = {}
        for p in cmd.params:
            value = getattr(args, p.name, None)
            if value is not None and p.type == "json":
                value = read_json(value)
            values[p.name] = value
        ctx = Context(actor=actor, path=db_path())
        try:
            result = run(cmd.name, ctx, values)
        finally:
            ctx.close()
    except ByobotError as exc:
        sys.stderr.write(json.dumps(exc.to_dict(), indent=2) + "\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    if cmd.name == "validate" and not result["valid"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
