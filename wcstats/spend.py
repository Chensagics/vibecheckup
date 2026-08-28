"""Token usage -> dollars.

Everything here is a *list-price estimate*. No adapter has access to a bill,
discounts, subscription bundling, or the promotional rates that were live when
a given turn ran, so the number is an order-of-magnitude honest reconstruction
and is labelled as one everywhere it surfaces.

The one rule that matters: a model with no price entry costs `null`, never
zero. Silently zeroing an unknown model turns "we do not know" into "it was
free", which is the failure mode that makes a spend dashboard worse than no
spend dashboard.
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "prices.json")

CURRENCY = "USD"
ESTIMATES_NOTE = "list-price estimates, not a bill"

# Mirrors adapters.base.USAGE_KEYS. Duplicated rather than imported so wcstats
# stays independent of the adapter layer; the contract test pins them together.
USAGE_KEYS = ("input", "output", "cache_read", "cache_write")

# Sources whose logs carry no token counts at all (verified on real logs:
# Grok records model_id but no usage; Antigravity's protobuf has no token
# fields). They are named explicitly so the dashboard can badge them instead
# of rendering them as a $0 line.
TOOLS_WITHOUT_USAGE = ("grok", "antigravity")

# Providers that report cached tokens as a discounted SUBSET of input rather
# than as a separate bucket. Adapters move that subset into `cache_read`; this
# set is what decides which column of the price table it is billed against.
SUBSET_CACHE_TOOLS = {"codex", "gemini_cli"}

MAX_MODELS = 60
MAX_TOOLS = 20

_MILLION = 1_000_000.0


def load_prices(path=PRICES):
    """Price entries, longest pattern first so prefix matching is greedy."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    rows = doc if isinstance(doc, list) else (doc.get("models") or [])
    out = [r for r in rows if isinstance(r, dict) and r.get("pattern")]
    out.sort(key=lambda r: -len(r["pattern"]))
    return out


def match_price(model, prices):
    """Longest-prefix match, so `gpt-5.4-mini` beats `gpt-5`. None if unknown."""
    if not model:
        return None
    for row in prices:
        if model.startswith(row["pattern"]):
            return row
    return None


def event_cost(usage, model, tool, prices):
    """Dollars for one event's usage, or None when the model has no price.

    `usage["input"]` is already cache-exclusive, so the components are
    disjoint and simply add. Which *rate* the cached bucket takes depends on
    the provider: Anthropic bills cache reads as their own line
    (`cache_read`), while OpenAI and Google discount a subset of input
    (`cached_input`).
    """
    row = match_price(model, prices)
    if row is None or not usage:
        return None
    cache_rate = row["cached_input"] if tool in SUBSET_CACHE_TOOLS else row["cache_read"]
    return (usage.get("input", 0) * row["input"]
            + usage.get("output", 0) * row["output"]
            + usage.get("cache_read", 0) * cache_rate
            + usage.get("cache_write", 0) * row["cache_write"]) / _MILLION


