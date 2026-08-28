#!/usr/bin/env python3
"""Save a point-in-time copy of the dashboard to snapshots/ (gitignored).

dashboard.html is self-contained -- the stats are embedded in it -- so a single
copied file is a complete, permanently viewable snapshot. The matching
stats.json is saved alongside it so a snapshot can be re-analysed later.

Snapshots hold your real session vocabulary and project names. They stay out of
git by design; see .gitignore.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "dashboard.html")
STATS = os.path.join(HERE, "data", "stats.json")
SNAPS = os.path.join(HERE, "snapshots")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="",
                    help="optional suffix, e.g. --label before-refactor")
    ap.add_argument("--list", action="store_true", help="list existing snapshots")
    args = ap.parse_args()

    if args.list:
        if not os.path.isdir(SNAPS):
            print("no snapshots yet")
            return 0
        rows = sorted(f for f in os.listdir(SNAPS) if f.endswith(".html"))
        if not rows:
            print("no snapshots yet")
            return 0
        for f in rows:
            p = os.path.join(SNAPS, f)
            print(f"  {f}  ({os.path.getsize(p)/1e6:.2f} MB)")
        print(f"\n{len(rows)} snapshot(s) in {SNAPS}")
        return 0

    if not os.path.exists(DASH):
        print("FATAL: dashboard.html not found. Run build_dashboard.py first.",
              file=sys.stderr)
        return 1

    stamp = time.strftime("%Y-%m-%d-%H%M")
    label = ("-" + args.label.strip().replace(" ", "-")) if args.label else ""
    name = f"lexicon-{stamp}{label}"
    os.makedirs(SNAPS, exist_ok=True)

    out_html = os.path.join(SNAPS, name + ".html")
    shutil.copy2(DASH, out_html)
    saved = [out_html]

    if os.path.exists(STATS):
        out_json = os.path.join(SNAPS, name + ".stats.json")
        shutil.copy2(STATS, out_json)
        saved.append(out_json)

    try:
        with open(STATS, encoding="utf-8") as fh:
            s = json.load(fh)
        summary = (f"{s['totals']['sessions']:,} sessions · "
                   f"{s['totals']['prompts']:,} prompts · "
                   f"{s['totals']['events']:,} events")
    except (OSError, ValueError, KeyError):
        summary = "(stats.json unavailable)"

    for p in saved:
        print(f"wrote {os.path.relpath(p, HERE)} ({os.path.getsize(p)/1e6:.2f} MB)")
    print(f"  {summary}")
    print(f"\nSelf-contained -- open it any time: file://{out_html}")
    print("snapshots/ is gitignored and never leaves this machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
