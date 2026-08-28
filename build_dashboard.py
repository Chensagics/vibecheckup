#!/usr/bin/env python3
"""Stage 3: dashboard_template.html + data/stats.json -> dashboard.html

Inlines the stats so the result is a single self-contained file that opens by
double-click. Fetching a sibling JSON would be blocked by the file:// origin
policy, which is why the data is embedded rather than loaded.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard_template.html")
STATS = os.path.join(HERE, "data", "stats.json")
OUT = os.path.join(HERE, "dashboard.html")
PLACEHOLDER = "__STATS__"


def main():
    for p in (TEMPLATE, STATS):
        if not os.path.exists(p):
            print(f"FATAL: missing {p}", file=sys.stderr)
            return 1

    with open(STATS, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        stats = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"FATAL: stats.json is not valid JSON: {exc}", file=sys.stderr)
        return 1
    for key in ("schema_version", "clouds", "coverage", "totals", "activity"):
        if key not in stats:
            print(f"FATAL: stats.json is missing '{key}'", file=sys.stderr)
            return 1

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    if PLACEHOLDER not in html:
        print(f"FATAL: {TEMPLATE} has no {PLACEHOLDER} placeholder", file=sys.stderr)
        return 1

    # Two different ways raw log text escapes the host <script> block:
    # "</script" closes it early, and a "<!--" anywhere before a "<script"
    # flips the parser into its double-escaped state, where the template's own
    # "</script>" closes nothing and the rest of the page is swallowed by the
    # data block -- a silently blank dashboard. Escaping every "<" kills both.
    # The replacement below is the escape JSON already defines for it, and "<"
    # only ever occurs inside a JSON string, so the payload stays parseable and
    # JSON.parse hands the original text back unchanged.
    safe = raw.replace("<", "\\u003c")
    html = html.replace(PLACEHOLDER, safe)

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {OUT} ({os.path.getsize(OUT)/1e6:.2f} MB, self-contained)")
    print(f"  schema v{stats['schema_version']} · "
          f"{stats['totals']['sessions']:,} sessions · "
          f"{stats['totals']['prompts']:,} prompts · "
          f"{len(stats['clouds']['by_project'])} project clouds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
