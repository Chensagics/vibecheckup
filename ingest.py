#!/usr/bin/env python3
"""Stage 1: read every local AI session log -> data/events.ndjson

Adapter failures are per-file and non-fatal. Unknown record types are counted
and reported by name, so a silent upstream format change is visible rather than
quietly dropping data.

This stage also writes a redaction sidecar next to the corpus. Nothing here is
masked -- events.ndjson is the local, gitignored source and stays complete --
but ingest is the only stage that touches the filesystem, so it is the only one
that can see which repositories the sessions ran in and which worktree branch
names those paths carry. Those go in the sidecar for analyze.py, which is the
stage that publishes.

The sidecar NAMES the strings that get masked, so it is as private as the
corpus it sits next to -- see SIDECAR_WARNING for why it exists at all and why
it is written owner-readable only.
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
from adapters.base import OBSERVED, git_config_texts  # noqa: E402
from wcstats.clean import (REDACT_HELP, build_redaction,  # noqa: E402
                           format_redaction)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "events.ndjson")
# Derived from --out, never hard-coded, exactly like analyze.py's vocab
# sidecar: a scratch run must not overwrite the real one.
REDACT_NAME = "redact.json"


def redact_path(out):
    """The redaction sidecar that belongs to this --out."""
    return os.path.join(os.path.dirname(os.path.abspath(out)), REDACT_NAME)


# build_redaction()'s own docstring says a redaction report "must never be
# written into stats.json or vocab.json ... a list of exactly the strings
# somebody wanted hidden is the leak, spelled out" -- and this file is that
# list. It is kept anyway, and the reason is narrow: two of the things in it
# cannot be derived anywhere else.
#
#   * the account half of the git remotes, which needs `.git/config` on the
#     machine the sessions ran on;
#   * which repo each worktree branch belongs to, which needs the worktree to
#     still exist on disk.
#
# analyze.py publishes and never sees a filesystem, so without the sidecar
# those two are simply lost and the strings they would have masked ship. What
# the sidecar is NOT is a shareable artifact: it lives beside events.ndjson,
# which is already the unredacted corpus, in a gitignored directory, and it is
# written owner-readable only with its own warning inside it. If you would not
# send somebody events.ndjson, do not send them data/.
SIDECAR_WARNING = (
    "PRIVATE — do not share. This is the list of literals vibecheckup masks "
    "out of stats.json, vocab.json and the dashboard: account names, and the "
    "worktree branch names of your repositories. Handing this file to somebody "
    "is handing them the answer key. analyze.py reads it; nothing else should."
)
SIDECAR_MODE = 0o600


def write_redaction(out, extra):
    """Derive who is running this and what they were branching, and record it.

    Returns the report so the caller can print it. Failing to write the
    sidecar is a warning, not an error: analyze.py re-derives the local
    identities itself and recovers branch names from the event stream, so the
    sidecar only adds the part that needs the filesystem.
    """
    _r, report = build_redaction(
        repo_configs=git_config_texts(OBSERVED.dirs),
        branch_pairs=OBSERVED.pairs(),
        extra=extra)
    path = redact_path(out)
    payload = {k: report[k] for k in
               ("identities", "branches", "decorated", "explicit")}
    payload["_warning"] = SIDECAR_WARNING
    # branch -> the repo it belongs to. Only the filesystem knows that a
    # directory called `finn-loop-writer` is a linked worktree of `finn` and
    # not a repository of its own, so a corpus ingested before that was
    # resolved cannot be repaired from its labels alone -- carrying the pairing
    # here is what lets analyze.py re-attribute those events instead of
    # masking them. Redaction ignores this key; label repair is its only use.
    payload["branch_repos"] = {b: r for b, r in OBSERVED.pairs() if r}
    try:
        # Owner-only, and set on the descriptor rather than after the write, so
        # the contents are never briefly world-readable. An existing file keeps
        # whatever mode it has, so tighten it explicitly too.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SIDECAR_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.chmod(path, SIDECAR_MODE)
    except OSError as exc:
        print(f"  warning: could not write {path}: {exc}", file=sys.stderr)
        return report, None
    return report, path


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


def warn_partial(args, report):
    """One line when a --tool/--limit smoke run is about to replace a full one.

    Not an error and not a prompt: the run is exactly what was asked for. It
    just says so once, so a suddenly thinner dashboard is never a mystery.
    """
    if not (args.tool or args.limit):
        return
    try:
        prev = os.path.getsize(args.out)
    except OSError:
        return
    if prev <= 0:
        return
    flags = []
    if args.tool:
        flags.append("--tool " + " --tool ".join(args.tool))
    if args.limit:
        flags.append(f"--limit {args.limit}")
    n = sum(report.files.values())
    print(f"  note: {' '.join(flags)} makes this a partial run of {n} "
          f"file{'' if n == 1 else 's'}; it replaces the existing {args.out} "
          f"({prev/1e6:.1f} MB). Re-run with no flags for the full corpus.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", action="append", choices=list(ADAPTERS),
                    help="limit to one or more sources (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="max files per source (0 = all)")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--redact", action="append", default=[], metavar="STR",
                    help=REDACT_HELP)
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)
    tools = args.tool or list(ADAPTERS)
    report = Report()
    t0 = time.time()

    # Discover everything before opening the output. A run that finds nothing
    # must leave a previous corpus alone rather than truncating it on the way
    # to an empty file.
    found = {}
    for tool in tools:
        files = ADAPTERS[tool].discover()
        if args.limit:
            files = files[:args.limit]
        found[tool] = files
        report.files[tool] = len(files)

    if sum(report.files.values()) == 0:
        if args.tool:
            print(f"\nno session logs found for {', '.join(tools)} on this "
                  f"machine — {args.out} was left as it was.")
        else:
            print(f"\nno session logs found — none of the {len(ADAPTERS)} "
                  f"supported tools ({', '.join(ADAPTERS)}) has session logs "
                  f"on this machine.")
        print("try the synthetic corpus instead:  ./vibecheckup.sh --demo")
        return 1

    warn_partial(args, report)

    # Write beside the target and rename over it only once the whole corpus is
    # on disk: a crash, a Ctrl-C or a partial --tool/--limit run must never
    # leave the previous events.ndjson truncated. Same directory, so os.replace
    # stays atomic.
    tmp = args.out + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            for tool in tools:
                mod = ADAPTERS[tool]
                files = found[tool]
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
    except BaseException:
        # Half a corpus is worse than none: drop it and keep what was there.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, args.out)

    print(report.render())
    size = os.path.getsize(args.out)
    print(f"\nwrote {args.out} ({size/1e6:.1f} MB) in {time.time()-t0:.1f}s")

    red, red_path = write_redaction(args.out, args.redact)
    print()
    print(format_redaction(red))
    if red_path:
        print(f"  wrote {red_path} (mode 0600, local only; analyze.py reads "
              f"it). It NAMES the strings that get masked — private, like the "
              f"rest of {os.path.dirname(red_path)}.")
    fatal = report.fatal
    if fatal:
        print(f"\nFATAL: sources found files but parsed none: {', '.join(fatal)}")
        return 1
    print("\nFATAL ERRORS: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
