#!/usr/bin/env python3
"""Stage 3: dashboard_template.html + data/stats.json -> dashboard.html

Inlines the stats so the result is a single self-contained file that opens by
double-click. Fetching a sibling JSON would be blocked by the file:// origin
policy, which is why the data is embedded rather than loaded.

Because the stats are inlined verbatim, dashboard.html carries everything
stats.json carries: project and worktree directory names, raw error text and
the shell commands the agents ran. That file is a private artifact.

`--scrub` builds a second file, dashboard-shareable.html, from a filtered copy
of the same stats: the facets that hold strings harvested from your machine are
removed, and what is left is aggregated vocabulary and counts. See scrub_stats()
for the exact rules and README.md for what that does and does not promise.
"""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "dashboard_template.html")
STATS = os.path.join(HERE, "data", "stats.json")
OUT = os.path.join(HERE, "dashboard.html")
SCRUB_OUT = os.path.join(HERE, "dashboard-shareable.html")
PLACEHOLDER = "__STATS__"

# Shown wherever a removed facet used to render, so the gap reads as a choice
# rather than as "you never ran a shell command". Kept short: the same string
# is laid out by the word-cloud placer, which drops anything too wide to fit.
NOTICE = "omitted in share mode"
# The template renders generated_at and spend.estimates_note as free text, so
# these are the two places a marker can reach the page without touching it.
BANNER = ("SHARE COPY — scrubbed: no project names, error text, "
          "shell commands or account names")

# Facets whose entries are strings lifted off the machine rather than typed.
# Replaced wholesale by the notice. Everything else in a bucket that looks like
# a term list -- prose_user, prose_assistant, phrases_*, distinctive_*, tools,
# extensions today -- is kept but filtered entry by entry. Filtering by default
# rather than by allowlist means a facet added to analyze.py later arrives here
# scrubbed instead of arriving here unnoticed.
DROPPED_FACETS = ("commands", "errors")

# A dot between two word characters means an identifier, not prose: a filename
# (combat_screen.gd), a hostname (mejanreteam.example.com), a bundle id
# (com.acme.finn) or an attribute path (gamestore.advanceday).
DOTTED = re.compile(r"\w\.\w")
# Paths, handles and URLs. None survive the cleaner today; the rule is here so
# that a change upstream cannot quietly start leaking them.
PATHISH = re.compile(r"[\\/@]|https?:")


def account_names(extra=()):
    """Names that identify the person this machine belongs to.

    Matched as substrings, so chensagi also catches chensagics and chensagi's.
    Short names are skipped -- a two-letter login would match half the corpus.
    """
    names = set(extra)
    try:
        names.add(getpass.getuser())
    except Exception:            # no passwd entry, no LOGNAME: not fatal here
        pass
    home = os.path.basename(os.path.expanduser("~").rstrip(os.sep))
    if home:
        names.add(home)
    return {n.strip().lower() for n in names if n and len(n.strip()) >= 3}


def project_names(stats):
    """Every project/directory name stats.json actually carries as a string."""
    names = set()
    clouds = stats.get("clouds") or {}
    for key in (clouds.get("by_project") or {}):
        if isinstance(key, str):
            names.add(key)
    for row in ((stats.get("activity") or {}).get("top_projects") or []):
        if isinstance(row, dict) and isinstance(row.get("t"), str):
            names.add(row["t"])
    return names


def _name_patterns(names):
    """Word-boundary patterns for each name and for its separator-free form.

    A repo called finn--claude-worktrees-native-ota is tokenised into prose as
    the phrase "finn claude worktrees native ota", so both spellings have to be
    matched. Segments are deliberately *not* matched on their own: half of
    dyslexic-type is "type", the most common word in the corpus.
    """
    pats = []
    seen = set()
    for name in names:
        base = (name or "").strip().lower()
        for form in (base, re.sub(r"[^a-z0-9]+", " ", base).strip()):
            if len(form) < 2 or form in seen:
                continue
            seen.add(form)
            pats.append(re.compile(r"(?<![a-z0-9])" + re.escape(form)
                                   + r"(?![a-z0-9])"))
    return pats


def _leaks(text, accounts, projects):
    """True if this cloud entry carries a string from the machine, not prose."""
    t = (text or "").strip().lower()
    if not t:
        return False
    if DOTTED.search(t) or PATHISH.search(t):
        return True
    if any(a in t for a in accounts):
        return True
    return any(p.search(t) for p in projects)


