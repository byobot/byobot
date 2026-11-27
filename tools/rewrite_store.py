#!/usr/bin/env python3
"""Rewrite a store from before ids and codes were settled (store version 5) into one this byobot opens.

    python tools/rewrite_store.py ~/.byobot/byobot.db

Ids become lowercase letters, digits and underscores; your own activities become `x.` ids and library ones take
their new names; WHOOP's scores take its name; a `load` in %{1RM} becomes `pct_of_1rm`; an activity's equipment
splits into its load type and kit; rearrange overlays go. Events keep their ids, clocks and order. The result is
a new store (new store id, same key) checked event by event; the old file is kept beside it. Phones pair again.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from byobot import library  # noqa: E402
from byobot.core.contract import Contract, Rejected, jcs, later  # noqa: E402
from byobot.core.store import (EXPORT_VERSION, SELECT_EVENTS, STORE_VERSION, Store, _envelope,  # noqa: E402
                               _local_now, stamp)
from byobot.util import ByobotError, uuid7  # noqa: E402

OLD_VERSION = 5
KEPT = ".before_rewrite"
LIBRARY_RENAMES = {
    "barbell_shrugs": "barbell_shrug",
    "dumbbell_shrugs": "dumbbell_shrug",
    "dumbbell_wrist_curls": "dumbbell_wrist_curl",
    "face_pulls": "face_pull",
    "kettlebell_swings": "kettlebell_swing",
    "assisted_dips": "assisted_dip",
    "hyperextension_with_dumbbells": "dumbbell_hyperextension",
    "flat_dumbbell_fly": "dumbbell_fly",
    "incline_dumbbell_fly": "dumbbell_incline_fly",
    "seated_dumbbell_incline_curl": "dumbbell_incline_curl",
    "single_arm_dumbbell_row": "dumbbell_one_arm_row",
    "single_leg_dumbbell_calf_raise_with_block": "dumbbell_single_leg_calf_raise_with_block",
    "cable_single_arm_rear_delt_fly": "cable_one_arm_rear_delt_fly",
    "dumbbell_single_arm_kickback": "dumbbell_one_arm_kickback",
    "cable_curl_straight_bar": "cable_straight_bar_curl",
    "cable_pushdown_with_rope": "cable_rope_pushdown",
    "cable_pushdown_with_straight_bar": "cable_straight_bar_pushdown",
    "cable_triceps_pushdown_v_bar": "cable_v_bar_pushdown",
    "cable_overhead_triceps_extension_with_rope": "cable_rope_overhead_triceps_extension",
    "leg_press_plate_loaded": "plate_loaded_leg_press",
    "single_leg_press_plate_loaded": "plate_loaded_single_leg_press",
    "barbell_bent_over_row_45_degrees": "barbell_bent_over_row",
    "close_grip_bench_press": "barbell_close_grip_bench_press",
    "ez_barbell_curl": "ez_bar_curl",
    "ez_barbell_incline_triceps_extension": "ez_bar_incline_triceps_extension",
    "ez_barbell_lying_triceps_extension": "ez_bar_lying_triceps_extension",
    "ez_barbell_preacher_curl": "ez_bar_preacher_curl",
    "ez_barbell_spider_curl": "ez_bar_spider_curl",
}
CODES = {"strain": "whoop_strain", "recovery_score": "whoop_recovery_score",
         "sleep_performance": "whoop_sleep_performance"}
KIT = {"plates": ["plate_loaded_machine"], "ez_barbell": ["barbell", "ez_bar"],
       "weighted_bodyweight": ["bodyweight"]}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


def code(c: str) -> str:
    return "x." + slug(c[2:]) if c.startswith("x.") else CODES.get(c, c)


def metrics(m: dict) -> dict:
    out = {}
    for c, measure in m.items():
        if c == "load" and isinstance(measure, dict) and measure.get("unit") == "%{1RM}":
            c, measure = "pct_of_1rm", {k: v for k, v in measure.items() if k != "unit"}
        else:
            c = code(c)
        if c in out:
            raise ByobotError(f"two metrics become {c}")
        out[c] = measure
    return out


def kit(items: list) -> list:
    out = []
    for item in items:
        for new in KIT.get(item, [slug(item)]):
            if new not in out:
                out.append(new)
    return out


class Rewrite:
    def __init__(self, contract: Contract):
        self.contract = contract
        self.library = {a["id"] for a in library.activities(ROOT / "library")}
        self.load_types = set(library.vocabulary(ROOT / "library")["load_types"])
        self.overlays: dict[str, str] = {}  # old overlay id -> new one
        self.dropped: set[tuple[str, str]] = set()

    def activity(self, i: str) -> str:
        i = LIBRARY_RENAMES.get(i, i)
        if i in self.library:
            return i
        return "x." + slug(i[2:] if i.startswith("x.") else i)

    def rid(self, kind: str, i: str) -> str:
        if kind == "activity":
            return self.activity(i)
        if kind in ("location", "setting"):
            return slug(i)
        if kind == "overlay":
            return self.overlays.get(i, i)
        return i

    def ref(self, r: dict) -> dict:
        return {**r, "id": self.rid(r["kind"], r["id"])}

    def plan(self, events: list[dict]) -> None:
        """Overlays: drop rearranges, and re-derive the id of one whose target took a new id."""
        for e in events:
            kind, i, p = e["record"]["kind"], e["record"]["id"], e["payload"]
            if kind != "overlay" or e["op"] != "put" or "effect" not in p:
                continue
            if p["effect"] == "rearrange":
                self.dropped.add((kind, i))
                continue
            target = self.ref(p["target"])
            new = self.contract.derived("overlay", [p["effect"], target["kind"], target["id"]])
            if new != i:
                self.overlays[i] = new

    def payload(self, kind: str, p: dict) -> dict:
        p = dict(p)
        if "links" in p:
            p["links"] = {k: [self.ref(r) for r in v] if isinstance(v, list) else self.ref(v)
                          for k, v in p["links"].items()}
        if "metrics" in p:
            p["metrics"] = metrics(p["metrics"])
        if "activity_id" in p:
            p["activity_id"] = self.activity(p["activity_id"])
        if "location_id" in p:
            p["location_id"] = slug(p["location_id"])
        if kind == "activity":
            for key in ("tags", "movement_type"):
                if key in p:
                    p[key] = [slug(t) for t in p[key]] if isinstance(p[key], list) else slug(p[key])
            if "targets" in p:
                p["targets"] = {k: [slug(m) for m in v] for k, v in p["targets"].items()}
            if "default_metrics" in p:
                p["default_metrics"] = [code(c) for c in p["default_metrics"]]
            if "equipment" in p and "load_type" not in p:
                items = kit(p.pop("equipment"))
                loads = [i for i in items if i in self.load_types]
                # plates beside a barbell are its weights, not a machine
                load = next((i for i in loads if i != "plate_loaded_machine"), loads[0] if loads else None)
                if load is None and "smith_machine" in items:
                    load = "plate_loaded_machine"
                if load:
                    p["load_type"] = load
                rest = [i for i in items if i not in self.load_types]
                if rest:
                    p["equipment"] = rest
        if kind == "planned_session":
            p["blocks"] = [{**b, **({"metrics": metrics(b["metrics"])} if "metrics" in b else {}),
                            "efforts": [self.payload("planned_effort", pe) for pe in b["efforts"]]}
                           for b in p["blocks"]]
        if kind == "observation" and "code" in p:
            p["code"] = code(p["code"])
        if kind == "overlay":
            p["target"] = self.ref(p["target"])
            if "with" in p:
                p["with"] = self.ref(p["with"])
            if "fields" in p:
                p["fields"] = self.payload("fields", p["fields"])
        return p

    def setting(self, i: str, value):
        if i == "current_location" and isinstance(value, str):
            return slug(value)
        if i == "equipment" and isinstance(value, list):
            return kit(value)
        if i == "source_priority" and isinstance(value, dict):
            return {**value, **({"codes": {code(c): v for c, v in value["codes"].items()}} if "codes" in value else {})}
        return value

    def event(self, e: dict) -> dict | None:
        kind, i = e["record"]["kind"], e["record"]["id"]
        if (kind, i) in self.dropped:
            return None
        if kind == "source_record" or later(e):
            return e
        e = {**e, "record": {"kind": kind, "id": self.rid(kind, i)}}
        p = e["payload"]
        if e["op"] == "put" and p:
            p = self.payload(kind, p)
            if kind == "setting" and "value" in p:
                p["value"] = self.setting(e["record"]["id"], p["value"])
            e["payload"] = p
        return e


def read(path: Path) -> tuple[dict, list[dict]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == STORE_VERSION:
            raise ByobotError(f"{path} is already rewritten")
        if version != OLD_VERSION:
            raise ByobotError(f"{path} is store version {version}; this rewrites version {OLD_VERSION} only")
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        events = [_envelope(r) for r in conn.execute(f"{SELECT_EVENTS} ORDER BY e.seq")]
    finally:
        conn.close()
    return meta, events


def rewrite(meta: dict, events: list[dict], contract: Contract) -> tuple[list[dict], dict]:
    r = Rewrite(contract)
    r.plan(events)
    out, owners, errors = [], {}, []
    for e in events:
        new = r.event(e)
        if new is None:
            continue
        key, was = (new["record"]["kind"], new["record"]["id"]), e["record"]["id"]
        if owners.setdefault(key, was) != was:
            errors.append(f"{owners[key]} and {was} both become {key[0]} {key[1]}")
        try:
            contract.check(new)
        except Rejected as err:
            errors.append(f"{key[0]} {key[1]} event {new['id']}: {err}")
        out.append(new)
    if errors:
        raise ByobotError("can't rewrite:\n  " + "\n  ".join(sorted(set(errors))[:50]))
    renamed = sorted({f"{k} {o} -> {n}" for (k, n), o in owners.items() if n != o})
    return out, {"events": len(out), "dropped": len(events) - len(out), "renamed": renamed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path, help="The store, such as ~/.byobot/byobot.db.")
    args = parser.parse_args(argv)
    path = args.path.expanduser()
    kept, fresh = path.with_name(path.name + KEPT), path.with_name(path.name + ".rewritten")
    if not path.is_file():
        raise SystemExit(f"no file {path}")
    if kept.exists() or fresh.exists():
        raise SystemExit(f"{kept} or {fresh} is in the way; move it aside first")
    contract = Contract.shipped()
    try:
        meta, events = read(path)
        out, summary = rewrite(meta, events, contract)
        header = {"byobot_export": EXPORT_VERSION, "schema_version": 1, "store_id": uuid7(),
                  "store_key": meta["store_key"], "exported_at": stamp(_local_now()), "events": len(out)}
        with Store(fresh, contract=contract, device_id=meta["device_id"]) as store:
            store.restore([jcs(header)] + [jcs(e) for e in out])
            summary["store_id"] = store.store_id
    except ByobotError as err:
        for f in (fresh, fresh.with_name(fresh.name + "-wal"), fresh.with_name(fresh.name + "-shm")):
            f.unlink(missing_ok=True)
        raise SystemExit(str(err))
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(kept.with_name(kept.name + suffix))
    path.rename(kept)
    fresh.rename(path)
    print(json.dumps({**summary, "kept": str(kept)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
