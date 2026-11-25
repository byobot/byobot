"""Workouts seen by more than one author, read as one: your sessions and each source's, merged.

A source workout joins another workout when they overlap by at least half the shorter one, or when you said
it is the same as it. Two of your own sessions never merge. Each value comes from the best item that has it:
yours first, then sources in `source_priority` order (its `codes` override `default` for those codes); the merged
workout keeps which record gave each value.

It is recomputed for the time window a change touches, widened until no workout outside it could join.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable

DEFAULT_PRIORITY = ("whoop", "hume", "apple_health")
SAME = 0.5

DDL = """
CREATE TABLE IF NOT EXISTS occurrences (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT, start_ms INTEGER NOT NULL, end_ms INTEGER,
  offset_min INTEGER NOT NULL, local_date TEXT NOT NULL, name TEXT);
CREATE INDEX IF NOT EXISTS occurrences_start ON occurrences (start_ms);
CREATE INDEX IF NOT EXISTS occurrences_day ON occurrences (local_date);
CREATE TABLE IF NOT EXISTS occurrence_items (
  occurrence_id TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY (kind, id));
CREATE INDEX IF NOT EXISTS occurrence_items_occurrence ON occurrence_items (occurrence_id);
CREATE TABLE IF NOT EXISTS occurrence_metrics (
  occurrence_id TEXT NOT NULL, code TEXT NOT NULL, value REAL, text TEXT, unit TEXT, std_value REAL, std_unit TEXT,
  from_kind TEXT NOT NULL, from_id TEXT NOT NULL, overlay_id TEXT, PRIMARY KEY (occurrence_id, code));
"""
TABLES = ("occurrences", "occurrence_items", "occurrence_metrics")
ITEMS = {"session": ("sessions", "you"), "source_record": ("source_sessions", None)}


def span(conn: sqlite3.Connection, kind: str, rid: str) -> tuple[int, int] | None:
    """The time a record's workout covers, as it now reads."""
    if kind not in ITEMS:
        return None
    row = conn.execute(f"SELECT start_ms, coalesce(end_ms, start_ms) FROM {ITEMS[kind][0]} WHERE id = ?",
                       (rid,)).fetchone()
    return (row[0], row[1]) if row else None


def links(conn: sqlite3.Connection, kind: str, rid: str) -> set[tuple[str, str]]:
    """Records a `same_as` ties to this one, either way round."""
    found = conn.execute(
        "SELECT target_kind, target_id FROM overlays WHERE effect = 'same_as' AND with_kind = ? AND with_id = ? "
        "UNION SELECT with_kind, with_id FROM overlays WHERE effect = 'same_as' AND target_kind = ? AND target_id = ?",
        (kind, rid, kind, rid)).fetchall()
    return {(k, i) for k, i in found}


def _items_in(conn: sqlite3.Connection, lo: int, hi: int) -> set[tuple[str, str]]:
    out = set()
    for kind, (table, _) in ITEMS.items():
        out |= {(kind, r[0]) for r in conn.execute(
            f"SELECT id FROM {table} WHERE start_ms <= ? AND coalesce(end_ms, start_ms) >= ?", (hi, lo))}
    return out


def rank(source: str | None, order: list[str]) -> tuple[int, str]:
    if source is None:
        return (0, "")
    return (1 + (order.index(source) if source in order else len(order)), source)


def _item(conn: sqlite3.Connection, kind: str, rid: str, priority: list[str]) -> dict | None:
    table, author = ITEMS[kind]
    cols = "started_at, ended_at, start_ms, coalesce(end_ms, start_ms), offset_min, local_date, name"
    row = conn.execute(f"SELECT {cols}{', source' if author is None else ''} FROM {table} WHERE id = ?",
                       (rid,)).fetchone()
    if row is None:
        return None
    source = row[7] if author is None else None
    metrics = {r[0]: tuple(r[1:]) for r in conn.execute(
        "SELECT code, value, text, unit, std_value, std_unit, overlay_id FROM metrics "
        "WHERE owner_kind = ? AND owner_id = ? AND part_id = ''", (kind, rid))}
    return {"kind": kind, "id": rid, "you": author == "you", "source": source, "order": rank(source, priority),
            "row": row[:7],
            "start": row[2], "end": row[3], "metrics": metrics}


