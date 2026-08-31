"""The Agent Wrapped counters: the handful of numbers that go on a share card.

Privacy rule, enforced here rather than in the dashboard: nothing in this
section names a project or a path. Counts and vocabulary only -- the words are
what the user is deliberately choosing to share, everything else is a number.

The politeness counters run over *cleaned* prose (wcstats.clean), so a "please"
in an injected skill instruction, a hook payload or a pasted config cannot be
credited to the user.

What cleaning CANNOT tell you is who put the words in the turn. A `prompt`
event is whatever arrived in the user's turn -- typed, pasted, or injected by a
skill or tool -- and nothing downstream of here can separate those. That is why
every counter in this module is named for what it measures ("words to AI") and
why the copy that renders it says *sent*, *used* and *came up* rather than
*typed*: the numbers are true about the user's side of the conversation, and
saying more than that would be a claim the data cannot support.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, timedelta

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Deliberately narrow. "thx" counts, "thanksgiving" does not; "thank you"
# matches as one thanks rather than as a thank plus a you.
#
# Manners were the whole of this section once, and readers said so: a year of
# talking to a machine is not three counts of "please". What a person actually
# does is swear at it, get exasperated, praise it, and demand things now, and
# none of that was anywhere on the card. Every family below is counted the
# same way -- word-boundary matches over *cleaned* prose, so a "please" in an
# injected skill definition still cannot be credited to the owner.
#
# Every pattern is anchored on \b at both ends for a reason: "classic" is not
# a curse, "massachusetts" is not one either, and "shitake" is a mushroom. The
# stems that do inflect ("fuck" -> fucked, fucking) opt in explicitly rather
# than by a blanket \w*.
EMOTIONS = {
    "please": re.compile(r"\bplease\b|\bpls\b", re.I),
    "thanks": re.compile(r"\bthanks?\s+you\b|\bthank\s+you\b|\bthanks\b|\bthx\b|\bty\b", re.I),
    "sorry": re.compile(r"\bsorry\b|\bapolog(?:y|ies|ise|ize)\b", re.I),
    "profanity": re.compile(
        r"\bfuck(?:ed|ing|er|ers|s)?\b|\bfck\b|\bwtf\b|\bffs\b"
        r"|\bshit(?:ty)?\b|\bbullshit\b|\bcrap(?:py)?\b"
        r"|\bdamn(?:ed|it)?\b|\bdammit\b|\bgoddamn\b"
        r"|\bhell\b|\bbloody\b|\barse\b|\bass(?:hole)?\b|\bbastard\b",
        re.I),
    "frustration": re.compile(
        r"\bugh+\b|\bargh+\b|\baargh+\b|\bffs\b|\bseriously\b|\bagain\?"
        r"|\bstill\s+(?:broken|not|doesn|does\s+not|failing|wrong)"
        r"|\bcome\s+on\b|\bfor\s+the\s+love\s+of\b|\bno+pe\b"
        r"|\bwhy\s+(?:is|isn|are|aren|does|doesn|did|didn|won|can)"
        r"|\bi\s+already\s+(?:told|said|asked)\b|\bstop\s+doing\b",
        re.I),
    # `exactly` was 453 of the 530 hits this family had on its first run --
    # "do exactly what it says" is an instruction, not applause. Same story
    # for a bare `love`, which is mostly somebody loving an approach in
    # passing. Both are out; what is left only fires on approval.
    "praise": re.compile(
        r"\bperfect\b|\bbeautiful(?:ly)?\b|\bexcellent\b|\bbrilliant\b"
        r"|\bamazing\b|\bawesome\b|\bnice\b|\bgreat\s+(?:work|job|stuff)\b"
        r"|\blove\s+(?:it|this|that)\b|\bgorgeous\b|\bslick\b"
        r"|\bnailed\s+it\b|\bmuch\s+better\b|\bgood\s+enough\b",
        re.I),
    # `immediately` was 384 of 529 and is nearly all machine phrasing ("stop
    # immediately", "return immediately"); `quick` and `fast` are usually
    # describing software, not asking for haste. All three are out.
    "urgency": re.compile(
        r"\basap\b|\bright\s+now\b|\burgent(?:ly)?\b|\bhurry\b"
        r"|\bquickly\b|\bright\s+away\b|\bdrop\s+everything\b"
        r"|\bbefore\s+(?:the\s+)?(?:demo|deadline|standup|meeting|launch)\b",
        re.I),
    "confusion": re.compile(
        r"\bconfus(?:ed|ing)\b|\bdon'?t\s+understand\b|\bno\s+idea\b"
        r"|\bwhat\s+does\s+(?:that|this|it)\s+mean\b|\bhuh\b"
        r"|\bmakes?\s+no\s+sense\b|\bi'?m\s+lost\b",
        re.I),
}

# The three that were on the card before. Kept as their own name so an older
# stats.json, and the card's manners panel, keep reading what they always read.
POLITENESS = {k: EMOTIONS[k] for k in ("please", "thanks", "sorry")}

# The families that are not manners, in the order the card shows them.
FEELINGS = ("profanity", "frustration", "praise", "urgency", "confusion")


def emotion_counts(text):
    """Every emotion family's hit count for one piece of cleaned prose."""
    out = Counter()
    if not text:
        return out
    for name, pat in EMOTIONS.items():
        n = len(pat.findall(text))
        if n:
            out[name] += n
    return out

TOP_WORDS = 10

#: Longest prompt still treated as something a person typed rather than pasted
#: or generated. The median real prompt in a full corpus is 17 words.
HUMAN_SCALE_WORDS = 200