def scrub_stats(stats, extra_names=()):
    """Return (shareable stats, report).

    What comes out is aggregated vocabulary and counts. What is removed:

      * clouds.by_project        -- the whole facet: every repo name, each with
                                    a cloud of its own distinctive terms.
      * every `errors` list      -- raw failure text, quoted from the logs.
      * every `commands` list    -- the shell commands the agents ran.
      * activity.top_projects    -- names replaced by "project 01", counts kept.
      * activity.session_lengths -- session ids replaced by "session 01".
      * any single term or phrase in a kept cloud that matches an account name,
        a project name, or looks like a filename, hostname or path.

    What stays: the global and per-tool prose clouds, phrases, per-month
    clouds, trends, spend, the activity histograms and Wrapped. Those are your
    own words, so a codename you typed all year still shows up -- the scrub
    removes strings harvested from the machine, not vocabulary.
    """
    out = copy.deepcopy(stats)
    accounts = account_names(extra_names)
    projects = _name_patterns(project_names(stats))
    report = {"accounts": sorted(accounts),
              "projects": len(project_names(stats)),
              "terms_dropped": 0, "facets_dropped": 0, "buckets": 0}

    def notice_list():
        report["facets_dropped"] += 1
        return [{"t": NOTICE, "n": 0}]

    def term_list(items):
        return (isinstance(items, list) and items
                and all(isinstance(d, dict) and "t" in d for d in items))

    def scrub_bucket(bucket):
        if not isinstance(bucket, dict):
            return
        report["buckets"] += 1
        for facet, items in list(bucket.items()):
            if facet in DROPPED_FACETS:
                bucket[facet] = notice_list()
            elif term_list(items):
                kept = [d for d in items
                        if not _leaks(d.get("t"), accounts, projects)]
                report["terms_dropped"] += len(items) - len(kept)
                bucket[facet] = kept

    clouds = out.get("clouds")
    if isinstance(clouds, dict):
        scrub_bucket(clouds.get("global"))
        for group in ("by_tool", "by_month"):
            for bucket in (clouds.get(group) or {}).values():
                scrub_bucket(bucket)
        # Emptied, not replaced by a placeholder entry: the template reads
        # share_mode and says so in the tab's own sub-line, so a sentinel card
        # here would state the same thing a second time.
        if "by_project" in clouds:
            report["facets_dropped"] += 1
            clouds["by_project"] = {}

    trends = out.get("trends")
    if isinstance(trends, dict):
        for key in ("rising", "fading"):
            items = trends.get(key)
            if isinstance(items, list):
                kept = [d for d in items
                        if not (isinstance(d, dict)
                                and _leaks(d.get("t"), accounts, projects))]
                report["terms_dropped"] += len(items) - len(kept)
                trends[key] = kept

    wrapped = out.get("wrapped")
    if isinstance(wrapped, dict):
        words = wrapped.get("top_words")
        if isinstance(words, list):
            kept = []
            for d in words:
                term = d[0] if isinstance(d, (list, tuple)) and d else (
                    d.get("t") if isinstance(d, dict) else d)
                if _leaks(term if isinstance(term, str) else "",
                          accounts, projects):
                    report["terms_dropped"] += 1
                else:
                    kept.append(d)
            wrapped["top_words"] = kept
        # The deck skips a slide whose stat is missing rather than faking it,
        # so dropping the key is the supported way to remove one.
        for key in ("top_phrase", "rising_word"):
            val = wrapped.get(key)
            if isinstance(val, str) and _leaks(val, accounts, projects):
                report["terms_dropped"] += 1
                wrapped.pop(key)

    activity = out.get("activity")
    if isinstance(activity, dict):
        rows = activity.get("top_projects")
        if isinstance(rows, list):
            report["facets_dropped"] += 1
            for i, row in enumerate(rows, 1):
                if isinstance(row, dict):
                    row["t"] = "project %02d" % i
        rows = activity.get("session_lengths")
        if isinstance(rows, list):
            report["facets_dropped"] += 1
            for i, row in enumerate(rows, 1):
                if isinstance(row, dict):
                    row["t"] = "session %02d" % i

    # A real flag, so the page can say what it is above the fold rather than
    # only in the footer, and so the Projects tab can stop advertising repos it
    # is no longer showing.
    out["share_mode"] = True

    # Two markers the template already prints as free text: the footer line and
    # the Spend tab's estimate note.
    stamp = out.get("generated_at")
    out["generated_at"] = f"{stamp} · {BANNER}" if stamp else BANNER
    spend = out.get("spend")
    if isinstance(spend, dict):
        note = spend.get("estimates_note")
        spend["estimates_note"] = f"{note} · {BANNER}" if note else BANNER

    return out, report


def render(stats_json, template):
    """Inline a stats payload into the template.

    Two different ways raw log text escapes the host <script> block:
    "</script" closes it early, and a "<!--" anywhere before a "<script"
    flips the parser into its double-escaped state, where the template's own
    "</script>" closes nothing and the rest of the page is swallowed by the
    data block -- a silently blank dashboard. Escaping every "<" kills both.
    The replacement below is the escape JSON already defines for it, and "<"
    only ever occurs inside a JSON string, so the payload stays parseable and
    JSON.parse hands the original text back unchanged.
    """
    return template.replace(PLACEHOLDER, stats_json.replace("<", "\\u003c"))


def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="Inline data/stats.json into dashboard_template.html.")
    ap.add_argument("--scrub", action="store_true",
                    help="also write a shareable copy with project names, "
                         "error text and shell commands removed")
    ap.add_argument("--out", metavar="PATH",
                    help="where the scrubbed copy goes "
                         "(default dashboard-shareable.html)")
    ap.add_argument("--scrub-name", metavar="NAME", action="append", default=[],
                    help="an extra name to strip from the scrubbed copy "
                         "(repeatable); the local account name is stripped "
                         "anyway")
    return ap.parse_args(list(argv))


def main(argv=()):
    args = parse_args(argv)
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

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(render(raw, html))
    print(f"wrote {OUT} ({os.path.getsize(OUT)/1e6:.2f} MB, self-contained)")
    print(f"  schema v{stats['schema_version']} · "
          f"{stats['totals']['sessions']:,} sessions · "
          f"{stats['totals']['prompts']:,} prompts · "
          f"{len(stats['clouds']['by_project'])} project clouds")
    print("  holds project names, error text and shell commands — keep it private")

    if args.scrub:
        shared, report = scrub_stats(stats, args.scrub_name)
        dest = args.out or SCRUB_OUT
        payload = json.dumps(shared, ensure_ascii=False, separators=(",", ":"))
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(render(payload, html))
        print(f"wrote {dest} ({os.path.getsize(dest)/1e6:.2f} MB, shareable)")
        print(f"  dropped {report['facets_dropped']} facets "
              f"({report['projects']} project names, every errors and commands "
              f"list) and {report['terms_dropped']:,} terms across "
              f"{report['buckets']} clouds")
        print("  the remaining clouds are still your own words — read them "
              "before you hand this over")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
