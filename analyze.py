#!/usr/bin/env python3
"""Stage 2: data/events.ndjson -> data/stats.json (+ a gated vocab.json sidecar
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
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.base import OBSERVED, normalize_project  # noqa: E402
# Stage 3 owns the definition of "this cloud entry is a string off the machine,
# not a word somebody typed" -- it is the rule the shareable dashboard is built
# on. Imported rather than restated: two copies of a privacy filter drift, and
# the copy that drifts is the one that stops catching things.
from build_dashboard import (_leaks as looks_harvested,  # noqa: E402
                             _name_patterns, account_names)
from wcstats.clean import (BRANCH_MODES, LABEL_PLACEHOLDER,  # noqa: E402
                           REDACT_HELP, build_redaction, format_redaction,
                           install_redactor, prose_text)
from wcstats.facets import (Facets, capped, error_signature,  # noqa: E402
                           RE_EXT, SHELL_SUBCMD)
from wcstats.score import log_odds, top_n, trends  # noqa: E402
from wcstats.spend import Spend, local_date  # noqa: E402
from wcstats.tokenize import (discourse_ngrams, phrase_candidates, shingle_key,  # noqa: E402
                              tokens)
from wcstats.wrapped import Wrapped, build as build_wrapped  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IN = os.path.join(DATA, "events.ndjson")
OUT = os.path.join(DATA, "stats.json")
# The sidecar is derived from --out, never hard-coded: running with a scratch
# --out must not overwrite the real data/vocab.json.
VOCAB_NAME = "vocab.json"
# Written by ingest.py, which is the only stage that can see the filesystem
# the sessions ran on. Optional: everything in it can be recovered here except
# the git remotes, so an old corpus still gets redacted.
REDACT_NAME = "redact.json"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# 2: events carry model/usage; stats.json gains "spend" and "wrapped".
SCHEMA_VERSION = 2
TOP_PROJECTS = 24


def vocab_path(out):
    """The vocab sidecar that belongs to this --out, in the same directory."""
    return os.path.join(os.path.dirname(os.path.abspath(out)), VOCAB_NAME)


# --- self-redaction setup ----------------------------------------------------
#
# stats.json is what a user hands to somebody else -- vocab.json is the same
# vocabulary with the top-N cut taken off, which is a much longer tail and a
# much worse thing to hand over ungated (see gate_vocab). Either way this is
# the boundary that has to hold. Three things feed it:
#
#   * the identities of whoever is running the tool -- account name, git
#     name/email, the account half of the git remotes ingest saw. A username
#     has no shape a regex can recognise, so knowing the literal is the only
#     way to catch `chensagi/finn` in the middle of a sentence.
#   * the worktree branch names, which ingest discovers while folding a
#     worktree onto its repo, and which this stage can also recover from the
#     encoded labels still stored in the event stream.
#   * whatever the user names with --redact.
#
# `project` is the only field carrying a branch name that this stage can see
# before it starts counting, so it gets a cheap pre-pass of its own. Reading
# the labels a second time costs a couple of seconds; getting the redaction
# list only halfway through the corpus would leave the first half published.
RE_PROJECT = re.compile(rb'"project"\s*:\s*"((?:[^"\\]|\\.)*)"')
_HEAD = 1000  # `project` is the 4th field an Event writes; never far in


def scan_project_labels(path):
    """Every distinct project label in the stream, without parsing the JSON.

    A full json.loads of a 200 MB corpus twice over would double the runtime
    of this stage for four bytes of each line.
    """
    seen = set()
    try:
        with open(path, "rb") as fh:
            for line in fh:
                m = RE_PROJECT.search(line[:_HEAD]) or RE_PROJECT.search(line)
                if not m:
                    continue
                raw = m.group(1)
                if raw in seen:
                    continue
                seen.add(raw)
    except OSError:
        return []
    out = []
    for raw in seen:
        try:
            out.append(json.loads(b'"' + raw + b'"'))
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            continue
    return out


def load_sidecar(inp):
    """ingest.py's redact.json, if this corpus has one.

    Returns (buckets for build_redaction, path). Its `branch_repos` mapping is
    fed straight into OBSERVED instead: it is not a redaction bucket but the
    branch -> repo pairing label_fixer() needs, and only ingest could see it.
    A sibling-directory worktree is the case that depends on it -- a label like
    `finn-loop-writer` is textually indistinguishable from a repository of that
    name, so without the pairing an old corpus can only mask it, not fold it.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(inp)), REDACT_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    out = {}
    for key in ("identities", "branches", "decorated", "explicit"):
        vals = data.get(key)
        if isinstance(vals, list):
            out[key] = [v for v in vals if isinstance(v, str)]
    pairs = data.get("branch_repos")
    if isinstance(pairs, dict):
        for branch, repo in pairs.items():
            if isinstance(branch, str) and isinstance(repo, str):
                OBSERVED.branch(branch.strip().lower(), repo.strip().lower())
    return out, path


