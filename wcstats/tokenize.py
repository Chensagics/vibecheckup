"""Tokenization, stopwords, phrase detection, and near-duplicate clustering.

Unicode-aware by construction: an ASCII-only tokenizer does not merely lose
non-English prose, it mangles it. 'función' comes back as 'funci' and a Hebrew
or Arabic corpus comes back empty, which used to abort the whole run.

The one script family this still cannot do properly is the one written without
spaces. Chinese and Japanese tokenize as whole runs rather than words -- we
ship no segmentation dictionary -- so a repeated phrase clusters as one term
and a longer run falls past the 28-character cap in keep() and drops out.
Such a corpus can legitimately yield no tokens at all, which analyze.py warns
about rather than treating as fatal.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

from .clean import HOSTNAME_TLDS, active_redactor

# Latin: ASCII plus the accented blocks (Latin-1 Supplement through Latin
# Extended-B, and Latin Extended Additional for Vietnamese).
_LATIN = "A-Za-z\u00c0-\u024f\u1e00-\u1eff"
# Combining marks are NOT \w, so they have to be named explicitly or the letter
# they sit on ends the token: NFD 'función' would tokenize as 'funcio' + 'n',
# and a vowelled Hebrew or Arabic word would shatter at every point.
_MARK = ("\u0300-\u036f"          # Latin/Greek/Cyrillic diacritics (NFD)
         "\u0591-\u05c7"          # Hebrew points and cantillation
         "\u0610-\u061a\u064b-\u065f\u0670")  # Arabic harakat
# A letter in any script that is not Latin: Hebrew, Arabic, Cyrillic, Greek,
# Devanagari, CJK...
_OTHER = f"[^\\W\\d_{_LATIN}]"

RE_TOKEN = re.compile(
    # A Latin word, keeping the tails this corpus depends on: c++, .py,
    # don't, e-mail. Digits and underscores still cannot open a token, so
    # `12` and `_tmp` behave exactly as they always did.
    f"[{_LATIN}][{_LATIN}{_MARK}0-9_+#'.\\-]*"
    # ...or a run in another script. Deliberately a separate alternative:
    # matching one class across both would glue the stray 'n' of an unescaped
    # \n onto the Arabic word after it.
    f"|{_OTHER}(?:{_OTHER}|[{_MARK}'\\-])*"
)

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
más está están también sólo después aquí cómo dónde cuándo qué según
não você vocês até então já só são
où très même déjà être été
""".split())

# Once the tokenizer stopped being ASCII-only, the same thing that happened to
# Spanish happened to every other script in the corpus: the most frequent
# "words" turned out to be prepositions and pronouns, and two Arabic
# prepositions landed on the share card. Same rule as ROMANCE_STOP -- function
# words only, deliberately short and high-confidence. Content words in these
# languages are exactly the signal we just recovered and they stay.
SCRIPT_STOP = set("""
على إلى هذا هذه ذلك تلك التي الذي الذين هؤلاء هو هي هم أنا انا نحن أنت انت
كان كانت يكون تكون ليس ليست وليس ولا لكن ولكن أو او أن ان إن إذا اذا
عندما بعد قبل خلال حتى منذ بينما لأن لان لقد كل بعض هل نعم فقط أيضا ايضا
الآن الان جدا عند عليه عليها بها لها منه منها فيه فيها كذلك حول بين دون
غير سوف يجب يمكن يمكنك مثل أكثر اكثر أقل اقل واحد واحدة مما نحو الـ إنه انه
دائما مرة شيء أخرى تزال يزال بشكل
זאת אני אתה הוא היא אנחנו אתם הם הן אבל איך למה מתי איפה כאן
היה היתה הייתה להיות יהיה עוד כבר כמו אחרי לפני בין תודה בבקשה
צריך אפשר יותר פחות מאוד עכשיו הזה הזאת שלי שלך שלנו כדי אשר ולא וגם
что это как или для так все всё был была были быть если когда где кто
тот эта этот эти они она мне тебе нам его её их там тут уже еще ещё
только очень надо нужно можно чтобы потому также тоже нет меня тебя себя
""".split())

STOPWORDS = BASE_STOP | CHAT_STOP | AGENT_STOP | ROMANCE_STOP | SCRIPT_STOP

RE_HAS_VOWEL = re.compile(r"[aeiouy]")

# Three or more dotted segments: a reverse-DNS bundle id (com.chencorp.finn),
# a fully-qualified host (mejanreteam.chensagi.com), a dotted module path.
# None of those are vocabulary and all of them can name a person or a client.
RE_DOTTED_CHAIN = re.compile(r"^\w+\.\w+\.\w+")


