"""Tokenization, stopwords, phrase detection, and near-duplicate clustering."""
from __future__ import annotations

import re
from collections import Counter

RE_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z0-9_+#'\.\-]*")

# Standard English function words.
BASE_STOP = set("""
a about above after again against all am an and any are aren't as at be because
been before being below between both but by can cannot could couldn't did didn't
do does doesn't doing don't down during each few for from further had hadn't has
hasn't have haven't having he he'd he'll he's her here here's hers herself him
himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself
let's me more most mustn't my myself no nor not of off on once only or other
ought our ours ourselves out over own same shan't she she'd she'll she's should
shouldn't so some such than that that's the their theirs them themselves then
there there's these they they'd they'll they're they've this those through to too
under until up very was wasn't we we'd we'll we're we've were weren't what what's
when when's where where's which while who who's whom why why's with won't would
wouldn't you you'd you'll you're you've your yours yourself yourselves
""".split())

# Conversational filler and agent-transcript boilerplate. Deliberately does NOT
# include domain verbs (fix, build, test, deploy, refactor, debug...) -- those
# are exactly what the clouds should show.
CHAT_STOP = set("""
ok okay yes yeah yep nope sure thanks thank please just also now still even
maybe perhaps really quite very much many lot lots bit little good great nice
right wrong sorry hmm well actually basically simply
i'll i'd we'll let lets like want need going go got get gets getting make makes
making made take takes taking see seen look looks looking know knows knowing
think thinks thought say says said tell tells told use uses used using try tries
tried put puts one two three first second next last thing things way ways
something anything nothing everything someone anyone everyone able based
instead however therefore thus hence etc via per within without across around
already always never often sometimes usually probably possibly likely
here's there's what's that's it's let's don't doesn't isn't aren't wasn't
weren't can't won't wouldn't shouldn't couldn't didn't hasn't haven't hadn't
you're we're they're i've we've they've you've he's she's
above below again once more most other another each every both either neither
new old current currently
""".split())

# Transcript-specific noise that survives cleaning.
AGENT_STOP = set("""
user assistant claude codex gemini grok agent model response message messages
tool tools call calls result results output input prompt prompts session
sessions turn turns content text token tokens context window
please note however overall summary done finished complete completed
""".split())

# The corpus contains substantial translation work, so other languages'
# function words surface as if they were vocabulary. Function words only --
# content words in these languages are real signal and stay.
ROMANCE_STOP = set("""
que los las del por con para una uno unos unas como pero mas este esta estos
estas todo toda todos todas muy sus sin sobre desde hasta cuando donde porque
entre segun tambien solo ahora aqui ese esa eso les nos ustedes ellos ellas
das dos das nao uma nas nos pelo pela isso essa esse
les des une aux dans pour avec sont est ete cette leur leurs mais ont
""".split())

STOPWORDS = BASE_STOP | CHAT_STOP | AGENT_STOP | ROMANCE_STOP

RE_HAS_VOWEL = re.compile(r"[aeiouy]")


def keep(tok: str) -> bool:
    if len(tok) < 3 or len(tok) > 28:
        return False
    if tok in STOPWORDS:
        return False
    if not RE_HAS_VOWEL.search(tok):
        return False
    if tok.isdigit():
        return False
    # Reject identifier-looking soup: many digits, or long lowercase hex.
    digits = sum(c.isdigit() for c in tok)
    if digits and (len(tok) <= 3 or digits / len(tok) > 0.4):
        return False
    return True


def tokens(text: str):
    """Lowercased content tokens from cleaned prose."""
    if not text:
        return []
    out = []
    for m in RE_TOKEN.finditer(text):
        t = m.group(0).lower().strip("-.'")
        if keep(t):
            out.append(t)
    return out


def raw_tokens(text: str):
    """Tokens before stopword removal -- needed for phrase detection."""
    if not text:
        return []
    return [m.group(0).lower().strip("-.'") for m in RE_TOKEN.finditer(text)]


def bigrams(toks):
    return zip(toks, toks[1:])


def phrase_candidates(toks):
    """Bigrams whose parts are both content words."""
    out = []
    for a, b in bigrams(toks):
        if keep(a) and keep(b):
            out.append(f"{a} {b}")
    return out


# --- near-duplicate prompt clustering ---------------------------------------

def shingle_key(text: str, n: int = 6) -> str:
    """A stable signature for near-duplicate detection.

    Loop automation fires thousands of prompts that differ only in a task id
    or path. Keying on the first n content tokens collapses those into one
    cluster while leaving genuinely different prompts apart.
    """
    toks = [t for t in raw_tokens(text) if t.isalpha()]
    return " ".join(toks[:n])


def cluster_prompts(texts):
    """Return (representative_text, count) per near-duplicate cluster."""
    groups = {}
    for t in texts:
        k = shingle_key(t)
        if not k:
            continue
        g = groups.setdefault(k, [0, t])
        g[0] += 1
        if len(t) > len(g[1]):
            g[1] = t
    return [(rep, n) for n, rep in groups.values()]
