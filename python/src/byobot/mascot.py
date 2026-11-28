"""The byobot mascot: a cell with a Y-shaped antenna (two inputs, one stem).

Rendered with half blocks, two pixel rows per text row. Banners go to stderr,
never stdout, so piping ``byobot`` output stays clean. ``NO_COLOR`` gives a
mono silhouette and ``BYOBOT_ASCII=1`` (or ``TERM=dumb``) gives plain ASCII.
"""

from __future__ import annotations

import os

SPRITE = [
    "..a........a..",
    "...a......a...",
    "....a....a....",
    ".....a..a.....",
    "......aa......",
    "....######....",
    "..##########..",
    ".#####eee####.",
    ".####eweee###.",
    "#####eeeee####",
    "#####eeeee####",
    "######eee#####",
    "##############",
    ".############.",
    ".############.",
    "..##########..",
    "....######....",
]
PALETTE = {
    "#": ((244, 162, 97), 215),  # apricot body
    "a": ((161, 161, 170), 248),  # neutral antenna
    "e": ((24, 24, 27), 234),  # ink nucleus
    "w": ((255, 240, 222), 230),  # cream catchlight
}


def tier() -> str:
    if os.environ.get("BYOBOT_ASCII") or os.environ.get("TERM") == "dumb":
        return "ascii"
    if os.environ.get("NO_COLOR"):
        return "mono"
    if os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return "truecolor"
    return "256"


def _sgr(fg: str | None, bg: str | None, t: str) -> str:
    codes = []
    for prefix, color in (("38", fg), ("48", bg)):
        if color is None:
            continue
        rgb, idx = PALETTE[color]
        codes.append(f"{prefix};2;{rgb[0]};{rgb[1]};{rgb[2]}" if t == "truecolor" else f"{prefix};5;{idx}")
    return f"\x1b[{';'.join(codes)}m" if codes else ""


def compose() -> list[list[str]]:
    grid = [list(row) for row in SPRITE]
    if len(grid) % 2:
        grid.append(["."] * len(grid[0]))
    return grid


def render(t: str | None = None) -> list[str]:
    t = t or tier()
    if t == "ascii":
        return ["  y", " (o)"]
    grid = compose()
    lines = []
    for y in range(0, len(grid), 2):
        out = []
        for top, bot in zip(grid[y], grid[y + 1]):
            if t == "mono":
                top = "." if top == "e" else top
                bot = "." if bot == "e" else bot
                out.append({(False, False): " ", (True, True): "█", (True, False): "▀", (False, True): "▄"}[
                    (top != ".", bot != ".")])
            elif top == "." and bot == ".":
                out.append(" ")
            elif top == bot:
                out.append(_sgr(top, None, t) + "█\x1b[0m")
            elif bot == ".":
                out.append(_sgr(top, None, t) + "▀\x1b[0m")
            elif top == ".":
                out.append(_sgr(bot, None, t) + "▄\x1b[0m")
            else:
                out.append(_sgr(top, bot, t) + "▀\x1b[0m")
        lines.append("".join(out))
    return lines


def _visible_len(s: str) -> int:
    n, skip = 0, False
    for ch in s:
        if ch == "\x1b":
            skip = True
        elif skip and ch == "m":
            skip = False
        elif not skip:
            n += 1
    return n


def beside(art: list[str], info: list[str], gap: int = 4) -> list[str]:
    width = max(_visible_len(a) for a in art)
    top = max(0, (len(art) - len(info)) // 2)
    rows = []
    for i, a in enumerate(art):
        j = i - top
        rows.append(a + " " * (width - _visible_len(a) + gap) + (info[j] if 0 <= j < len(info) else ""))
    return rows