# --- opaque high-entropy strings ---------------------------------------------
#
# clean_prose() removes the obvious blobs by shape, and between its rules there
# was a gap exactly the width of a modern API key: RE_B64 wants 40+ characters
# and rejects the '-' and '_' of base64url, RE_HEX wants 12+ hex digits and
# nothing else, and keep() only caps a token at 28. Anything base64url-ish
# between about 20 and 28 characters fell straight through --
# `sk-proj-Ab3xQ9pLm2` tokenized to `['key', 'sk-proj-ab3xq9plm2']` -- and 202
# such strings sat in the shipped vocab.json (Antigravity's protobuf-recovered
# step ids, e.g. `eyp7apy0npczxn8p1l-xqqe`).

# Where a token is split into alphanumeric runs before it is judged. A name
# like `missing_strings_0.json` is three ordinary words and a counter; an
# opaque id is one long run.
RE_ALNUM_RUN = re.compile(r"[0-9a-z]+")
RE_DIGIT_RUN = re.compile(r"\d+")
VOWELS = frozenset("aeiouy")

# One run has to be at least this long before it can be called opaque.
OPAQUE_RUN_LEN = 10
# ...and carry digits at this many separate positions. THE discriminating
# feature, and the reason a cheaper rule could not be used: a version suffix
# (`v2`, `chunk1`, `wp213`) and a numeronym (`i18n`, `a11y`, `l10n`, `k8s`)
# each have exactly ONE digit run, while a random id has them scattered.
# Requiring three keeps `i18nmanager` (53 occurrences in the real corpus),
# `initreacti18next`, `confirma11ylabel` and `compute20daylow` as vocabulary.
OPAQUE_DIGIT_RUNS = 3
# A long run with almost no vowels is opaque even without digits.
OPAQUE_NOVOWEL_LEN = 14
OPAQUE_VOWEL_RATIO = 0.20


def looks_opaque(tok: str) -> bool:
    """True when a token is a random-looking blob rather than a word.

    Judged per alphanumeric run, so that separators keep their meaning: the
    run is the unit a human names, and a name is either short or pronounceable.
    """
    if len(tok) < OPAQUE_RUN_LEN:
        return False
    for run in RE_ALNUM_RUN.findall(tok):
        if len(run) < OPAQUE_RUN_LEN:
            continue
        letters = [c for c in run if c.isalpha()]
        if not letters:
            continue                      # a pure number: RE_NUM's business
        if len(RE_DIGIT_RUN.findall(run)) >= OPAQUE_DIGIT_RUNS:
            return True
        if (len(run) >= OPAQUE_NOVOWEL_LEN
                and sum(1 for c in letters if c in VOWELS) / len(letters)
                < OPAQUE_VOWEL_RATIO):
            return True
    return False


# Credential shapes that are worth naming outright: a vendor prefix is a much
# stronger signal than any entropy measure, and it catches the short ones the
# entropy rule cannot see (`AKIA` + 16 characters carries a single digit run).
# Deliberately excludes `npm_` and `hf_`, which collide with `npm_package_*`
# environment variables and `hf_hub`.
RE_CREDENTIAL = re.compile(
    r"^(?:"
    r"(?:sk|pk|rk)[-_][a-z0-9_\-]{9,}"        # OpenAI / Anthropic / Stripe
    r"|gh[pousr]_[a-z0-9]{8,}"                # GitHub token
    r"|github_pat_[a-z0-9_]{8,}"
    r"|glpat-[a-z0-9_\-]{8,}"                 # GitLab
    r"|xox[abprse]-[a-z0-9_\-]{6,}"           # Slack
    r"|(?:akia|asia)[a-z0-9]{12,}"            # AWS access key id
    r"|aiza[a-z0-9_\-]{12,}"                  # Google API key
    r"|sq0(?:atp|csp)-[a-z0-9_\-]{8,}"        # Square
    r"|(?:shpat|shpss|shpca|shppa)_[a-z0-9]{8,}"   # Shopify
    r"|dop_v1_[a-z0-9]{8,}"                   # DigitalOcean
    r"|pypi-[a-z0-9_\-]{8,}"                  # PyPI
    r")$"
)


