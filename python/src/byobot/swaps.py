"""Swaps ranked by the muscles they share and the equipment you have: the rules the app's Swaps.swift uses, so an
agent suggests the same swaps. schema/fixtures/v1/swaps.json holds cases both run."""

from __future__ import annotations

from typing import Any

KINDS = {"strength", "cardio", "conditioning", "mobility", "core"}


def needs(activity: dict) -> list[str]:
    """Its load type and the kit it needs; your own body you always have."""
    load_type = activity.get("load_type")
    return ([load_type] if load_type is not None and load_type != "bodyweight" else []) + \
        list(activity.get("equipment") or [])


def alternatives(original: dict, library: list[dict], equipment: set[str] | None = None,
                 limit: int = 8) -> list[dict[str, Any]]:
    """Activities that work the same muscles, ones you have the equipment for first. `equipment` None means you
    have everything. Each is {activity, score, missing_equipment}."""
    targets = original.get("targets") or {}
    p, s = set(targets.get("main") or []), set(targets.get("synergists") or [])
    if not p:
        return []
    kinds = set(original.get("tags") or []) & KINDS
    out = []
    for candidate in library:
        if candidate["id"] == original["id"]:
            continue
        theirs = candidate.get("targets") or {}
        cp, cs = set(theirs.get("main") or []), set(theirs.get("synergists") or [])
        if p.isdisjoint(cp) or kinds and kinds.isdisjoint(candidate.get("tags") or []):
            continue
        shared = 3 * len(p & cp)
        support = len(p & cs) + len(s & cp) + 0.5 * len(s & cs)
        penalty = 1.5 * len(cp - p) + len(p - cp)
        score = shared + support - penalty
        if score <= 0:
            continue
        missing = [n for n in needs(candidate) if n not in equipment] if equipment is not None else []
        out.append({"activity": candidate, "score": score, "missing_equipment": missing})
    out.sort(key=lambda a: (bool(a["missing_equipment"]), -a["score"], a["activity"]["name"]))
    return out[:limit]
