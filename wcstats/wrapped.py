"""The Agent Wrapped counters: the handful of numbers that go on a share card.

Privacy rule, enforced here rather than in the dashboard: nothing in this
section names a project or a path. Counts and vocabulary only -- the words are
what the user is deliberately choosing to share, everything else is a number.

The politeness counters run over *cleaned* prose (wcstats.clean), so the
"please" in an injected skill instruction or a hook payload can never be
credited to the user.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Deliberately narrow. "thx" counts, "thanksgiving" does not; "thank you"
# matches as one thanks rather than as a thank plus a you.
POLITENESS = {
    "please": re.compile(r"\bplease\b|\bpls\b", re.I),
    "thanks": re.compile(r"\bthanks?\s+you\b|\bthank\s+you\b|\bthanks\b|\bthx\b|\bty\b", re.I),
    "sorry": re.compile(r"\bsorry\b|\bapolog(?:y|ies|ise|ize)\b", re.I),
}

TOP_WORDS = 10


class Wrapped:
    """Accumulates the wrapped counters alongside the main analyze pass."""

    def __init__(self):
        self.politeness = Counter()
        self.words_to_ai = 0
        self.prompts_by_tool = Counter()
        self.prompt_days = set()

    def add_user_prose(self, tool, cleaned, day):
        """One user prompt, already stripped of injected and machine text."""
        self.prompts_by_tool[tool] += 1
        if day:
            self.prompt_days.add(day)
        if not cleaned:
            return
        # Every word the user actually wrote, not just the content tokens --
        # "you typed N words at a machine this year" is the headline, and
        # dropping stopwords would quietly halve it.
        self.words_to_ai += len(cleaned.split())
        for name, pat in POLITENESS.items():
            n = len(pat.findall(cleaned))
            if n:
                self.politeness[name] += n

    def top_tool(self):
        if not self.prompts_by_tool:
            return {"name": "", "share": 0.0}
        name, n = self.prompts_by_tool.most_common(1)[0]
        total = sum(self.prompts_by_tool.values())
        return {"name": name, "share": round(n / total, 4) if total else 0.0}


def longest_streak(days):
    """Longest run of consecutive calendar days that each hold >=1 prompt."""
    parsed = sorted(_as_date(d) for d in days if _as_date(d))
    if not parsed:
        return 0
    best = run = 1
    for prev, cur in zip(parsed, parsed[1:]):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        best = max(best, run)
    return best


def _as_date(s):
    try:
        y, m, d = (int(x) for x in str(s).split("-")[:3])
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def _argmax(counter, default=None):
    if not counter:
        return default
    return max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0]


def build(wrapped, stats_pieces):
    """Assemble the `wrapped` section from the accumulated counters.

    `stats_pieces` carries the values analyze.py has already computed, so the
    wrapped card and the rest of the dashboard can never disagree.
    """
    per_day = stats_pieces["per_day"]
    days = sorted(per_day)
    busiest = max(per_day.items(), key=lambda kv: kv[1]) if per_day else ("", 0)
    spend = stats_pieces.get("spend") or {}
    priciest = stats_pieces.get("priciest_day") or {"date": "", "cost": 0.0}
    end = days[-1] if days else ""

    return {
        "window": {"start": days[0] if days else "", "end": end},
        "year": int(end[:4]) if len(end) >= 4 and end[:4].isdigit() else 0,
        "words_to_ai": wrapped.words_to_ai,
        "prompts": stats_pieces["prompts"],
        "sessions": stats_pieces["sessions"],
        "days_active": len(per_day),
        "longest_streak_days": longest_streak(per_day),
        "top_words": [[d["t"], d["n"]] for d in stats_pieces["top_words"][:TOP_WORDS]],
        "top_phrase": (stats_pieces["top_phrases"][0]["t"]
                       if stats_pieces["top_phrases"] else ""),
        "rising_word": (stats_pieces["rising"][0]["t"]
                        if stats_pieces["rising"] else ""),
        "peak_hour": int(_argmax(stats_pieces["hour_histogram"], 0) or 0),
        "peak_weekday": _argmax(stats_pieces["weekday_histogram"], "") or "",
        "busiest_day": {"date": busiest[0], "prompts": busiest[1]},
        "politeness": {k: wrapped.politeness.get(k, 0) for k in POLITENESS},
        "top_tool": wrapped.top_tool(),
        "tools_used": stats_pieces["tools_used"],
        "projects_count": stats_pieces["projects_count"],
        "spend": {"total": spend.get("total_cost", 0.0), "priciest_day": priciest},
    }