def local_date(ts):
    """UTC ISO timestamp -> the local calendar date it happened on.

    A 2am prompt in UTC+3 belongs to the day the user experienced, not to the
    previous UTC day, and "what did I spend on Tuesday" is a local question.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().date().isoformat()
    except (ValueError, AttributeError, OSError, OverflowError):
        pass
    # An unparseable timestamp must not become a by_day key; fall back to the
    # date head only when it is a real calendar date.
    try:
        return date.fromisoformat(str(ts)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


class Spend:
    """Accumulates the `spend` section of stats.json, one event at a time."""

    def __init__(self, prices=None):
        self.prices = load_prices() if prices is None else prices
        self.by_day = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
        self.by_model = defaultdict(Counter)      # (model, tool) -> token counts
        self.model_cost = defaultdict(float)
        self.model_priced = {}                    # (model, tool) -> bool
        self.by_tool = defaultdict(lambda: {"cost": 0.0, "tokens": 0})
        self.tool_sessions = defaultdict(set)
        self.tools_seen = set()
        self.tools_with_usage = set()
        self.unpriced = set()
        self.totals = Counter()
        self.total_cost = 0.0
        self.events = 0

    def add(self, ev):
        """Feed one raw event dict; events without usage are ignored."""
        tool = ev.get("tool") or "unknown"
        self.tools_seen.add(tool)
        u = ev.get("usage")
        if not isinstance(u, dict):
            return
        counts = {k: int(u.get(k) or 0) for k in USAGE_KEYS}
        tokens = sum(counts.values())
        if tokens <= 0:
            return

        model = ev.get("model") or "unknown"
        cost = event_cost(counts, ev.get("model"), tool, self.prices)
        priced = cost is not None
        if not priced:
            self.unpriced.add(model)
            cost = 0.0

        self.events += 1
        self.tools_with_usage.add(tool)
        self.totals.update(counts)
        self.total_cost += cost

        key = (model, tool)
        self.by_model[key].update(counts)
        self.model_cost[key] += cost
        self.model_priced[key] = priced

        t = self.by_tool[tool]
        t["cost"] += cost
        t["tokens"] += tokens
        sid = ev.get("session_id")
        if sid:
            self.tool_sessions[tool].add(sid)

        day = local_date(ev.get("ts"))
        if day:
            d = self.by_day[day]
            d["cost"] += cost
            d["tokens"] += tokens

    def cache_hit_rate(self):
        """Share of all prompt-side tokens that were served from cache."""
        prompt_side = (self.totals["input"] + self.totals["cache_read"]
                       + self.totals["cache_write"])
        if prompt_side <= 0:
            return 0.0
        return round(self.totals["cache_read"] / prompt_side, 4)

    def priciest_day(self):
        if not self.by_day:
            return {"date": "", "cost": 0.0}
        day, d = max(self.by_day.items(), key=lambda kv: kv[1]["cost"])
        return {"date": day, "cost": round(d["cost"], 2)}

    def render(self):
        models = []
        for (model, tool), c in self.by_model.items():
            models.append({
                "model": model, "tool": tool,
                "cost": (round(self.model_cost[(model, tool)], 4)
                         if self.model_priced[(model, tool)] else None),
                "input": c["input"], "output": c["output"],
                "cache_read": c["cache_read"], "cache_write": c["cache_write"],
            })
        models.sort(key=lambda m: (-(m["cost"] or 0.0),
                                   -(m["input"] + m["output"])))

        tools = [{"tool": t,
                  "cost": round(v["cost"], 4),
                  "tokens": v["tokens"],
                  "sessions": len(self.tool_sessions[t])}
                 for t, v in self.by_tool.items()]
        tools.sort(key=lambda x: -x["cost"])

        return {
            "currency": CURRENCY,
            "estimates_note": ESTIMATES_NOTE,
            "total_cost": round(self.total_cost, 2),
            "total_tokens": sum(self.totals.values()),
            "by_day": [{"date": d, "cost": round(v["cost"], 4),
                        "tokens": v["tokens"]}
                       for d, v in sorted(self.by_day.items())],
            "by_model": models[:MAX_MODELS],
            "by_tool": tools[:MAX_TOOLS],
            "cache_hit_rate": self.cache_hit_rate(),
            "unpriced_models": sorted(self.unpriced),
            # Derived, not declared: any source that produced events but never
            # a token count. TOOLS_WITHOUT_USAGE is the documented expectation,
            # and a new name showing up here means an upstream format changed.
            "tools_without_usage": sorted(self.tools_seen - self.tools_with_usage),
            # Stable shape even when empty, so the dashboard never has to
            # guard a missing key to draw a zeroed KPI tile.
            "tokens": {k: self.totals[k] for k in USAGE_KEYS},
            "events": self.events,
        }
