#!/usr/bin/env python3
"""Join the Mac's send log with the phone's push log and report delivery and wake latency.

    python3 analyze.py push-sends.jsonl push-probe.jsonl
"""

import argparse
import bisect
import json
from collections import defaultdict
from datetime import datetime


def read(path):
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def secs(v):
    if v is None:
        return "-"
    return f"{v:.0f}s" if v < 120 else f"{v / 60:.0f}m"


def time_of_day(t):
    h = datetime.fromtimestamp(t).hour
    return "overnight 0-7" if h < 7 else "day 7-19" if h < 19 else "evening 19-24"


def timeline(entries, kinds, value):
    points = sorted((e["at"], value(e)) for e in entries if e.get("kind") in kinds)
    times = [t for t, _ in points]

    def at(t, default):
        i = bisect.bisect_right(times, t)
        return points[i - 1][1] if i else default
    return at


def received_state(p):
    if p.get("app_state") == "active":
        state = "app open"
    else:
        state = "background, " + ("unlocked" if p.get("protected_data") else "locked")
        if p.get("uptime", 1e9) < 30:
            state += ", relaunched"
    return state + (", low power" if p.get("low_power") else "")


def table(title, rows):
    print(f"\n{title}")
    print(f"  {'':44} {'sent':>5} {'got':>5} {'rate':>5} {'<=10s':>6} {'<=60s':>6} {'p50':>5} {'p90':>5} {'max':>5}")
    for name, items in sorted(rows.items()):
        lat = [x for x in items if x is not None]
        n = len(items)
        fast = lambda s: f"{sum(1 for x in lat if x <= s) / n:.0%}"
        print(f"  {name:44} {n:5} {len(lat):5} {len(lat) / n:5.0%} {fast(10):>6} {fast(60):>6}"
              f" {secs(pct(lat, .5)):>5} {secs(pct(lat, .9)):>5} {secs(max(lat) if lat else None):>5}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sends")
    p.add_argument("phone")
    p.add_argument("--window", type=float, default=24, help="hours after a send to still count it (default 24)")
    args = p.parse_args()

    sends = read(args.sends)
    phone = read(args.phone)
    accepted = [s for s in sends if s.get("status") == 200]
    refused = len(sends) - len(accepted)

    pushes = [e for e in phone if e.get("kind") == "push" and e.get("id")]
    first = {}
    for e in sorted(pushes, key=lambda e: e["at"]):
        first.setdefault(e["id"], e)
    condition = timeline(phone, {"mark"}, lambda e: e["condition"])
    open_at = timeline(phone, {"foreground", "background", "launch"}, lambda e: e["kind"] == "foreground")

    by_condition, by_time, by_state = defaultdict(list), defaultdict(list), defaultdict(list)
    remaining = []
    for s in accepted:
        got = first.get(s["id"])
        lat = got["at"] - s["sent_at"] if got and got["at"] - s["sent_at"] <= args.window * 3600 else None
        cond = "app open" if open_at(s["sent_at"], False) else condition(s["sent_at"], "normal")
        by_condition[cond].append(lat)
        if cond == "normal":
            by_time[time_of_day(s["sent_at"])].append(lat)
        if lat is not None:
            by_state[received_state(got)].append(lat)
            if "time_remaining" in got:
                remaining.append(got["time_remaining"])

    if not accepted:
        print("No pushes accepted by APNs yet.")
        return
    start, end = accepted[0]["sent_at"], accepted[-1]["sent_at"]
    gaps = sorted(b["sent_at"] - a["sent_at"] for a, b in zip(accepted, accepted[1:]))
    print(f"{len(accepted)} pushes accepted by APNs over {(end - start) / 3600:.1f} hours"
          f" ({refused} refused), median gap {secs(pct(gaps, .5))}")
    print(f"{len(first)} arrived, {len(pushes) - len(first)} duplicates,"
          f" {len(set(first) - {s['id'] for s in sends})} not in the send log")
    if remaining:
        print(f"Background run time left on arrival: median {secs(pct(remaining, .5))}, min {secs(min(remaining))}")

    all_lat = [x for v in by_condition.values() for x in v]
    table("Overall", {"all": all_lat})
    table("By condition at send time", by_condition)
    table("Normal condition, app closed, by time of day (Mac's time zone)", by_time)
    table("By state on arrival (delivered pushes only)", by_state)


if __name__ == "__main__":
    main()
