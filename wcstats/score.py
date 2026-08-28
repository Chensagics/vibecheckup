"""Scoring: frequency, distinctiveness, phrase promotion, and trends."""
from __future__ import annotations

import math
from collections import Counter


def top_n(counter: Counter, n: int):
    return [{"t": t, "n": c} for t, c in counter.most_common(n)]


def log_odds(target: Counter, background: Counter, n=200, min_count=3,
             alpha=0.01):
    """Monroe et al. log-odds ratio with an informative Dirichlet prior.

    Raw frequency makes every facet look identical -- each project and each
    tool would render "code, file, test". This surfaces what is *distinctive*
    about one corpus against the whole, with a z-score that damps rare terms
    instead of letting them dominate.
    """
    if not target:
        return []
    rest = Counter(background)
    rest.subtract(target)
    n_t = sum(target.values())
    n_r = sum(v for v in rest.values() if v > 0)
    if n_t == 0 or n_r <= 0:
        return []
    a0 = alpha * max(len(background), 1)
    out = []
    for w, y_t in target.items():
        if y_t < min_count:
            continue
        y_r = max(rest.get(w, 0), 0)
        a_w = alpha * max(background.get(w, 1), 1)
        num_t = y_t + a_w
        den_t = n_t + a0 - y_t - a_w
        num_r = y_r + a_w
        den_r = n_r + a0 - y_r - a_w
        if den_t <= 0 or den_r <= 0 or num_t <= 0 or num_r <= 0:
            continue
        delta = math.log(num_t / den_t) - math.log(num_r / den_r)
        var = 1.0 / num_t + 1.0 / num_r
        z = delta / math.sqrt(var) if var > 0 else 0.0
        out.append({"t": w, "n": y_t, "z": round(z, 3)})
    out.sort(key=lambda d: -d["z"])
    return out[:n]


def pmi_phrases(phrase_counts: Counter, unigrams: Counter, n=120,
                min_count=4, min_pmi=2.0):
    """Promote bigrams that co-occur far more than chance.

    Keeps "dev server" and "pull request" whole instead of shattering them
    into unrelated unigrams.
    """
    total_uni = sum(unigrams.values()) or 1
    total_bi = sum(phrase_counts.values()) or 1
    out = []
    for phrase, c in phrase_counts.items():
        if c < min_count:
            continue
        parts = phrase.split(" ")
        if len(parts) != 2:
            continue
        a, b = parts
        pa = unigrams.get(a, 0) / total_uni
        pb = unigrams.get(b, 0) / total_uni
        if pa <= 0 or pb <= 0:
            continue
        pab = c / total_bi
        pmi = math.log2(pab / (pa * pb))
        if pmi >= min_pmi:
            out.append({"t": phrase, "n": c, "pmi": round(pmi, 2)})
    out.sort(key=lambda d: -(d["n"] * d["pmi"]))
    return out[:n]


def trends(month_counts, months, min_total=8, n=40):
    """Rising and fading terms by month-over-month rate change.

    Compares the first and second half of the covered range using rates, not
    raw counts, so a month with more sessions does not read as a vocabulary
    shift.
    """
    if len(months) < 2:
        return {"rising": [], "fading": []}
    half = len(months) // 2
    early, late = months[:half], months[half:]

    def rates(group):
        agg = Counter()
        total = 0
        for m in group:
            c = month_counts.get(m)
            if not c:
                continue
            agg.update(c)
            total += sum(c.values())
        return agg, max(total, 1)

    e_c, e_n = rates(early)
    l_c, l_n = rates(late)
    vocab = set(e_c) | set(l_c)
    out = []
    for w in vocab:
        te, tl = e_c.get(w, 0), l_c.get(w, 0)
        if te + tl < min_total:
            continue
        # Per-100k rates, smoothed so a zero side does not blow up.
        re_ = (te + 0.5) / e_n * 1e5
        rl_ = (tl + 0.5) / l_n * 1e5
        out.append({"t": w, "early": round(re_, 2), "late": round(rl_, 2),
                    "lift": round(math.log2(rl_ / re_), 3),
                    "n": te + tl})
    rising = sorted(out, key=lambda d: -d["lift"])[:n]
    fading = sorted(out, key=lambda d: d["lift"])[:n]
    return {"rising": rising, "fading": fading,
            "early_months": early, "late_months": late}
