#!/usr/bin/env python3
"""Deterministic synthetic corpus for `vibecheck.sh --demo`.

Writes a data/events.ndjson that looks like a real six-month multi-tool
history without touching (or resembling) anyone's actual logs: fake project
names, invented vocabulary, plausible models and token counts.

The output is fully seeded -- the same seed always produces byte-identical
NDJSON -- so README screenshots and demo runs are reproducible.

    python3 samples/generate.py                    # -> data/events.ndjson
    python3 samples/generate.py /tmp/events.ndjson # -> anywhere
    python3 samples/generate.py --seed 7 --events 4000

Event fields mirror adapters/base.py:Event, plus the `model` and `usage`
fields from the vibecheck design contract (section 4):
usage = {"input", "output", "cache_read", "cache_write"}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_OUT = os.path.join(ROOT, "data", "events.ndjson")

SEED = 20260826
TARGET_EVENTS = 1500
# Fixed window: a demo corpus must not change from one day to the next.
END_DATE = "2026-08-24"
SPAN_DAYS = 182  # ~6 months
# A holiday, so the streak and activity stats have a real gap to find, and a
# crunch fortnight, so the streak and busiest-day stats have a real peak.
GAP_START, GAP_LEN = 96, 9
CRUNCH_START, CRUNCH_LEN = 138, 14

# --- tools -------------------------------------------------------------------

# Grok CLI genuinely exposes no usage data (design section 2), so its events
# carry no model and no usage -- the dashboard badges it as such.
# Every id here resolves against wcstats/prices.json by longest-prefix match;
# the dated haiku id is deliberate, so the demo exercises that resolution.
MODELS = {
    "claude_code": [
        ("claude-sonnet-5", 6),
        ("claude-opus-5", 3),
        ("claude-haiku-4-5-20251001", 2),
    ],
    "codex": [("gpt-5.3-codex", 7), ("gpt-5.2", 3)],
    "gemini_cli": [("gemini-3-pro", 6), ("gemini-2.5-flash", 4)],
    "grok": [],
}

# Grok has no per-message timestamps in its logs; the adapter backfills them.
INEXACT_TS = {"grok"}

# --- projects ----------------------------------------------------------------
# Fun fake names only. No real paths, no real repos.

PROJECTS = [
    ("quantum-teapot", 16, "backend", {"claude_code": 7, "codex": 2, "gemini_cli": 1, "grok": 1}),
    ("nimbus-ledger", 14, "payments", {"claude_code": 5, "codex": 4, "gemini_cli": 1, "grok": 1}),
    ("velvet-otter", 12, "frontend", {"claude_code": 6, "codex": 1, "gemini_cli": 3, "grok": 1}),
    ("pixel-forge", 10, "frontend", {"claude_code": 3, "codex": 2, "gemini_cli": 4, "grok": 2}),
    ("harbor-lantern", 9, "infra", {"claude_code": 4, "codex": 4, "gemini_cli": 1, "grok": 2}),
    ("sable-compass", 8, "data", {"claude_code": 3, "codex": 3, "gemini_cli": 3, "grok": 1}),
    ("tidepool-api", 8, "backend", {"claude_code": 5, "codex": 3, "gemini_cli": 1, "grok": 1}),
    ("cobalt-parrot", 6, "data", {"claude_code": 2, "codex": 2, "gemini_cli": 4, "grok": 3}),
    ("meadowlark-cli", 5, "infra", {"claude_code": 4, "codex": 2, "gemini_cli": 1, "grok": 3}),
    ("jetsam-notes", 4, "frontend", {"claude_code": 3, "codex": 1, "gemini_cli": 2, "grok": 2}),
]

# --- vocabulary --------------------------------------------------------------
# Kept as plain prose: no leading code keywords, no trailing braces, nothing
# wcstats/clean.py would strip as machine output.

PROMPTS = [
    "the retry loop drops the last event whenever the queue drains, can you work out why",
    "walk me through how the session cache decides to evict an entry",
    "refactor the auth middleware so the token refresh happens in exactly one place",
    "this test is flaky on cold start and I cannot reproduce it locally",
    "add a regression test for the pagination helper, especially the empty page case",
    "why does the build take four minutes now when it used to take forty seconds",
    "split this module into a parser and a renderer, keep the public surface the same",
    "the deploy went out but the health check never turned green",
    "read through the diff and tell me what would break in production",
    "rename the worker queue to something that says what it actually does",
    "we are seeing duplicate rows after the migration, find where the write happens twice",
    "make the error message useful, right now it just says something went wrong",
    "explain what this regular expression matches and give me three examples",
    "the dark mode palette washes out on the settings screen",
    "swap the polling loop for a proper subscription and keep the fallback",
    "write a short summary of what changed on this branch for the pull request",
    "please review the migration script before I run it against staging",
    "please add a loading state so the page stops flashing empty",
    "thanks, that fixed it, now the same thing happens on the mobile layout",
    "thanks for catching that, can you also update the docs to match",
    "sorry, I pasted the wrong file, here is the one I actually meant",
    "sorry to keep going back and forth on this, one more pass on the naming",
    "the linter and the formatter disagree about this file, settle it",
    "cache the expensive lookup but make sure it invalidates when the config changes",
    "trace where the timestamp loses its timezone between the reader and the writer",
    "this function has grown to three hundred lines, pull the validation out",
    "set up a smoke test that runs the whole pipeline on the fixtures",
    "the memory keeps climbing on long sessions, find the leak",
    "convert the callback style to async and update the callers",
    "double check the boundary conditions on the date range filter",
    "add structured logging around the retry path so we can see what actually happened",
    "the response schema changed upstream, make the parser tolerant of both shapes",
    "give me a plan before touching any code, I want to review the approach first",
    "keep the change small, I only want the bug fixed and nothing else moved",
    "roll this back and try the simpler approach we discussed yesterday",
    "the coverage report says this branch is untested, prove it either way",
    "make the cli flag names consistent with the ones we already have",
    "our staging database drifted from the schema, generate the catch up migration",
    "the websocket reconnects forever when the server sends a close frame",
    "audit the dependencies and tell me which ones we no longer import",
    "batch these writes so we stop hammering the database on every keystroke",
    "the search results are ranked badly for short queries",
    "document why this workaround exists so nobody deletes it in six months",
    "compare the two implementations and recommend which one to keep",
    "the timezone handling is wrong for anyone east of the office",
    "profile the hot path and tell me where the time actually goes",
    "add a dry run mode so I can see what it would do before it does it",
    "clean up the dead code paths left over from the old exporter",
]

TOPIC_PROMPTS = {
    "backend": [
        "the worker pool starves when a single job runs long, add a timeout",
        "move the rate limiter in front of the handler instead of inside it",
        "the background job retries forever on a poison message",
        "add an idempotency key so a repeated request does not double charge",
        "the connection pool exhausts under load, tune it and prove the numbers",
        "split the monolithic handler into routes that each do one thing",
    ],
    "payments": [
        "the refund flow leaves the invoice in a half settled state",
        "reconcile the ledger entries against the gateway report for last month",
        "currency rounding is off by a cent on split payments",
        "add a webhook replay guard so a duplicate settlement cannot post twice",
        "the subscription proration looks wrong when a plan changes mid cycle",
        "make the payout summary readable by someone in finance",
    ],
    "frontend": [
        "the modal traps focus but never gives it back when it closes",
        "the list jumps around while images load, reserve the space",
        "make the empty state say something helpful instead of nothing",
        "the animation stutters on a slow device, drop it to a fade",
        "keyboard navigation skips the second column entirely",
        "the layout breaks at tablet width between the sidebar and the content",
    ],
    "infra": [
        "the deploy pipeline runs the same build twice, cut one of them",
        "rotate the credentials and make sure nothing reads the old ones",
        "the container image is nine hundred megabytes, slim it down",
        "add a health endpoint that actually checks the database",
        "the nightly backup silently skipped a week, alert on that",
        "the staging environment drifts from production every few weeks",
    ],
    "data": [
        "the nightly aggregation double counts rows that arrive late",
        "the column names in the export do not match what the report expects",
        "figure out why the daily totals disagree with the weekly rollup",
        "sample the messy records and tell me what the actual shapes are",
        "the parser chokes on rows with an embedded newline inside a quoted field",
        "make the chart readable when a series has a single point",
    ],
}

# Near-identical automation prompts: the clustering toggle needs something to
# collapse, and a real corpus is full of these.
AUTOMATION = [
    "run the full test suite and fix whatever fails",
    "update the changelog and bump the version",
    "summarize what changed on this branch",
    "check the types and the linter, then commit",
    "review the working tree and tell me what is unfinished",
]

REPLIES = [
    "the root cause is that the reader closes the stream before the last flush lands",
    "I found two places doing the same validation and only one of them handles the empty case",
    "the change is smaller than it looks, most of the diff is the moved block",
    "the tests pass locally but the fixture assumes an ordering the database never promised",
    "here is the plan before I touch anything, tell me if the second step looks wrong",
    "that field is optional upstream, so the parser has to tolerate both shapes",
    "the slow part is the repeated lookup inside the loop, not the query itself",
    "I kept the public behaviour identical and only reorganised the internals",
    "the failure only reproduces when the cache is cold, which is why it looks flaky",
    "this is a workaround for an upstream bug, so I left a note explaining why",
    "the migration is safe to run twice, it checks before it writes",
    "I would rather fix the underlying model than add another special case here",
    "the numbers now agree with the report, the difference was late arriving rows",
    "one call site still uses the old signature, I updated it as well",
    "the timezone was being dropped when the string round tripped through the reader",
    "I added a dry run flag so you can see the plan before anything is written",
    "coverage on that branch was genuinely zero, the new test fails without the fix",
    "the config change alone is enough, no code needed to move",
    "there are three ways to do this and the boring one is probably right",
    "I left the old path in place behind a flag so a rollback is one line",
]

THINKING = [
    "the user wants the smallest possible change, so I should not restructure the module",
    "before editing I should check whether the same helper already exists elsewhere",
    "the failure mode suggests ordering, not a logic error, so look at the sort first",
    "there may be a second call site with the same assumption, worth grepping",
    "reproducing the bug in a test is more valuable than guessing at the fix",
    "the cheapest verification here is running the existing suite before touching anything",
]

TOOL_NAMES = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "WebFetch", "Task"]
TOOL_WEIGHTS = [30, 20, 6, 22, 12, 6, 2, 2]

CMDS = [
    "npm test", "npm run build", "pytest -q", "python3 -m unittest discover -s tests",
    "git status", "git diff --stat", "git log --oneline -12", "git commit -m fix",
    "ruff check .", "go test ./...", "cargo build --release", "docker compose up -d",
    "make lint", "npx tsc --noEmit", "rg todo", "ls -la", "kubectl get pods",
]

FILES = [
    "src/router/handlers.ts", "src/lib/cache.ts", "app/models/invoice.py",
    "app/jobs/worker.py", "tests/test_pipeline.py", "pkg/queue/retry.go",
    "web/components/Modal.tsx", "web/styles/theme.css", "scripts/migrate.sql",
    "internal/store/pool.go", "docs/architecture.md", "Makefile",
    "src/parse/reader.rs", "config/staging.yaml", "notebooks/rollup.ipynb",
]

TOOL_RESULTS = [
    "12 files changed, 340 insertions, 118 deletions",
    "All 59 tests passed in 2.1s",
    "no matches found",
    "wrote 1 file",
    "Successfully built image tagged app:latest",
    "3 warnings, 0 errors",
]

ERRORS = [
    "ModuleNotFoundError: No module named 'requests'",
    "TypeError: cannot read properties of undefined (reading 'id')",
    "AssertionError: expected 4 rows, got 5",
    "error: connection refused on port 5432",
    "FAILED tests/test_pipeline.py::test_late_rows - AssertionError",
    "fatal: not a git repository",
    "Permission denied: cannot write to the output directory",
    "Command timed out after 120 seconds",
]

# Evening and night heavy, with a real working-hours bump.
HOUR_WEIGHTS = [6, 4, 2, 1, 1, 1, 1, 2, 4, 7, 9, 9,
                7, 6, 8, 9, 9, 8, 7, 10, 13, 15, 14, 9]


def hexid(rnd: random.Random, n: int) -> str:
    return "".join(rnd.choice("0123456789abcdef") for _ in range(n))


def session_uuid(rnd: random.Random) -> str:
    return "-".join(hexid(rnd, n) for n in (8, 4, 4, 4, 12))


def pick(rnd: random.Random, pairs):
    """Weighted choice over a list of (value, weight) pairs."""
    vals = [v for v, _ in pairs]
    wts = [w for _, w in pairs]
    return rnd.choices(vals, weights=wts, k=1)[0]


def usage_for(rnd: random.Random, tool: str, model: str, turn: int):
    """Plausible per-reply token usage, shaped like each tool's real logs.

    Claude Code reports `input` excluding the cache fields, so a long session
    shows a tiny input against a large cache read. Codex and Gemini report a
    cached subset of the input, which lands in cache_read with no cache write.
    """
    out = rnd.randint(120, 2600)
    if tool == "claude_code":
        # A working session sits at 30k-180k of cached context per turn.
        cache_read = rnd.randint(22_000, 140_000) + turn * rnd.randint(2_000, 9_000)
        cache_write = rnd.choice([0, 0, rnd.randint(2_000, 24_000)])
        inp = rnd.randint(3, 90)
        if "haiku" in model:
            out = rnd.randint(60, 700)
    elif tool == "codex":
        total_in = rnd.randint(15_000, 110_000) + turn * rnd.randint(1_500, 8_000)
        cache_read = int(total_in * rnd.uniform(0.55, 0.9))
        inp = total_in - cache_read
        cache_write = 0
        out += rnd.randint(200, 2200)  # reasoning tokens price as output
    else:  # gemini_cli
        total_in = rnd.randint(8_000, 70_000) + turn * rnd.randint(1_000, 5_000)
        cache_read = int(total_in * rnd.uniform(0.0, 0.7))
        inp = total_in - cache_read
        cache_write = 0
    return {"input": inp, "output": out,
            "cache_read": cache_read, "cache_write": cache_write}


def day_weights(days):
    """Weekday-heavy, gently growing, with one holiday gap and one crunch."""
    wts = []
    for i, d in enumerate(days):
        w = 1.0 + 1.4 * (i / max(1, len(days) - 1))  # usage grows over time
        if CRUNCH_START <= i < CRUNCH_START + CRUNCH_LEN:
            w *= 4.0  # a deadline: weekends included
        elif d.weekday() >= 5:
            w *= 0.45
        if GAP_START <= i < GAP_START + GAP_LEN:
            w = 0.0
        wts.append(w)
    return wts


def build_events(rnd: random.Random, target: int, end_date: str, span: int):
    end = dt.date.fromisoformat(end_date)
    days = [end - dt.timedelta(days=span - 1 - i) for i in range(span)]
    dwts = day_weights(days)
    proj_pairs = [(p, w) for p, w, _, _ in PROJECTS]
    proj_meta = {p: (topic, tools) for p, _, topic, tools in PROJECTS}

    events = []
    while len(events) < target:
        day = rnd.choices(days, weights=dwts, k=1)[0]
        hour = rnd.choices(range(24), weights=HOUR_WEIGHTS, k=1)[0]
        project = pick(rnd, proj_pairs)
        topic, tool_pref = proj_meta[project]
        tool = pick(rnd, list(tool_pref.items()))
        model = pick(rnd, MODELS[tool]) if MODELS[tool] else None
        sid = session_uuid(rnd)
        clock = dt.datetime(day.year, day.month, day.day, hour,
                            rnd.randrange(60), rnd.randrange(60),
                            tzinfo=dt.timezone.utc)
        turns = rnd.choices([1, 2, 3, 4, 6, 9], weights=[30, 26, 18, 12, 10, 4], k=1)[0]

        def emit(role, kind, text="", **kw):
            nonlocal clock
            clock += dt.timedelta(seconds=rnd.randint(4, 190))
            ev = {
                "ts": clock.isoformat(),
                "tool": tool,
                "session_id": sid,
                "project": project,
                "role": role,
                "kind": kind,
                "text": text,
                "tool_name": kw.get("tool_name"),
                "cmd": kw.get("cmd"),
                "exit_code": kw.get("exit_code"),
                "ts_exact": tool not in INEXACT_TS,
                "confidence": "exact",
                "tokens": kw.get("tokens"),
            }
            # Match Event.to_json: model/usage are omitted, not nulled, on the
            # events that carry no usage.
            if kw.get("model"):
                ev["model"] = kw["model"]
            if kw.get("usage"):
                ev["usage"] = kw["usage"]
            events.append(ev)

        for turn in range(turns):
            roll = rnd.random()
            if roll < 0.14:
                prompt = rnd.choice(AUTOMATION)
            elif roll < 0.48:
                prompt = rnd.choice(TOPIC_PROMPTS[topic])
            else:
                prompt = rnd.choice(PROMPTS)
            extra = rnd.random()
            if extra < 0.30:
                prompt = prompt + ". " + rnd.choice(PROMPTS)
                if extra < 0.08:
                    prompt = prompt + ". " + rnd.choice(TOPIC_PROMPTS[topic])
            emit("user", "prompt", prompt)

            if tool == "claude_code" and rnd.random() < 0.3:
                emit("assistant", "thinking", rnd.choice(THINKING))

            for _ in range(rnd.choices([0, 1, 2, 3, 5],
                                       weights=[30, 30, 20, 14, 6], k=1)[0]):
                name = rnd.choices(TOOL_NAMES, weights=TOOL_WEIGHTS, k=1)[0]
                cmd = rnd.choice(CMDS) if name == "Bash" else None
                text = "" if name == "Bash" else rnd.choice(FILES)
                emit("assistant", "tool_call", text, tool_name=name, cmd=cmd)
                if rnd.random() < 0.12:
                    emit("tool", "error", rnd.choice(ERRORS),
                         tool_name=name, exit_code=rnd.choice([1, 1, 2, 127]))
                else:
                    emit("tool", "tool_result", rnd.choice(TOOL_RESULTS),
                         tool_name=name, exit_code=0 if cmd else None)

            if model:
                u = usage_for(rnd, tool, model, turn)
                emit("assistant", "reply", rnd.choice(REPLIES), model=model,
                     usage=u, tokens=sum(u.values()))
            else:
                emit("assistant", "reply", rnd.choice(REPLIES))

    events.sort(key=lambda e: (e["ts"], e["session_id"]))
    return events


def summarize(events):
    by_tool = Counter(e["tool"] for e in events)
    by_kind = Counter(e["kind"] for e in events)
    sessions = {(e["tool"], e["session_id"]) for e in events}
    projects = {e["project"] for e in events}
    tokens = sum(sum(e["usage"].values()) for e in events if e.get("usage"))
    days = {e["ts"][:10] for e in events}
    lines = [
        f"events           {len(events):,}",
        f"sessions         {len(sessions):,}   projects {len(projects)}",
        f"window           {min(days)} .. {max(days)}  ({len(days)} active days)",
        f"tokens (usage)   {tokens:,}",
        "by tool          " + "  ".join(f"{t}={n}" for t, n in by_tool.most_common()),
        "by kind          " + "  ".join(f"{k}={n}" for k, n in by_kind.most_common()),
    ]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate a deterministic synthetic event corpus.")
    ap.add_argument("out", nargs="?", default=DEFAULT_OUT,
                    help=f"output NDJSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--events", type=int, default=TARGET_EVENTS,
                    help="approximate event count (a session is never cut short)")
    ap.add_argument("--end-date", default=END_DATE,
                    help="last day of the synthetic window (YYYY-MM-DD)")
    ap.add_argument("--days", type=int, default=SPAN_DAYS,
                    help="length of the window in days")
    args = ap.parse_args(argv)

    try:
        dt.date.fromisoformat(args.end_date)
    except ValueError:
        print(f"FATAL: --end-date must be YYYY-MM-DD, got {args.end_date!r}",
              file=sys.stderr)
        return 2
    if args.events < 1 or args.days < 7:
        print("FATAL: --events must be >= 1 and --days >= 7", file=sys.stderr)
        return 2

    rnd = random.Random(args.seed)
    events = build_events(rnd, args.events, args.end_date, args.days)

    parent = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False))
            fh.write("\n")

    print(summarize(events))
    print(f"\nwrote {args.out} "
          f"({os.path.getsize(args.out)/1e6:.2f} MB, seed {args.seed})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
