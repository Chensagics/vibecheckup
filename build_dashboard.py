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

Both files are stamped with REDACTION_SCHEMA, the version of those rules. A
dated copy of a dashboard outlives the code that built it, so `snapshot.py
--list` reads the stamp back and says so when a snapshot predates the rules in
force today.
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
# (com.acme.finn) or an attribute path (gamestore.advanceday). It is a rule
# about the DOT, and nothing else: an undotted hostname, org or container name
# (buildbox, acmecorp-prod-eu) reads as prose here and survives.
DOTTED = re.compile(r"\w\.\w")
# Paths, handles and URLs. None survive the cleaner today; the rule is here so
# that a change upstream cannot quietly start leaking them.
PATHISH = re.compile(r"[\\/@]|https?:")

# `mcp__<server>__<tool>` in the tools facet names a third-party MCP server the
# user connected. That server list is configuration read off the machine, not
# vocabulary the user typed, and it is a list of vendors: mcp__meta-ads__*
# asserts "this person runs Meta ad accounts". The server half goes. The tool
# half stays, because it says what was DONE -- ui_tap, execute_sql -- and on a
# machine that leans on MCP it is over half the Tools cloud for its main agent.
MCP = re.compile(r"^mcp__(.+?)__(.+)$", re.I)
MCP_KEPT = "mcp:"

# The redaction rules a page was built under. Bump it whenever those rules
# change -- a string that used to survive stops surviving, or a facet stops
# being kept -- so that a page built by the older, leakier code can be told
# apart from a current one. Every build stamps it as an HTML comment OUTSIDE
# the inlined payload, which is what keeps dashboard.html a byte-for-byte copy
# of stats.json. snapshot.py --list reads it back; a page with no marker at all
# predates the marker and so predates every rule below.
REDACTION_SCHEMA = 1
MARKER = "vibecheckup-redaction-schema"
# Anchored on the comment delimiters: render() escapes every "<" in the
# payload, so a "<!--" cannot occur inside the inlined data. Log text that
# happens to contain the marker word therefore cannot forge a version.
RE_MARKER = re.compile(r"<!--\s*" + re.escape(MARKER) + r":\s*(\d+)\s*-->")
# The stamp goes at the top, straight after the opening tags: reading a
# snapshot's first few kilobytes is then enough to date it, and the marker
# stays well clear of the data block, where a "<!--" would be a parser bug.
RE_HEAD = re.compile(r"^\s*(?:<!doctype[^>]*>\s*)?(?:<html[^>]*>\s*)?", re.I)
# How much of a built page has to be read to find the stamp. Generous by two
# orders of magnitude; a page that somehow hides it deeper reads as unstamped,
# which is the safe direction to be wrong in.
HEAD_BYTES = 8192


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


def strip_mcp_server(term):
    """mcp__meta-ads__ads_get_ad_entities -> mcp:ads_get_ad_entities.

    Anything that is not an `mcp__server__tool` triple comes back unchanged.
    """
    m = MCP.match((term or "").strip())
    return MCP_KEPT + m.group(2) if m else term


def _merge_terms(items):
    """Fold entries that became identical once the server name was removed.

    Two servers can expose the same verb, and a cloud with the same word in it
    twice renders it twice. Counts add; the surviving entry keeps the fields of
    the first (highest-count) of the pair, and the list is re-sorted because
    the fold can move a term up.
    """
    out, by_term = [], {}
    for d in items:
        seen = by_term.get(d.get("t"))
        if seen is None:
            by_term[d.get("t")] = d
            out.append(d)
        else:
            seen["n"] = (seen.get("n") or 0) + (d.get("n") or 0)
    out.sort(key=lambda d: -(d.get("n") or 0))
    return out


def stamp(html, version=REDACTION_SCHEMA):
    """Write the redaction-schema marker into a built page.

    Placed in the document head rather than in the data block: the unscrubbed
    page has to stay a verbatim copy of stats.json so a snapshot of it can
    still be re-analysed later. After the doctype and the <html> tag, so the
    page does not fall into quirks mode and the language is still declared
    before anything else.
    """
    at = RE_HEAD.match(html or "").end()
    return f"{html[:at]}<!-- {MARKER}: {version} -->\n{html[at:]}"


def marker_version(html):
    """The redaction schema a built page was produced under, 0 if unmarked."""
    m = RE_MARKER.search(html or "")
    return int(m.group(1)) if m else 0


def scrub_stats(stats, extra_names=()):
    """Return (shareable stats, report).

    What comes out is aggregated vocabulary and counts. What is removed:

      * clouds.by_project        -- the whole facet: every repo name, each with
                                    a cloud of its own distinctive terms.
      * every `errors` list      -- raw failure text, quoted from the logs.
      * every `commands` list    -- the shell commands the agents ran.
      * activity.top_projects    -- names replaced by "project 01", counts kept.
      * activity.session_lengths -- session ids replaced by "session 01".
      * the MCP server half of every tool name: mcp__meta-ads__ads_get_ad_
        entities becomes mcp:ads_get_ad_entities, so the page stops carrying an
        inventory of the third-party servers this machine is wired to.
      * any single term or phrase in a kept cloud that matches an account name,
        or a project name, or carries a dot between two word characters -- a
        filename, hostname, bundle id or attribute path.

    Note what the dot rule does NOT catch, because it is a rule about the dot:
    an undotted hostname, org, container or hardware name (buildbox,
    acmecorp-prod-eu, macbookpro18) is indistinguishable from prose here and
    survives. Widening it would cost real vocabulary; --redact is the answer.

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
              "terms_dropped": 0, "facets_dropped": 0, "buckets": 0,
              "servers_stripped": 0}

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
                kept, stripped = [], 0
                for d in items:
                    term = strip_mcp_server(d.get("t"))
                    if term != d.get("t"):
                        d = dict(d, t=term)
                        stripped += 1
                    # After the rename, not before: the tool half still has to
                    # answer for an account or project name inside it.
                    if not _leaks(term, accounts, projects):
                        kept.append(d)
                report["terms_dropped"] += len(items) - len(kept)
                report["servers_stripped"] += stripped
                bucket[facet] = _merge_terms(kept) if stripped else kept

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
                         "error text, shell commands and MCP server names "
                         "removed")
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
        fh.write(stamp(render(raw, html)))
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
            fh.write(stamp(render(payload, html)))
        print(f"wrote {dest} ({os.path.getsize(dest)/1e6:.2f} MB, shareable)")
        print(f"  dropped {report['facets_dropped']} facets "
              f"({report['projects']} project names, every errors and commands "
              f"list) and {report['terms_dropped']:,} terms across "
              f"{report['buckets']} clouds")
        if report["servers_stripped"]:
            print(f"  stripped the MCP server name off "
                  f"{report['servers_stripped']:,} tool entries — the verb is "
                  f"kept, the vendor is not")
        print("  the remaining clouds are still your own words — read them "
              "before you hand this over")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