def setup_redaction(args):
    """Build and install the redactor. Returns (redactor, report, sidecar)."""
    # normalize_project() records every branch name it decodes into OBSERVED,
    # so walking the labels is also how the branch list gets built.
    for label in scan_project_labels(args.inp):
        normalize_project(label)
    sidecar, sidecar_path = load_sidecar(args.inp)
    redactor, report = build_redaction(
        branch_pairs=OBSERVED.pairs(),
        extra=list(args.redact),
        sidecar=sidecar,
        branch_mode=args.branch_redaction)
    install_redactor(redactor)
    return redactor, report, sidecar_path


def label_fixer(redactor):
    """project label -> a label that names no branch and no person.

    Order matters. normalize_project() handles the encoded worktree labels;
    what is left over is a bare label that may still BE a branch name, because
    some tools record the worktree directory directly. Those are re-attributed
    to the repo the branch belongs to when ingest saw the pairing -- which is
    strictly better than masking, since the events really do belong to that
    repo -- and masked when it did not.

    This is also the whole of what an old corpus gets for a SIBLING-directory
    worktree. `finn-loop-writer` has no pool marker and no doubled dash: it is
    spelled exactly like a repository called finn-loop-writer, so
    normalize_project() cannot and must not touch it. It folds here only when
    ingest's sidecar carried the pairing, and otherwise it stays a project of
    its own until the next ingest.
    """
    branch_repo = {b: r for b, r in OBSERVED.pairs() if r}

    def fix(label):
        p = normalize_project(label)
        repo = branch_repo.get((p or "").lower())
        if repo:
            return repo
        return redactor.scrub_label(p, LABEL_PLACEHOLDER)

    return fix


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


# --- the vocab.json sidecar --------------------------------------------------
#
# vocab.json is stats.json's clouds with the top-N cut taken OFF. That makes it
# the more dangerous of the two files, not the safer one, and it shipped with
# no gate at all: the owner's forename and surname, a laptop model identifier
# lifted out of a pasted crash report, a private repo name, opaque
# high-entropy blobs out of thinking events, and three thousand dotted
# filenames -- an inventory of the source trees of unreleased products. None of
# it appears in stats.json, because all of it lives below rank 300.
#
# So the tail gets the treatment the top already gets, plus the two rules that
# only matter once you are past rank 300:
#
#   * the shape filter the shareable dashboard uses (looks_harvested): dotted
#     identifiers, paths, handles, URLs, the local account names and the
#     project names. A `<component>.tsx` is not vocabulary in any sense --
#     nobody "used the word" -- and a vocabulary file is exactly where a
#     source-tree listing should not be.
#   * a frequency floor. A term seen once carries no frequency information at
#     all; what it carries is the fact that this corpus contains it, which is
#     the half of the file with no analytical value and all of the exposure.
#     Half the user vocabulary and 43% of the assistant vocabulary is hapax,
#     and every opaque blob and the laptop identifier were in it.
#
# Errors are dropped outright. An error signature is not a word: it is a
# sentence quoted off the machine, and a filter that judges single terms by
# their shape cannot vet one. stats.json keeps the top 40 for the private
# dashboard, which is where raw failure text belongs.
VOCAB_MIN_COUNT = 2
# ...but only once there is a tail to speak of. In a corpus of forty distinct
# words a hapax is not the tail, it is the corpus, and a floor there would hand
# back an empty file instead of a gated one.
VOCAB_FLOOR_MIN_TERMS = 400
VOCAB_ABOUT = (
    "Full-length vocabulary tail behind stats.json. GATED, not raw: "
    "redacted literals, filename/path/handle-shaped tokens, account and "
    "project names, and terms seen only once are removed, and error "
    "signatures are not included at all. Still your own words about your own "
    "work — read it before you hand it to anybody."
)


