#!/usr/bin/env python3
"""Stage 2: data/events.ndjson -> data/stats.json (+ data/vocab.json sidecar)

Fails loudly on an empty or malformed event stream rather than emitting a
misleading empty dashboard.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wcstats.clean import prose_text  # noqa: E402
from wcstats.facets import Facets, error_signature, RE_EXT, SHELL_SUBCMD  # noqa: E402
from wcstats.score import top_n, trends  # noqa: E402
from wcstats.spend import Spend  # noqa: E402
from wcstats.tokenize import (phrase_candidates, raw_tokens, shingle_key,  # noqa: E402
                              tokens)
from wcstats.wrapped import Wrapped, build as build_wrapped  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IN = os.path.join(DATA, "events.ndjson")
OUT = os.path.join(DATA, "stats.json")
VOCAB = os.path.join(DATA, "vocab.json")
# 2: events carry model/usage; stats.json gains "spend" and "wrapped".
SCHEMA_VERSION = 2
TOP_PROJECTS = 24


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
            if p.isalpha() or "-" in p:
                sub = f"{head} {p}"
            break
    return head, sub


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=IN)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--top", type=int, default=300)
    args = ap.parse_args()

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
            month = ts[:7] if len(ts) >= 7 else "unknown"
            day = ts[:10] if len(ts) >= 10 else None
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

            if role == "user" and kind == "prompt":
                # Cleaned prose, before stopword removal: a prompt that is
                # only "thanks!" survives here but tokenizes to nothing.
                wrapped.add_user_prose(tool, text, day)

            toks = tokens(text)
            if not toks:
                continue
            phrases = phrase_candidates(raw_tokens(text))

            if role == "user" and kind == "prompt":
                key = shingle_key(ev.get("text") or "")
                for b in slices:
                    b.add_prompt(toks, phrases, key)
                if day:
                    per_day[day] += 1
                    hour_hist[int(ts[11:13]) if len(ts) >= 13 else 0] += 1
                    weekday_hist[_weekday(day)] += 1
                month_user[month].update(toks)
            elif role == "assistant":
                for b in slices:
                    b.add_assistant(toks, phrases)

    if total == 0:
        print("FATAL: no events parsed from the stream.", file=sys.stderr)
        return 1

    g = F.get("global", "all")
    if not g.prose_user:
        print("FATAL: zero user prose survived filtering -- refusing to emit "
              "an empty dashboard.", file=sys.stderr)
        return 1

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
    with open(VOCAB, "w", encoding="utf-8") as fh:
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

    print(f"\nwrote {args.out} ({size/1e6:.2f} MB) in {time.time()-t0:.1f}s")
    return 0


def _weekday(day):
    import datetime
    try:
        y, m, d = (int(x) for x in day.split("-"))
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][
            datetime.date(y, m, d).weekday()]
    except (ValueError, IndexError):
        return "Mon"


if __name__ == "__main__":
    sys.exit(main())
