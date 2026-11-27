"""The ``byobot`` command line, generated from the command registry.

Every command prints JSON on stdout, so scripts and agents can read it.
Errors print JSON on stderr and exit with status 1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import textwrap
from pathlib import Path
from typing import Any, NoReturn, Sequence

from byobot import __version__, mascot
from byobot.commands import REGISTRY, Context, Param, run
from byobot.util import ByobotError, NotFound, ValidationFailed, db_path, home, profile, utf8_stdio

DEFAULT_ACTOR = "human:me"

# `byobot --help` lists the commands in the order someone needs them.
SECTIONS = {
    "start": ("init", "status", "connect", "disconnect", "import", "serve", "mcp"),
    "training data": ("log", "list", "review", "query", "effort", "swap"),
    "fixing": ("edit", "remove", "undo", "conflicts", "resolve", "correct", "exclude", "same_as", "versions", "erase"),
    "setup": ("devices", "cloud", "remote", "export", "restore", "schema", "validate"),
}
GROUPS = {
    "effort": "What you did of one activity",
    "swap": "Activities that can stand in for one another",
    "devices": "Phones paired with `byobot serve`",
    "cloud": "Google Drive sync",
    "remote": "Assistants on your phone, over the internet",
}


def parse_actor(text: str) -> dict:
    """``KIND:ID[:MODEL]``, for example ``human:me`` or ``agent:claude-code:some-model``."""
    parts = text.split(":", 2)
    if len(parts) < 2 or parts[0] not in ("human", "agent", "connector") or not parts[1]:
        raise ValidationFailed(f"actor must look like KIND:ID[:MODEL] with KIND human, agent or connector, not {text!r}")
    actor = {"kind": parts[0], "id": parts[1]}
    if len(parts) == 3 and parts[2]:
        actor["model"] = parts[2]
    return actor


def read_json(text: str, text_ok: bool = False) -> Any:
    """A JSON argument: literal JSON, ``@path`` for a file, or ``-`` for stdin. Files may be JSON lines.
    With ``text_ok``, an argument that isn't JSON is taken as text."""
    if text == "-":
        raw, where = sys.stdin.read(), "stdin"
    elif text.startswith("@"):
        where = text[1:]
        try:
            raw = Path(where).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationFailed(f"could not read {where}: {exc}") from None
    else:
        raw, where = text, "argument"
    try:
        return json.loads(raw)
    except json.JSONDecodeError as first:
        if where == "argument" and text_ok:
            return raw
        if where != "argument":
            try:
                return [json.loads(line) for line in raw.splitlines() if line.strip()]
            except json.JSONDecodeError:
                pass
        raise ValidationFailed(f"could not read JSON from {where}: {first}") from None


class Parser(argparse.ArgumentParser):
    """Usage errors are JSON errors too."""

    def error(self, message: str) -> NoReturn:
        raise ValidationFailed(message, details={"usage": self.format_usage().strip()})


def _add_param(parser: argparse.ArgumentParser, p: Param) -> None:
    kwargs: dict[str, Any] = {"help": p.help + (" Use @path for a file." if p.type == "json" else "")}
    if p.type == "integer":
        kwargs["type"] = int
    if p.type == "boolean":
        kwargs["action"] = argparse.BooleanOptionalAction
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


def _overview(lines: dict[str, str]) -> str:
    """The commands by section, each with the first clause of its summary."""
    width = shutil.get_terminal_size().columns - 2
    out = ["byobot: bring your own bot. Your health data in one local store, for the agent of your choice."]
    for section, names in SECTIONS.items():
        out.append(f"\n{section}:")
        for name in names:
            short = (lines[name] if name in GROUPS else re.split(r"[.:] ", lines[name], maxsplit=1)[0]).rstrip(".")
            out.append(textwrap.fill(short, width, initial_indent=f"  {name:<12}", subsequent_indent=" " * 14))
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(prog="byobot", usage="%(prog)s [options] <command> ...",
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"byobot {__version__}")
    parser.add_argument("--home", help="Use this byobot home instead of $BYOBOT_HOME or ~/.byobot.")
    parser.add_argument("--profile", help="Use this person's profile, with its own store and sign-ins. "
                                          "Default: $BYOBOT_PROFILE, else the default profile.")
    parser.add_argument("--actor", help=f"Who is acting, as KIND:ID[:MODEL]. Default: $BYOBOT_ACTOR or {DEFAULT_ACTOR}.")
    sub = parser.add_subparsers(dest="command", metavar="<command>", help=argparse.SUPPRESS)
    groups: dict[str, Any] = {}
    lines: dict[str, str] = {}
    for cmd in REGISTRY.values():
        if not cmd.cli:
            continue
        where, name = sub, cmd.name
        if " " in cmd.name:
            group, name = cmd.name.split(" ", 1)
            if group not in groups:
                lines[group] = f"{GROUPS[group]}: {', '.join(_members(group))}."
                gp = sub.add_parser(group, help=lines[group], description=lines[group])
                groups[group] = gp.add_subparsers(dest="subcommand", metavar="<command>")
            where = groups[group]
        elif not cmd.hidden:
            lines[name] = cmd.summary
        shown = {} if cmd.hidden else {"help": cmd.summary}  # a parser added with no help is left out of the list
        p = where.add_parser(name, **shown, description=" ".join(x for x in (cmd.summary, cmd.description) if x))
        for param in cmd.params:
            _add_param(p, param)
    parser.description = _overview(lines)
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
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # the reader went away, as with `byobot list ... | head`
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141


def _fail(exc: ByobotError) -> int:
    sys.stderr.write(json.dumps(exc.to_dict(), indent=2) + "\n")
    return 1


def _main(argv: Sequence[str] | None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except ByobotError as exc:
        return _fail(exc)
    if args.home:
        os.environ["BYOBOT_HOME"] = args.home
    if args.profile:
        os.environ["BYOBOT_PROFILE"] = args.profile
    if args.command is None:
        _banner()
        parser.print_help()
        return 0
    try:
        if (name := profile()) and not home().is_dir() and args.command not in ("init", "restore"):
            raise NotFound(f"no profile {name}; run `byobot --profile {name} init` to make it")
        actor = parse_actor(args.actor or os.environ.get("BYOBOT_ACTOR") or DEFAULT_ACTOR)
        name = f"{args.command} {args.subcommand}" if getattr(args, "subcommand", None) else args.command
        if name not in REGISTRY:
            parser.parse_args([args.command, "--help"])
        cmd = REGISTRY[name]
        values = {}
        for p in cmd.params:
            value = getattr(args, p.name, None)
            if value is not None and p.type == "json":
                kinds = (p.schema or {}).get("type")
                value = read_json(value, kinds == "string" or isinstance(kinds, list) and "string" in kinds)
            values[p.name] = value
        ctx = Context(actor=actor, path=db_path())
        try:
            result = run(cmd.name, ctx, values)
        finally:
            ctx.close()
    except ByobotError as exc:
        return _fail(exc)
    except BrokenPipeError:
        raise
    except OSError as exc:  # such as a --home that is a file
        return _fail(ByobotError(str(exc)))
    if result is None:  # a server, which ran until stopped
        return 0
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
