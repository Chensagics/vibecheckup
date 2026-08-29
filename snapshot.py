#!/usr/bin/env python3
"""Save a point-in-time copy of the dashboard to snapshots/ (gitignored).

dashboard.html is self-contained -- the stats are embedded in it -- so a single
copied file is a complete, permanently viewable snapshot. The matching
stats.json is saved alongside it so a snapshot can be re-analysed later.

That completeness cuts both ways. A snapshot of dashboard.html is a byte copy:
it carries your project and worktree names, the raw error text and the shell
commands, exactly as the dashboard does. Keeping it out of git is not the same
as it being safe to send -- git is one way a file travels, not the only one.

`--scrub` snapshots dashboard-shareable.html instead (build it first with
`python3 build_dashboard.py --scrub`) and leaves stats.json behind.

A snapshot outlives the code that built it, so what got redacted out of it is a
property of that build and not of this checkout. Every page carries the
redaction-schema version it was built under; `--list` reads it back and marks
anything older than the rules in force today, because the fix for a snapshot
built by a leakier version is to rebuild it, and nothing about the file says so
on its own.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

from build_dashboard import HEAD_BYTES, REDACTION_SCHEMA, marker_version

HERE = os.path.dirname(os.path.abspath(__file__))
DASH = os.path.join(HERE, "dashboard.html")
SHARE = os.path.join(HERE, "dashboard-shareable.html")
STATS = os.path.join(HERE, "data", "stats.json")
SNAPS = os.path.join(HERE, "snapshots")
STALE = "PRE-REDACTION, rebuild before sharing"


def snapshot_schema(path):
    """The redaction schema a snapshot page was built under.

    0 for anything unmarked, which is the only answer available for a page
    built before the marker existed -- and those are exactly the pages that
    have to be flagged, so absence is treated as older rather than unknown.

    Only the head is read. A snapshot is a megabyte of inlined stats and the
    stamp is in the first line or two of it, so `--list` stays instant on a
    directory that has been accumulating for a year.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return marker_version(fh.read(HEAD_BYTES))
    except OSError:
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="",
                    help="optional suffix, e.g. --label before-refactor")
    ap.add_argument("--list", action="store_true", help="list existing snapshots")
    ap.add_argument("--scrub", action="store_true",
                    help="snapshot dashboard-shareable.html instead, and skip "
                         "stats.json (build it with build_dashboard.py --scrub)")
    args = ap.parse_args()

    if args.list:
        if not os.path.isdir(SNAPS):
            print("no snapshots yet")
            return 0
        rows = sorted(f for f in os.listdir(SNAPS) if f.endswith(".html"))
        if not rows:
            print("no snapshots yet")
            return 0
        stale = 0
        for f in rows:
            p = os.path.join(SNAPS, f)
            kind = "shareable" if f.endswith("-shareable.html") else "full copy"
            if snapshot_schema(p) < REDACTION_SCHEMA:
                kind += " — " + STALE
                stale += 1
            print(f"  {f}  ({os.path.getsize(p)/1e6:.2f} MB, {kind})")
        print(f"\n{len(rows)} snapshot(s) in {SNAPS}")
        if stale:
            print(f"{stale} built under redaction rules older than this "
                  f"checkout's (schema {REDACTION_SCHEMA}). Redaction is done "
                  f"at build time, so an old page keeps whatever the old rules "
                  f"let through — re-run the pipeline and snapshot again "
                  f"before handing one of these to anybody.")
        return 0

    src = SHARE if args.scrub else DASH
    if not os.path.exists(src):
        how = ("python3 build_dashboard.py --scrub" if args.scrub
               else "build_dashboard.py")
        print(f"FATAL: {os.path.basename(src)} not found. Run {how} first.",
              file=sys.stderr)
        return 1

    stamp = time.strftime("%Y-%m-%d-%H%M")
    label = ("-" + args.label.strip().replace(" ", "-")) if args.label else ""
    suffix = "-shareable" if args.scrub else ""
    name = f"lexicon-{stamp}{label}{suffix}"
    os.makedirs(SNAPS, exist_ok=True)

    out_html = os.path.join(SNAPS, name + ".html")
    shutil.copy2(src, out_html)
    saved = [out_html]

    # Only for a full snapshot: stats.json is the corpus the page was built
    # from, so pairing it with a scrubbed page would undo the scrub.
    if not args.scrub and os.path.exists(STATS):
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
    if snapshot_schema(out_html) < REDACTION_SCHEMA:
        print(f"  {STALE}: the page this was copied from was built under "
              f"redaction rules older than schema {REDACTION_SCHEMA}. "
              f"Re-run build_dashboard.py and snapshot again.")
    print(f"\nSelf-contained -- open it any time: file://{out_html}")
    if args.scrub:
        print("Copied from the scrubbed build: no project names, error text or")
        print("shell commands. The clouds are still your own words, so read them")
        print("before you hand it over.")
    else:
        print("This is a byte copy of dashboard.html: project names, error text")
        print("and shell commands included. snapshots/ is gitignored, which keeps")
        print("it out of commits and nothing more -- treat the file as private.")
        print("`snapshot.py --scrub` saves the shareable build instead.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