def priority(conn: sqlite3.Connection) -> tuple[list[str], dict[str, list[str]]]:
    row = conn.execute("SELECT value FROM settings WHERE id = 'source_priority'").fetchone()
    found = json.loads(row[0]) if row else {}
    if not isinstance(found, dict):
        found = {}
    return list(found.get("default") or DEFAULT_PRIORITY), dict(found.get("codes") or {})


def _overlap(a: dict, b: dict) -> bool:
    shared = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return shared > 0 and shared >= SAME * min(a["end"] - a["start"], b["end"] - b["start"])


def resolve(conn: sqlite3.Connection, touched: Iterable[tuple[str, str]], spans: Iterable[tuple[int, int]]) -> None:
    """Recompute the merged workouts around `touched` records and the `spans` they covered or cover now."""
    items = set(touched)
    windows = list(spans)
    while True:
        grown = set(items)
        for lo, hi in windows:
            grown |= _items_in(conn, lo, hi)
        for kind, rid in list(grown):
            grown |= links(conn, kind, rid)
        held = {r[0] for k, i in grown for r in conn.execute(
            "SELECT occurrence_id FROM occurrence_items WHERE kind = ? AND id = ?", (k, i))}
        for oid in held:
            grown |= {(k, i) for k, i in conn.execute(
                "SELECT kind, id FROM occurrence_items WHERE occurrence_id = ?", (oid,))}
        found = [s for k, i in grown if (s := span(conn, k, i))]
        windows = windows + found
        if grown == items:
            break
        items = grown
    for oid in held:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'occurrences' else 'occurrence_id'} = ?",
                         (oid,))
    default, codes = priority(conn)
    live = [it for k, i in items if k in ITEMS and (it := _item(conn, k, i, default))]
    tied = {(it["kind"], it["id"]): links(conn, it["kind"], it["id"]) for it in live}
    clusters: list[list[dict]] = []
    for it in sorted(live, key=lambda x: (x["start"], x["id"])):
        home = None
        for c in clusters:
            keys = {(x["kind"], x["id"]) for x in c}
            linked = bool(tied[(it["kind"], it["id"])] & keys)
            both_yours = it["you"] and any(x["you"] for x in c)
            if linked or (not both_yours and any(_overlap(it, x) for x in c)):
                home = c
                break
        if home is None:
            clusters.append([it])
        else:
            home.append(it)
    for c in clusters:
        _write(conn, sorted(c, key=lambda x: (x["order"], x["start"], x["id"])), codes)


def _write(conn: sqlite3.Connection, ranked: list[dict], codes: dict[str, list[str]]) -> None:
    best = ranked[0]
    oid = best["id"]
    conn.execute("INSERT INTO occurrences VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (oid, *best["row"][:2], best["row"][2], best["end"], *best["row"][4:7]))
    taken: dict[str, tuple] = {}
    for it in ranked:
        conn.execute("INSERT INTO occurrence_items VALUES (?, ?, ?)", (oid, it["kind"], it["id"]))
    for code in {c for it in ranked for c in it["metrics"]}:
        order = codes.get(code)
        by = sorted(ranked, key=lambda x: (rank(x["source"], order), x["start"], x["id"])) if order else ranked
        it = next(x for x in by if code in x["metrics"])
        m = it["metrics"][code]
        taken[code] = (*m[:5], it["kind"], it["id"], m[5])
    conn.executemany("INSERT INTO occurrence_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [(oid, code, *v) for code, v in taken.items()])


def clear(conn: sqlite3.Connection) -> None:
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")