def gate_vocab(bucket, redactor, projects, floor=VOCAB_MIN_COUNT,
               floor_min_terms=VOCAB_FLOOR_MIN_TERMS):
    """The global bucket -> the payload vocab.json is allowed to carry.

    Returns (payload, report). The report is counts only -- naming the dropped
    terms would put them straight back into the file they were dropped from.
    """
    accounts = account_names()
    pats = _name_patterns(projects)
    report = {"seen": 0, "kept": 0, "redacted": 0, "harvested": 0, "rare": 0,
              "floor": floor, "floored": []}

    def gate(counter, name, apply_floor):
        items = counter.most_common()
        report["seen"] += len(items)
        use_floor = apply_floor and len(items) >= floor_min_terms
        if use_floor:
            report["floored"].append(name)
        out = []
        for i, (term, n) in enumerate(items):
            # most_common() is sorted descending, so the floor ends the list
            # and everything after it is rare by definition.
            if use_floor and n < floor:
                report["rare"] += len(items) - i
                break
            # Normally zero: clean_prose() already scrubbed the redactor's
            # literals out of the text before it was tokenized. Kept as a
            # second pass because the counters this reads are also fed by
            # paths that do not go through clean_prose (tools, commands), and
            # because a gate that trusts an upstream stage is a gate that
            # stops holding the day that stage changes.
            if redactor.hits(term):
                report["redacted"] += 1
            elif looks_harvested(term, accounts, pats):
                report["harvested"] += 1
            else:
                out.append([term, n])
        report["kept"] += len(out)
        return out

    return {
        "_about": VOCAB_ABOUT,
        "prose_user": gate(bucket.prose_user, "prose_user", True),
        "prose_assistant": gate(bucket.prose_asst, "prose_assistant", True),
        "tools": gate(bucket.tools, "tools", False),
        "commands": gate(bucket.commands, "commands", False),
    }, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=IN)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--vocab", default=None,
                    help="gated full-length vocabulary tail (no top-N cut); "
                         "defaults to vocab.json beside --out")
    ap.add_argument("--top", type=int, default=300)
    ap.add_argument("--redact", action="append", default=[], metavar="STR",
                    help=REDACT_HELP)
    ap.add_argument("--branch-redaction", choices=list(BRANCH_MODES),
                    default="full",
                    help="how hard to mask worktree branch names in prose: "
                         "'full' also masks the bare name (default), "
                         "'decorated' only worktree-<branch> spellings, "
                         "'off' neither")
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
    redactor, redaction, sidecar = setup_redaction(args)
    fix_label = label_fixer(redactor)
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
    wrapped_seen = set()
    # Discourse n-grams per side, for the model's-voice slide. The PMI
    # phrase pool is content bigrams only and cannot hold "let me check".
    voice_grams = {"assistant": Counter(), "user": Counter()}
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
            # Second line of defence. The adapters now fold a worktree onto its
            # repo, but events.ndjson stores the label rather than the path, so
            # a corpus ingested before that fix still carries branch names --
            # and stats.json is the file that gets shared. Repair them here too
            # rather than requiring a re-ingest to stop publishing them.
            project = fix_label(ev.get("project"))
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
                # A locally written script is named after whatever it does,
                # and here that was an unreleased feature: `graphify` reached
                # the shipped commands cloud four times over. The command
                # clouds are facets like any other and get the same treatment.
                if head and redactor.hits(head):
                    head, sub = None, None
                elif sub and redactor.hits(sub):
                    sub = None
                path_text = ev.get("text") or ""
                m = RE_EXT.search(path_text.strip().split()[-1]) if path_text.strip() else None
                ext = m.group(1).lower() if m else None
                if ext and redactor.hits(ext):
                    ext = None
                for b in slices:
                    b.tools[name] += 1
                    if head:
                        b.commands[head] += 1
                        if sub:
                            b.commands[sub] += 1
                    if ext:
                        b.exts[ext] += 1
            elif kind == "error":
                # error_signature() masks by shape (paths, hosts, hashes); the
                # literals it cannot recognise -- an owner name in a Vercel
                # error, a branch name in a HERD_LANE= assignment -- are
                # exactly what this list is for. Uppercase to match the
                # signature idiom, where PATH and HOST already mean "masked".
                raw_sig = error_signature(ev.get("text") or "")
                sig = redactor.scrub(raw_sig, "REDACTED")
                if sig is not raw_sig:
                    # Only when something was actually masked: re-spacing every
                    # signature would rewrite keys that have nothing to hide.
                    sig = " ".join(sig.split())
                if sig:
                    for b in slices:
                        b.errors[sig] += 1

            text = prose_text(ev)
            if not text:
                continue

            is_prompt = role == "user" and kind == "prompt"
            if is_prompt:
                # The same near-duplicate key the clouds use, computed here so
                # the wrapped counters can honour it too. Without it an
                # automation prompt fired once per repo -- "perform the
                # analysis based on gemini.md and save to md" -- reads as a
                # phrase used across fourteen projects, and wins the signature
                # slide outright. It is one prompt, sent fourteen times.
                dup_key = shingle_key(ev.get("text") or "")
                repeat = bool(dup_key) and dup_key in wrapped_seen
                if dup_key:
                    wrapped_seen.add(dup_key)
                # Cleaned prose, before stopword removal: a prompt that is
                # only "thanks!" survives here but tokenizes to nothing.
                # Same cleaned prose the clouds see, but over a pool that
                # keeps function words: a habit is made of the words the
                # tokenizer's stopword list exists to throw away.
                grams = () if repeat else discourse_ngrams(text)
                wrapped.add_user_prose(
                    tool, text, day, project=project, prompt_id=sid,
                    grams=grams)
                if grams:
                    voice_grams["user"].update(capped(grams))
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
            # Cleaned prose, not the token list: a phrase is two words with a
            # space between them, and only the text still knows that.
            phrases = phrase_candidates(text)

            if is_prompt:
                key = dup_key
                for b in slices:
                    b.add_prompt(toks, phrases, key)
                month_user[month].update(toks)
            elif role == "assistant" and kind == "reply":
                voice_grams["assistant"].update(capped(discourse_ngrams(text)))
                # Replies only. Thinking is 43% of the model's prose and it
                # does not sound like the model talking to you -- it reads
                # "focusing", "prioritizing", "emphasizing specific", and it
                # was drowning the actual reply voice in the cloud the
                # dashboard has always labelled "Assistant replies".
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
    # Background for the model's voice: everything either side said, so the
    # log-odds asks "distinctive against this conversation", not against itself.
    voice_bg = Counter(g.prose_asst)
    voice_bg.update(g.prose_user)
    assistant_voice = log_odds(g.prose_asst, voice_bg, n=40)
    gram_bg = Counter(voice_grams["assistant"])
    gram_bg.update(voice_grams["user"])
    assistant_voice_phrases = log_odds(voice_grams["assistant"], gram_bg,
                                       n=20, min_count=8)
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
        # Not raw frequency. Both sides of a coding session say "file" and
        # "run" constantly, so a frequency list of the model's words is a
        # frequency list of the user's with the names changed. What makes the
        # slide worth reading is the *difference*: scored against the owner's
        # own vocabulary, what comes back is the register only the model uses.
        "assistant_top_words": assistant_voice,
        "assistant_top_phrases": assistant_voice_phrases,
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

    vocab, vocab_report = gate_vocab(
        g, redactor, [p for p in F.keys("project") if p != "unknown"])

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(stats, fh, ensure_ascii=False, separators=(",", ":"))
    with open(vocab_out, "w", encoding="utf-8") as fh:
        json.dump(vocab, fh, ensure_ascii=False, separators=(",", ":"))

    size = os.path.getsize(args.out)
    print(f"events read      {total:,}  (malformed lines: {bad})")
    print(f"user prompts     {g.prompts:,}")
    print(f"prose words      {g.words:,}")
    print(f"unique terms     {len(g.prose_user):,} user / {len(g.prose_asst):,} assistant")
    print(f"months covered   {len(months)}  ({months[0] if months else '-'} .. "
          f"{months[-1] if months else '-'})")
    print(f"projects         {stats['totals']['projects']} ({len(top_projects)} with clouds)")
    print(format_redaction(redaction))
    if sidecar:
        print(f"                 (identities and remotes from {sidecar})")

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

    vr = vocab_report
    print(f"\nwrote {args.out} ({size/1e6:.2f} MB) + {vocab_out} "
          f"({os.path.getsize(vocab_out)/1e6:.2f} MB) in {time.time()-t0:.1f}s")
    print(f"vocab gate       kept {vr['kept']:,} of {vr['seen']:,} terms — "
          f"dropped {vr['redacted']:,} redacted, {vr['harvested']:,} "
          f"filename/path/name-shaped, {vr['rare']:,} seen fewer than "
          f"{vr['floor']} times"
          + (f" (floor on {', '.join(vr['floored'])})" if vr["floored"]
             else " (no floor: too few terms to have a tail)"))
    print("                 error signatures are not in it; stats.json keeps "
          "the top 40 for the private dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