class Signature:
    """Which repeated phrase is a habit rather than a topic.

    The card used to print the highest-scoring PMI collocation and call it a
    signature. That is a *topic*: "day trading" won because one project talked
    about day trading constantly, and the owner did not recognise it as
    anything he says.

    A habit is distinguished by breadth, not volume. It turns up wherever the
    person is working, while a topic is dense inside one codebase and absent
    everywhere else -- so rank on the number of distinct projects first, and
    use the number of separate prompts only to break ties. A phrase said in six
    projects beats one said sixty times in a single project.
    """

    #: Below this many separate prompts a phrase is a coincidence, not a tell.
    MIN_PROMPTS = 3

    def __init__(self):
        self.projects = defaultdict(set)
        self.prompts = Counter()

    def add(self, grams, project, prompt_id=None):
        """One prompt's n-grams. Counted once each, however often repeated."""
        for g in set(grams):
            self.prompts[g] += 1
            if project:
                self.projects[g].add(project)

    def ranked(self, n=10):
        out = []
        for g, c in self.prompts.items():
            if c < self.MIN_PROMPTS:
                continue
            out.append({"t": g, "n": c, "projects": len(self.projects[g])})
        out.sort(key=lambda d: (-d["projects"], -d["n"], d["t"]))
        return out[:n]

    def best(self):
        top = self.ranked(1)
        return top[0]["t"] if top else ""


class Wrapped:
    """Accumulates the wrapped counters alongside the main analyze pass."""

    def __init__(self):
        self.emotions = Counter()
        self.words_to_ai = 0
        self.prompts_by_tool = Counter()
        self.prompt_days = set()
        self.signature = Signature()
        self.human_prompts = 0

    @property
    def politeness(self):
        """The three manners counters, kept under their own name."""
        return Counter({k: self.emotions.get(k, 0) for k in POLITENESS})

    def add_user_prose(self, tool, cleaned, day, project=None, prompt_id=None,
                       grams=()):
        """One user prompt, already stripped of injected and machine text."""
        self.prompts_by_tool[tool] += 1
        if day:
            self.prompt_days.add(day)
        if not cleaned:
            return
        # Every word that went to the model, not just the content tokens --
        # "N words sent to an AI this year" is the headline, and dropping
        # stopwords would quietly halve it. This one counts everything: a
        # pasted brief was still sent, and the claim is about volume.
        n_words = len(cleaned.split())
        self.words_to_ai += n_words
        # Tone and habit are claims about the *person*, and the median typed
        # prompt in a real corpus is 17 words while the tail runs past 2,900.
        # Those long ones are generated briefs and pasted specs; letting them
        # vote put "gemini.md and save" on the card as a signature phrase and
        # made "immediately" read as urgency. Volume counts everything, voice
        # counts only what a person plausibly typed.
        if n_words > HUMAN_SCALE_WORDS:
            return
        self.human_prompts += 1
        self.emotions.update(emotion_counts(cleaned))
        if grams:
            self.signature.add(grams, project, prompt_id)

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


# The privacy rule this module exists to enforce -- no directory names, no
# file paths -- was written when only the owner's own words went on the card.
# The model's words are quoted on it now and are held to exactly the same line.
def _shareable(term):
    t = str(term or "")
    return bool(t) and "/" not in t and "\\" not in t and not t.startswith(".")


def _clean_pairs(items, limit):
    out = []
    for d in items or ():
        t = d.get("t") if isinstance(d, dict) else (d[0] if d else "")
        n = d.get("n", 0) if isinstance(d, dict) else (d[1] if len(d) > 1 else 0)
        if _shareable(t):
            out.append([t, n])
        if len(out) >= limit:
            break
    return out


def _first_clean(items):
    pairs = _clean_pairs(items, 1)
    return pairs[0][0] if pairs else ""


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
    signature = wrapped.signature.best()
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
        # The signature is a habit if we found one, and falls back to the old
        # collocation only when the corpus is too thin to show a habit at all.
        "top_phrase": signature or (stats_pieces["top_phrases"][0]["t"]
                                    if stats_pieces["top_phrases"] else ""),
        "signature_phrases": wrapped.signature.ranked(5),
        # What the machine said back. Computed all along and shown only in the
        # Lexicon tab, which meant the half of the year that is the model's own
        # voice never reached the card anybody actually shares.
        "assistant_top_words": _clean_pairs(
            stats_pieces.get("assistant_top_words"), TOP_WORDS),
        "assistant_top_phrase": _first_clean(
            stats_pieces.get("assistant_top_phrases")),
        "assistant_top_phrases": [
            t for t, _ in _clean_pairs(
                stats_pieces.get("assistant_top_phrases"), 5)],
        "rising_word": (stats_pieces["rising"][0]["t"]
                        if stats_pieces["rising"] else ""),
        "peak_hour": int(_argmax(stats_pieces["hour_histogram"], 0) or 0),
        "peak_weekday": _argmax(stats_pieces["weekday_histogram"], "") or "",
        "busiest_day": {"date": busiest[0], "prompts": busiest[1]},
        "politeness": {k: wrapped.emotions.get(k, 0) for k in POLITENESS},
        "emotions": {k: wrapped.emotions.get(k, 0) for k in EMOTIONS},
        "top_tool": wrapped.top_tool(),
        "tools_used": stats_pieces["tools_used"],
        "projects_count": stats_pieces["projects_count"],
        "spend": {"total": spend.get("total_cost", 0.0), "priciest_day": priciest},
    }
