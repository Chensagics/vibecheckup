#!/usr/bin/env python3
"""Stage 2: data/events.ndjson -> data/stats.json (+ a vocab.json sidecar
written beside it)

Fails loudly on an empty or malformed event stream. A corpus that yields no
*words* is a warning rather than a failure: the spend, activity and wrapped
counts are still real, and half a dashboard beats none.

Every calendar figure here -- day, hour, weekday, streak, busiest day -- is
LOCAL time, on the same boundary wcstats.spend uses, so no two panels disagree
about which day a 1am prompt belongs to.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcstats.clean import prose_text  # noqa: E402
from wcstats.facets import Facets, error_signature, RE_EXT, SHELL_SUBCMD  # noqa: E402
from wcstats.score import top_n, trends  # noqa: E402
from wcstats.spend import Spend, local_date  # noqa: E402
from wcstats.tokenize import (phrase_candidates, raw_tokens, shingle_key,  # noqa: E402
                              tokens)
from wcstats.wrapped import Wrapped, build as build_wrapped  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IN = os.path.join(DATA, "events.ndjson")
OUT = os.path.join(DATA, "stats.json")
# The sidecar is derived from --out, never hard-coded: running with a scratch
# --out must not overwrite the real data/vocab.json.
VOCAB_NAME = "vocab.json"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# 2: events carry model/usage; stats.json gains "spend" and "wrapped".
SCHEMA_VERSION = 2
TOP_PROJECTS = 24


def vocab_path(out):
    """The vocab sidecar that belongs to this --out, in the same directory."""
    return os.path.join(os.path.dirname(os.path.abspath(out)), VOCAB_NAME)


def local_stamp(ts):
    """UTC ISO timestamp -> (local day, local hour, local weekday).

    Events are stored in UTC; the user lived them in their own timezone. A
    22:00 UTC habit is a 1am habit in UTC+3 and a 2pm habit in UTC-8, so
    reading the hour out of the ISO string puts the wrong number on the share
    card for everyone outside Greenwich. `local_date` is spend.py's, on
    purpose: activity and spend must key days identically or the dashboard
    contradicts itself.

    Anything unparseable comes back as (None, None, None), so one malformed
    record is skipped rather than killing the whole stage.
    """
    day = local_date(ts)
    if not day:
        return None, None, None
    hour = None
    s = str(ts)
    # A bare "2026-08-02" parses to midnight, and counting it as a 00:00
    # prompt would invent an hour the record never carried. Only a timestamp
    # with a clock in it gets a vote in the hour histogram.
    if len(s) > 10:
        try:
            hour = datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().hour
        except (ValueError, TypeError, OSError, OverflowError):
            pass
    y, m, d = (int(x) for x in day.split("-"))
    return day, hour, WEEKDAYS[date(y, m, d).weekday()]


def argv_head(cmd):
    if not cmd:
        return None
    for tok in cmd.strip().split():
        if "=" in tok and not tok.startswith("-") and tok.split("=")[0].isidentifier():
            continue
        return tok.strip("(){}`'\"$").rsplit("/", 1)[-1] or None
    return None


def command_label(cmd):
    """`git commit -m x` -> ("git", "git commit"); `rg foo` -> ("rg", None)."""
    head = argv_head(cmd)
    if not head:
        return None, None
    sub = None
    if head in SHELL_SUBCMD:
        parts = cmd.strip().split()
        for p in parts[1:]:
            if p.startswith("-"):
                continue
            p = p.strip("'\"")
            # A path is never a subcommand, and `git -C /Users/me/proj-x` would
            # otherwise match the hyphen test below and publish a home directory
            # into the commands cloud, which the dashboard renders.
            if "/" in p or p.startswith((".", "~", "$")):
                break
            if p.isalpha() or "-" in p:
                sub = f"{head} {p}"
            break
    return head, sub


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=IN)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--vocab", default=None,
                    help="full-count sidecar; defaults to vocab.json beside --out")
    ap.add_argument("--top", type=int, default=300)
    args = ap.parse_args()
    vocab_out = args.vocab or vocab_path(args.out)

    if not os.path.exists(args.inp):
        print(f"FATAL: {args.inp} does not exist. Run ingest.py first.",
              file=sys.stderr)
        return 1
    if os.path.getsize(args.inp) == 0:
        print(f"FATAL: {args.inp} is empty.", file=sys.stderr)
        return 1

    t0 = time.time()
    F = Facets()
    coverage = defaultdict(lambda: {"first": None, "last": None,
                                    "sessions": set(), "months": Counter(),
                                    "events": 0, "heuristic": 0})
    per_day = Counter()
    hour_hist = Counter()
    weekday_hist = Counter()
    session_events = Counter()
    session_tool = {}
    session_first = {}
    session_last = {}
    project_tool = defaultdict(Counter)
    month_user = defaultdict(Counter)
    spend = Spend()
    wrapped = Wrapped()
    bad = 0
    total = 0

    with open(args.inp, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                bad += 1
                continue
            total += 1

            tool = ev.get("tool") or "unknown"
            project = ev.get("project") or "unknown"
            ts = ev.get("ts") or ""
            day, hour, weekday = local_stamp(ts)
            month = day[:7] if day else "unknown"
            sid = f"{tool}:{ev.get('session_id')}"
            kind = ev.get("kind")
            role = ev.get("role")

            spend.add(ev)

            cov = coverage[tool]
            cov["events"] += 1
            cov["sessions"].add(sid)
            if ev.get("confidence") == "heuristic":
                cov["heuristic"] += 1
            if ts:
                if cov["first"] is None or ts < cov["first"]:
                    cov["first"] = ts
                if cov["last"] is None or ts > cov["last"]:
                    cov["last"] = ts
                cov["months"][month] += 1

            session_events[sid] += 1
            session_tool[sid] = tool
            if ts:
                if sid not in session_first or ts < session_first[sid]:
                    session_first[sid] = ts
                if sid not in session_last or ts > session_last[sid]:
                    session_last[sid] = ts

            slices = [F.get("global", "all"), F.get("tool", tool),
                      F.get("project", project), F.get("month", month)]
            for b in slices:
                b.events += 1
                b.sessions.add(sid)

            project_tool[project][tool] += 1

            tk = ev.get("tokens")
            if isinstance(tk, int) and tk > 0:
                for b in slices:
                    b.llm_tokens += tk

            if kind == "tool_call":
                name = ev.get("tool_name") or "unknown"
                head, sub = command_label(ev.get("cmd"))
                path_text = ev.get("text") or ""
                m = RE_EXT.search(path_text.strip().split()[-1]) if path_text.strip() else None
                for b in slices:
                    b.tools[name] += 1
                    if head:
                        b.commands[head] += 1
                        if sub:
                            b.commands[sub] += 1
                    if m:
                        b.exts[m.group(1).lower()] += 1
            elif kind == "error":
                sig = error_signature(ev.get("text") or "")
                if sig:
                    for b in slices:
                        b.errors[sig] += 1

            text = prose_text(ev)
            if not text:
                continue

            is_prompt = role == "user" and kind == "prompt"
            if is_prompt:
                # Cleaned prose, before stopword removal: a prompt that is
                # only "thanks!" survives here but tokenizes to nothing.
                wrapped.add_user_prose(tool, text, day)
                # Activity is a fact about the clock, not about what survived
                # the stopword list: a prompt that tokenizes to nothing still
                # happened, and in a script we cannot segment that is every
                # prompt there is.
                if day:
                    per_day[day] += 1
                    weekday_hist[weekday] += 1
                    if hour is not None:
                        hour_hist[hour] += 1

            toks = tokens(text)
            if not toks:
                continue
            phrases = phrase_candidates(raw_tokens(text))

            if is_prompt:
                key = shingle_key(ev.get("text") or "")
                for b in slices:
                    b.add_prompt(toks, phrases, key)
                month_user[month].update(toks)
            elif role == "assistant":
                for b in slices:
                    b.add_assistant(toks, phrases)

    if total == 0:
        print("FATAL: no events parsed from the stream.", file=sys.stderr)
        return 1

    g = F.get("global", "all")
    if not g.prose_user:
        # Not fatal. A corpus can be entirely non-lexical -- a script written
        # without spaces, or nothing but "ok" and "thanks" -- while the spend,
        # activity and wrapped counts stay perfectly real. Refusing to build
        # would hand the user nothing at all instead of most of a dashboard.
        print("WARNING: zero user prose survived filtering. The word clouds, "
              "phrases and trends will be empty; spend, activity and the "
              "wrapped counts are unaffected.", file=sys.stderr)

    bg_user, bg_asst = g.prose_user, g.prose_asst
    months = sorted(m for m in F.keys("month") if m != "unknown")

    # Keep the payload small: only the busiest projects get their own cloud.
    proj_rank = sorted(F.keys("project"),
                       key=lambda p: -F.get("project", p).prompts)
    top_projects = [p for p in proj_rank if p != "unknown"][:TOP_PROJECTS]

    trend = trends(month_user, months)
    spend_section = spend.render()
    global_cloud = g.render(bg_user, bg_asst, args.top, distinctive=False)
    wrapped_section = build_wrapped(wrapped, {
        "per_day": per_day,
        "prompts": g.prompts,
        "sessions": len(session_events),
        "top_words": global_cloud["prose_user"],
        "top_phrases": global_cloud["phrases_user"],
        "rising": trend["rising"],
        "hour_histogram": hour_hist,
        "weekday_histogram": weekday_hist,
        "tools_used": len(coverage),
        "projects_count": len([p for p in F.keys("project") if p != "unknown"]),
        "spend": spend_section,
        "priciest_day": spend.priciest_day(),
    })

    stats = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_events": total,
        "coverage": {
            t: {
                "first": c["first"], "last": c["last"],
                "sessions": len(c["sessions"]), "events": c["events"],
                "months": dict(sorted(c["months"].items())),
                "heuristic_events": c["heuristic"],
            } for t, c in sorted(coverage.items())
        },
        "totals": {
            "sessions": len(session_events),
            "events": total,
            "prompts": g.prompts,
            "words": g.words,
            "llm_tokens": g.llm_tokens,
            "projects": len([p for p in F.keys("project") if p != "unknown"]),
            "tools": len(coverage),
        },
        "clouds": {
            "global": global_cloud,
            "by_tool": {t: F.get("tool", t).render(bg_user, bg_asst, args.top)
                        for t in sorted(F.keys("tool"))},
            "by_project": {p: F.get("project", p).render(bg_user, bg_asst, 150)
                           for p in top_projects},
            "by_month": {m: F.get("month", m).render(bg_user, bg_asst, 150)
                         for m in months},
        },
        "trends": trend,
        "spend": spend_section,
        "wrapped": wrapped_section,
        "activity": {
            "per_day": dict(sorted(per_day.items())),
            "hour_histogram": {str(h): hour_hist.get(h, 0) for h in range(24)},
            "weekday_histogram": {d: weekday_hist.get(d, 0) for d in
                                  ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]},
            "session_lengths": top_n(session_events, 40),
            "top_projects": [
                {"t": p, "n": F.get("project", p).prompts,
                 "events": F.get("project", p).events,
                 "sessions": len(F.get("project", p).sessions),
                 "tools": dict(project_tool[p])}
                for p in proj_rank[:40] if p != "unknown"
            ],
            "sessions_by_tool": {t: len(c["sessions"])
                                 for t, c in sorted(coverage.items())},
        },
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, separators=(",", ":"))
    with open(vocab_out, "w", encoding="utf-8") as fh:
        json.dump({"prose_user": g.prose_user.most_common(),
                   "prose_assistant": g.prose_asst.most_common(),
                   "tools": g.tools.most_common(),
                   "commands": g.commands.most_common(),
                   "errors": g.errors.most_common()},
                  fh, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(args.out)
    print(f"events read      {total:,}  (malformed lines: {bad})")
    print(f"user prompts     {g.prompts:,}")
    print(f"prose words      {g.words:,}")
    print(f"unique terms     {len(g.prose_user):,} user / {len(g.prose_asst):,} assistant")
    print(f"months covered   {len(months)}  ({months[0] if months else '-'} .. "
          f"{months[-1] if months else '-'})")
    print(f"projects         {stats['totals']['projects']} ({len(top_projects)} with clouds)")

    sp = spend_section
    print(f"\nspend (list-price estimate)  ${sp['total_cost']:,.2f} over "
          f"{sp['total_tokens']:,} tokens / {sp['events']:,} billed events")
    for row in sp["by_tool"]:
        print(f"  {row['tool']:<14}${row['cost']:>10,.2f}  "
              f"{row['tokens']:>14,} tokens  {row['sessions']:>5} sessions")
    if sp["tools_without_usage"]:
        print(f"  no usage data: {', '.join(sp['tools_without_usage'])}")
    if sp["unpriced_models"]:
        # Loud on purpose: these carry real tokens at an unknown rate, so the
        # headline total is an undercount until the price table catches up.
        print(f"  UNPRICED MODELS (cost reported as null): "
              f"{', '.join(sp['unpriced_models'])}")
    w = wrapped_section
    print(f"wrapped          {w['words_to_ai']:,} words to AI, "
          f"{w['longest_streak_days']}-day streak, peak {w['peak_hour']:02d}:00 "
          f"{w['peak_weekday']}, politeness {dict(w['politeness'])}")

    print(f"\nwrote {args.out} ({size/1e6:.2f} MB) + {vocab_out} "
          f"in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