def keep(tok: str) -> bool:
    if len(tok) < 3 or len(tok) > 28:
        return False
    if tok in STOPWORDS:
        return False
    # Addresses and hostnames are identity, not language. The shipped clouds
    # carried `mejanreteam.chensagi.com` (a personal host), `izs.me` (a THIRD
    # PARTY's email domain, promoted as a distinctive term), `grok.com` and
    # `myinstants.com`. The TLD list is curated so `app.py` and `server.ts`
    # keep working -- see HOSTNAME_TLDS.
    #
    # RE_TOKEN cannot currently emit an '@' (an address splits into local part
    # and domain, and the domain half is what the TLD rule catches); the check
    # is here so that widening the token class can never quietly ship one.
    if "@" in tok:
        return False
    if "." in tok:
        head, _, last = tok.rpartition(".")
        if head and last.lower() in HOSTNAME_TLDS:
            return False
        if RE_DOTTED_CHAIN.match(tok):
            return False
    # The vowel test rejects hashes and identifier soup, and it can only judge
    # Latin script: 'bdika' in Hebrew letters and 'blad' spelled with Polish
    # diacritics carry no aeiouy and are perfectly good words. One non-ASCII
    # character means a script this heuristic cannot rule on, so it steps aside.
    if tok.isascii() and not RE_HAS_VOWEL.search(tok):
        return False
    if tok.isdigit():
        return False
    # Reject identifier-looking soup: many digits, or long lowercase hex.
    digits = sum(c.isdigit() for c in tok)
    if digits and (len(tok) <= 3 or digits / len(tok) > 0.4):
        return False
    # A secret is not vocabulary, and it is the one leak whose cost is
    # unbounded. Named shapes first (cheap and exact), then the entropy test
    # for the ids no vendor prefix announces.
    if RE_CREDENTIAL.match(tok) or looks_opaque(tok):
        return False
    # Second boundary for self-redaction. clean_prose() already masked these
    # out of prose, so in the normal path this never fires -- but keep() is
    # what decides whether a string becomes vocabulary, and vocab.json is built
    # straight out of the counters keep() fills. Anything that reaches the
    # tokenizer by another route (a caller that skipped clean_prose, a phrase
    # candidate rebuilt from raw tokens) stops here rather than being published.
    # Cost when no redactor is installed: one attribute lookup.
    if active_redactor().hits(tok):
        return False
    return True


def _fold(text: str) -> str:
    """NFC, so the same word typed two ways counts once.

    macOS hands back decomposed text in places, and IMEs disagree: 'función'
    can arrive as 7 characters or as 8 with a floating accent. Composing first
    means one key in the counter instead of two.
    """
    return unicodedata.normalize("NFC", text)


def tokens(text: str):
    """Lowercased content tokens from cleaned prose."""
    if not text:
        return []
    out = []
    for m in RE_TOKEN.finditer(_fold(text)):
        t = m.group(0).lower().strip("-.'")
        if keep(t):
            out.append(t)
    return out


def raw_tokens(text: str):
    """Tokens before stopword removal -- needed for phrase detection."""
    if not text:
        return []
    return [m.group(0).lower().strip("-.'")
            for m in RE_TOKEN.finditer(_fold(text))]


# What may sit between the two halves of a phrase: a space, and nothing else.
# This is the whole rule, and it is worth stating plainly because the old one
# was "whatever was between them, we don't look". phrase_candidates() used to
# run over a flat token list, which had already thrown away every separator, so
# a bigram formed across ANY gap: a newline, a comma, a bullet -- and, the one
# that reached the share card, the `": "` between a JSON key and its value.
# `"type": "string"` became the phrase `type string` 435 times and was
# announced to the owner as the phrase he typed often enough that it became a
# tell. A phrase spanning a colon-quote boundary is not a phrase anyone said.
#
# Space-only is stricter than it needs to be for that one bug and that is the
# point: it is a rule a reader can check ("two words with a space between
# them"), where a blocklist of forbidden punctuation is a rule that is wrong
# again the next time a serialization format shows up. It costs nothing --
# unigrams, word counts and every other statistic are untouched, since this
# only decides which PAIRS are offered to the PMI scorer.
# ...spelled out rather than typed: a literal no-break space inside a
# character class is invisible in a diff and indistinguishable from the plain
# one beside it. It is in the set because a no-break space IS a space -- word
# processors and some IMEs emit one between two ordinary words.
RE_PHRASE_GAP = re.compile("[ \\t\\u00a0]+")


def phrase_candidates(text: str):
    """Bigrams of content words that were adjacent, separated by a space.

    Takes TEXT rather than tokens: the separator is the signal, and a token
    list no longer has it.
    """
    if not text:
        return []
    # Folded once, and the offsets are read off the SAME string: NFC can change
    # the length of the text, so slicing the original with folded spans would
    # look at the wrong characters.
    folded = _fold(text)
    out = []
    prev, prev_end = None, 0
    for m in RE_TOKEN.finditer(folded):
        tok = m.group(0).lower().strip("-.'")
        if (prev and tok
                and RE_PHRASE_GAP.fullmatch(folded[prev_end:m.start()])
                and keep(prev) and keep(tok)):
            out.append(f"{prev} {tok}")
        prev, prev_end = tok, m.end()
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
