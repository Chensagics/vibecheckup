"""Facet accumulation: one bucket per global / tool / project / month slice."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .score import log_odds, pmi_phrases, top_n

RE_EXT = re.compile(r"\.([a-zA-Z][a-zA-Z0-9]{0,6})$")
RE_MASK_NUM = re.compile(r"\d+")
RE_MASK_PATH = re.compile(r"(?:/[\w.\-@+]+)+")
RE_MASK_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
RE_MASK_QUOTE = re.compile(r"'[^']{1,80}'|\"[^\"]{1,80}\"")

SHELL_SUBCMD = {"git", "npm", "pnpm", "yarn", "docker", "cargo", "go", "kubectl",
                "brew", "pip", "pip3", "vercel", "gh", "expo", "eas", "supabase"}


def error_signature(text: str) -> str:
    """Collapse a failure message to a comparable signature."""
    if not text:
        return ""
    line = ""
    for ln in text.splitlines():
        s = ln.strip()
        low = s.lower()
        if any(k in low for k in ("error", "exception", "failed", "fatal",
                                  "traceback", "cannot", "not found",
                                  "permission denied", "refused")):
            line = s
            break
    if not line:
        line = (text.strip().splitlines() or [""])[0]
    line = RE_MASK_QUOTE.sub("'X'", line)
    line = RE_MASK_PATH.sub("PATH", line)
    line = RE_MASK_HEX.sub("HEX", line)
    line = RE_MASK_NUM.sub("N", line)
    line = " ".join(line.split())
    return line[:110]


class Bucket:
    """Counters for one facet slice."""

    __slots__ = ("prose_user", "prose_asst", "phrases_user", "phrases_asst",
                 "tools", "commands", "exts", "errors", "events", "prompts",
                 "words", "llm_tokens", "sessions", "seen_prompts",
                 "prose_user_raw")

    def __init__(self):
        self.prose_user = Counter()      # collapsed (near-dupes once)
        self.prose_user_raw = Counter()  # every occurrence
        self.prose_asst = Counter()
        self.phrases_user = Counter()
        self.phrases_asst = Counter()
        self.tools = Counter()
        self.commands = Counter()
        self.exts = Counter()
        self.errors = Counter()
        self.events = 0
        self.prompts = 0
        self.words = 0
        self.llm_tokens = 0
        self.sessions = set()
        self.seen_prompts = set()

    def add_prompt(self, toks, phrases, dup_key):
        self.prompts += 1
        self.words += len(toks)
        self.prose_user_raw.update(toks)
        if dup_key and dup_key in self.seen_prompts:
            return
        if dup_key:
            self.seen_prompts.add(dup_key)
        self.prose_user.update(toks)
        self.phrases_user.update(phrases)

    def add_assistant(self, toks, phrases):
        self.words += len(toks)
        self.prose_asst.update(toks)
        self.phrases_asst.update(phrases)

    def render(self, bg_user, bg_asst, n=300, distinctive=True):
        out = {
            "prose_user": top_n(self.prose_user, n),
            "prose_user_raw": top_n(self.prose_user_raw, n),
            "prose_assistant": top_n(self.prose_asst, n),
            "phrases_user": pmi_phrases(self.phrases_user, self.prose_user, n=80),
            "phrases_assistant": pmi_phrases(self.phrases_asst, self.prose_asst, n=80),
            "tools": top_n(self.tools, 60),
            "commands": top_n(self.commands, 60),
            "extensions": top_n(self.exts, 40),
            "errors": top_n(self.errors, 40),
            "counts": {
                "events": self.events,
                "prompts": self.prompts,
                "words": self.words,
                "sessions": len(self.sessions),
                "llm_tokens": self.llm_tokens,
            },
        }
        if distinctive:
            out["distinctive_user"] = log_odds(self.prose_user, bg_user, n=120)
            out["distinctive_assistant"] = log_odds(self.prose_asst, bg_asst, n=120)
        return out


class Facets:
    def __init__(self):
        self.buckets = defaultdict(Bucket)

    def get(self, kind, key):
        return self.buckets[(kind, key)]

    def keys(self, kind):
        return [k for (t, k) in self.buckets if t == kind]
