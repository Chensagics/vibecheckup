#!/usr/bin/env python3
"""Stage 1: read every local AI session log -> data/events.ndjson

Adapter failures are per-file and non-fatal. Unknown record types are counted
and reported by name, so a silent upstream format change is visible rather than
quietly dropping data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters import ADAPTERS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "events.ndjson")


class Report:
    def __init__(self):
        self.files = Counter()
        self.parsed = Counter()
        self.failed = Counter()
        self.events = Counter()
        self.bad_lines = Counter()
        self.unknown_types = defaultdict(Counter)
        self.ignored_types = defaultdict(Counter)

    def bad_line(self, tool):
        self.bad_lines[tool] += 1

    def bad_file(self, tool):
        self.failed[tool] += 1

    def unknown(self, tool, name):
        """A record type we do not handle and did not expect."""
        self.unknown_types[tool][name] += 1

    def ignored(self, tool, name):
        """A record type deliberately outside the text allowlist."""
        self.ignored_types[tool][name] += 1

    def render(self):
        w = []
        w.append("")
        w.append(f"{'tool':<14}{'files':>7}{'parsed':>8}"
                 f"{'failed':>8}{'badlines':>10}{'events':>10}")
        w.append("-" * 57)
        for tool in ADAPTERS:
            w.append(f"{tool:<14}{self.files[tool]:>7}{self.parsed[tool]:>8}"
                     f"{self.failed[tool]:>8}"
                     f"{self.bad_lines[tool]:>10}{self.events[tool]:>10}")
        w.append("-" * 57)
        w.append(f"{'TOTAL':<14}{sum(self.files.values()):>7}"
                 f"{sum(self.parsed.values()):>8}"
                 f"{sum(self.failed.values()):>8}{sum(self.bad_lines.values()):>10}"
                 f"{sum(self.events.values()):>10}")
        for tool, c in self.unknown_types.items():
            if c:
                items = ", ".join(f"{k}({v})" for k, v in c.most_common(12))
                w.append(f"\n  UNKNOWN record types [{tool}]: {items}")
        for tool, c in self.ignored_types.items():
            if c:
                items = ", ".join(f"{k}({v})" for k, v in c.most_common(8))
                w.append(f"\n  ignored (non-text) [{tool}]: {items}")
        for tool in ADAPTERS:
            if self.files[tool] == 0:
                w.append(f"\n  WARNING: {tool} found no files")
        return "\n".join(w)

    @property
    def fatal(self):
        """A source is fatal only if it found files and parsed none of them."""
        return [t for t in ADAPTERS
                if self.files[t] > 0 and self.parsed[t] == 0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", action="append", choices=list(ADAPTERS),
                    help="limit to one or more sources (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="max files per source (0 = all)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    tools = args.tool or list(ADAPTERS)
    report = Report()
    t0 = time.time()

    with open(args.out, "w", encoding="utf-8") as out:
        for tool in tools:
            mod = ADAPTERS[tool]
            files = mod.discover()
            if args.limit:
                files = files[:args.limit]
            report.files[tool] = len(files)
            for i, path in enumerate(files, 1):
                try:
                    n = 0
                    for ev in mod.iter_events(path, report):
                        out.write(ev.to_json())
                        out.write("\n")
                        n += 1
                    report.events[tool] += n
                    report.parsed[tool] += 1
                except (OSError, ValueError, KeyError, TypeError,
                        AttributeError, RecursionError) as exc:
                    report.failed[tool] += 1
                    print(f"  ! {tool}: {os.path.basename(path)}: "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                if i % 100 == 0 or i == len(files):
                    print(f"  {tool}: {i}/{len(files)} files, "
                          f"{report.events[tool]:,} events", flush=True)

    print(report.render())
    size = os.path.getsize(args.out)
    print(f"\nwrote {args.out} ({size/1e6:.1f} MB) in {time.time()-t0:.1f}s")
    fatal = report.fatal
    if fatal:
        print(f"\nFATAL: sources found files but parsed none: {', '.join(fatal)}")
        return 1
    print("\nFATAL ERRORS: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
