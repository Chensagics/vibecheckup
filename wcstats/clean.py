"""Content-based filtering: what counts as prose, and what is machine noise.

This is the highest-leverage module in the project. A typical Claude Code
session holds a handful of real prompts against hundreds of tool results and
hook attachments -- unfiltered, the corpus is overwhelmingly machine text and
every cloud reads "skill, important, file, the".

Adapters classify records by type; every judgement about *content* lives here.

The bottom half of the file is *self-redaction*. Every other rule in this
module recognises a leak by its SHAPE -- an address has an @, a path has
slashes, an `ls -l` row starts with a mode field. A username does not: written
as ``chensagi/finn`` in a sentence it is two ordinary-looking words, and the
only thing that distinguishes it from vocabulary is knowing whose machine this
is. So the last defence is a list of known literals -- derived from the account
running the tool, from git, and from the worktree branch names ingest sees --
masked wherever they appear.
"""
from __future__ import annotations

import os
import re

# Kinds that may contain human- or model-authored prose.
PROSE_KINDS = {"prompt", "reply", "thinking"}

# --- blocks removed wholesale from prose -------------------------------------

BLOCK_PATTERNS = [
    re.compile(r"<system-reminder>.*?</system-reminder>", re.S | re.I),
    re.compile(r"<local-command-stdout>.*?</local-command-stdout>", re.S | re.I),
    re.compile(r"<command-message>.*?</command-message>", re.S | re.I),
    re.compile(r"<command-name>.*?</command-name>", re.S | re.I),
    re.compile(r"<EPHEMERAL_MESSAGE>.*?</EPHEMERAL_MESSAGE>", re.S | re.I),
    re.compile(r"<extremely[_-]important>.*?</extremely[_-]important>", re.S | re.I),
    re.compile(r"<skills_instructions>.*?</skills_instructions>", re.S | re.I),
    re.compile(r"<citations>.*?</citations>", re.S | re.I),
    re.compile(r"<budget:.*?</budget:.*?>", re.S | re.I),
    re.compile(r"```.*?```", re.S),          # fenced code
    re.compile(r"~~~.*?~~~", re.S),
    re.compile(r"<function_calls>.*?</function_calls>", re.S | re.I),
    re.compile(r"<thinking>.*?</thinking>", re.S | re.I),
    re.compile(r"<user_info>.*?</user_info>", re.S | re.I),
    re.compile(r"<task-notification>.*?</task-notification>", re.S | re.I),
    re.compile(r"<in-app-browser-context.*?</in-app-browser-context>", re.S | re.I),
    re.compile(r"^\s*\[Image:[^\]]*\]", re.M | re.I),
    re.compile(r"<environment_details>.*?</environment_details>", re.S | re.I),
]

# Whole messages that are injected context rather than authored text.
INJECTED_PREFIXES = (
    "caveat: the messages below",
    "base directory for this skill:",
    "the following is the codex agent history",
    "another claude session sent a message",
    "[request interrupted by user]",
    "<task-notification>",
    "<user_info>",
    "<in-app-browser-context",
    "<env>",
    "sessionid ",
    "unsupported mime type",
    "this session is being continued from",
    "the following is an <ephemeral_message>",
    "# conversation history",
    "<system-reminder>",
    "you are codex,",
    "you are claude code,",
    "# security rules",
    "here are guidelines for using",
    "the user opened the file",
    "new task: read and execute the brief",
)

INJECTED_MARKERS = (
    "hookspecificoutput",
    "<command-message>",
    "<command-name>",
    '"step_index"',
    "base directory for this skill:",
    "this is an automated background-task event",
    "[system notification - not user input]",
    "sessionstart:startup hook",
    "<skills_instructions>",
    "codebase and user instructions are shown below",
    "vercel knowledge updates",
    "the following deferred tools are now available",
    "available agent types for the agent tool",
    "# mcp server instructions",
    "stop hook feedback",
    "outstanding user requests",
    "conversation summary",
    "the conversation is being continued",
    # Antigravity returns a file read as an assistant *reply*, under a fixed
    # banner. Its wording ("line number, colon, and leading space") was the
    # model's own distinctive vocabulary as far as the clouds could tell, and
    # it took four of the top ten things Claude supposedly says.
    "has been modified to include a line number before every line",
)

# --- line-level noise --------------------------------------------------------

RE_URL = re.compile(r"https?://\S+")
RE_PATH = re.compile(r"(?:/[\w.\-@+]+){2,}/?")

# Windows and UNC paths. RE_PATH above is forward-slash only, so a home
# directory written the Windows way used to survive cleaning whole: the README
# supports Windows through WSL and Git Bash, where `C:\Users\<name>` is exactly
# what tool output prints, and `C:\Users\jane.doe\Projects\acme-client` came
# out of the tokenizer as jane.doe / projects / acme-client.
_WIN_SEG = r"[^\\/:*?\"<>|\s]+"
RE_WINPATH = re.compile(
    # C:\Users\jane\x  and  C:/Users/jane/x  -- the lookbehind keeps "note:/x"
    # and a leftover "s://" from being read as a drive letter.
    r"(?<![\w:])[A-Za-z]:[\\/](?:" + _WIN_SEG + r"[\\/]?)*"
    # \\fileserver\share\Clients\budget.xlsx
    r"|\\\\" + _WIN_SEG + r"(?:[\\/]" + _WIN_SEG + r")+[\\/]?"
)

# An address is identity -- the author's or, worse, a third party's.
RE_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")

# Last labels that mark a token as a hostname rather than a word. Deliberately
# curated: ccTLDs that are also everyday file extensions (.py .ts .rs .sh .md
# .pl .so .cs .ml .in .el .pm .cc .cx) are left OUT, so `app.py` and
# `server.ts` stay ordinary vocabulary. Shared by tokenize.keep() and by
# facets.error_signature() so the two can never drift apart.
#
# The second block is the one a LAN actually uses, and it was the hole here:
# every name below is reserved by an RFC precisely so that it can never be a
# public TLD, which makes it a hostname suffix with no ambiguity at all.
# `.local` is the one that matters most -- `hostname` on every Mac answers
# `<account>-MacBook-Pro.local`, and that string rides along in ssh banners,
# mDNS failures and a dev server's "Network:" line. Without it,
# `janedoe-macbook-pro.local` was a vocabulary word.
#
#   local                RFC 6762 (mDNS)
#   localhost            RFC 6761
#   arpa                 RFC 3172 -- and `home.arpa` from RFC 8375
#   internal lan corp home localdomain intranet private
#                        RFC 6762 appendix G / RFC 8375: the suffixes home and
#                        corporate networks are told to use, and do.
#
# `test`, `example` and `invalid` (also RFC 6761) are deliberately NOT here:
# `Button.test` is a real filename stem and `foo.example` a real fixture name,
# and neither is worth a hostname's worth of privacy. The rule matches a DOTTED
# SUFFIX, so the bare words `local`, `home`, `private` and `lan` are untouched.
HOSTNAME_TLDS = frozenset("""
com org net int edu gov mil info biz name pro xyz app dev io co ai me tv
cloud tech site online store shop blog page live life work team group email
network systems solutions agency studio design media news wiki space fun
uk de fr jp cn ru br it es nl se no dk fi ca au ch at be ie nz kr mx ar
cl za pt gr cz hu ro tr ua sg hk tw vn th ph my il eu us gg
local localhost localdomain internal intranet lan corp home private arpa
""".split())

RE_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
RE_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{12,}\b", re.I)
RE_B64 = re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b")
RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RE_TAG = re.compile(r"</?[a-zA-Z][\w:-]{0,40}\s*/?>")
RE_NUM = re.compile(r"\b\d[\d,._]*\b")
RE_WS = re.compile(r"[ \t]+")

# Lines that are clearly machine output rather than prose.
RE_DIFF = re.compile(r"^\s*(?:[+-]{1,3}\s|@@|diff --git|index [0-9a-f]{7})")
RE_TREE = re.compile(r"^\s*[│├└─|`+\\-]{2,}")
RE_LOGLINE = re.compile(r"^\s*(?:\[\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T\d{2}:|at [A-Za-z_$]+ \()")

# A POSIX long listing. The owner and group columns are a username, and the
# row is otherwise ordinary-looking words, so an `ls -l` block pasted into a
# session used to sail through as prose: "chensagi staff" reached the shipped
# landing tab as a top phrase, 126 occurrences, and "chensagi" ranked as a
# distinctive term. RE_PATH cannot help -- the username arrives with no slash.
# Covers the mode field plus macOS's `@`/`+` ACL flag and Linux's `.` SELinux
# flag; `ls -la` hardlink counts change nothing, the anchor is the mode.
RE_LS_LONG = re.compile(r"^[-dlbcpsD][rwxsStTL-]{9}[@+.]?(?:\s|$)")
# ...and the header `ls -l` prints above it (`total 2504`, `total 4.0K`).
RE_LS_TOTAL = re.compile(r"^total\s+[\d.,]+[kKmMgGtTpP]?[bB]?$")


# --- source code and markup -------------------------------------------------

# Unambiguous markup/document openers: a message starting with one of these
# is a file dump at any length.
STRICT_PREFIXES = (
    "<!doctype", "<html", "<?xml", "<svg", "<script", "/**", "#!/",
)
# Word-like openers are NOT decisive on their own -- "import the CSV into the
# database" is a prompt, not code -- so they only contribute via line density.
CODE_PREFIXES = STRICT_PREFIXES

RE_CODE_LINE = re.compile(
    r"(?:^\s*(?:const|let|var|function|def|class|import|export|return|if|for|"
    r"while|else|elif|try|except|catch|switch|case)\b)"
    r"|(?:[;{}]\s*$)"
    r"|(?:=>)"
    r"|(?:^\s*</?[a-zA-Z][\w:-]*[\s/>])"
    r"|(?:^\s*[\w-]+\s*:\s*[^\s].*[;,]\s*$)"
)


def looks_like_code(text: str) -> bool:
    """True when a message is a source file or markup dump rather than prose.

    Whole-file pastes arrive unfenced (Antigravity bundles file bodies into the
    same step as the prompt), so fence-stripping alone does not catch them.
    """
    t = text.strip()
    if t.startswith(STRICT_PREFIXES):
        return True
    if len(t) < 40:
        return False
    lines = [ln for ln in t.splitlines() if ln.strip()][:200]
    if len(lines) < 4:
        return False
    codey = sum(1 for ln in lines if RE_CODE_LINE.search(ln))
    return codey / len(lines) > 0.35


# --- structured data: JSON, YAML, TOML, schemas -------------------------------
#
# The hole this closes was the loudest bug the product had. A JSON object with
# quoted keys is not fenced, is not markup, and is not dense enough in
# RE_CODE_LINE terms to trip looks_like_code(), so a settings schema pasted into
# a prompt sailed through as prose -- and because it arrives INSIDE a user turn
# (a skill definition or a tool schema injected into the message), no role gate
# could catch it either. The shipped card told the owner his signature phrase
# was "type string": JSON Schema, 435 times, from lines like
#
#     "description": "Path to a script that outputs authentication values",
#     "type": "string"
#
# Two things make this filter different from every other rule in the file.
#
# FIRST, it is REGIONAL rather than per-line. The corpus has these embedded
# mid-message -- a real typed question, then a pasted schema -- so throwing the
# whole message away would throw away the question. Each line is judged, the
# structured ones go, and the prose around them stays.
#
# SECOND, it grades its evidence, in three tiers rather than two, because the
# shapes differ enormously in how much they prove.
#
#   STRONG    a QUOTED IDENTIFIER KEY followed by a colon, or a `[section]`
#             header. Decisive on its own: nothing a person writes looks like
#             `"type":`, and the whole corpus holds two lone `[bracket]` lines.
#   GLUE      brackets, a lone quoted enum member, a `---` fence, and a
#             `key: value` whose VALUE is a single unbroken token
#             (`line-length = 88`, `name: update-config`, `target: "py39"`).
#             Meaningless outside a block, so removed whenever the run they sit
#             in has something decisive in it.
#   AMBIGUOUS `key:` with nothing after it, or with a value of several words.
#             This is YAML -- and it is also how this owner writes. Of the 474
#             bare `Key: value` lines in his prompts, the runs of three or more
#             read "Deliverable: ... / TDD: ... / Run: ..." and
#             "Fix: ... / METHOD: ... / TESTS: ..." -- 595 tokens of the most
#             characteristic writing he does, and `Examples:` and `METHOD:` are
#             section headings, not YAML mappings. So an ambiguous line is only
#             removed where it is demonstrably INSIDE a block: bracketed by
#             unambiguous structure on both sides. At the edge of a region,
#             where prose meets config, it is kept -- "Note: read this first"
#             sitting directly above a pasted schema is prose, and stays.
#
# The strong key must be IDENTIFIER-shaped -- bounded, no spaces -- and that
# bound is load-bearing rather than cosmetic. The owner's Arabic vocabulary
# lives inside translation files whose keys are whole English sentences:
#
#     "When the chart turns red": "عندما يتحول الرسم البياني إلى اللون الأحمر",
#
# A rule that took any quoted key would have doubled its yield (18.5% of prompt
# tokens instead of 9.1%) by eating the one thing on the card that was most
# unmistakably his.

# `"type":`  ·  `{"winner": "A|B|tie"}`  ·  `"$schema":`  ·  `, "path": "x"`
RE_JSON_KEY = re.compile(r"""^[\s\[{,]*["'][A-Za-z_$@][\w.\-$]{0,39}["']\s*:""")

# A data ROW, judged by the pair rather than by the key. The rule above is
# deliberately narrow about what a key may contain, and that bound is what let
# a pasted translation table through as authored prose: its keys are file
# paths (`"src/data/academy/compiled.ar.json.10.script.4.chat"`) or whole
# English sentences, so neither the charset nor the 40-character limit fits,
# every row classified as PROSE, and one paste contributed 5,000 tokens of
# Arabic UI copy the owner never typed. Ten such messages were a third of the
# corpus.
#
# What makes a row data is not the key: it is a quoted key sitting against a
# structured value -- another quoted string, a bracket, a number, a keyword.
# Prose does not do that. A labelled sentence ("Note: read this first") has no
# quotes around the label, and a quoted phrase mid-sentence has no colon after
# the closing quote.
RE_JSON_ROW = re.compile(
    r"""^[\s\[{,]*(?P<q>["'])(?:(?!(?P=q))[^\n]){1,200}(?P=q)\s*:\s*"""
    r"""(?:["'\[{]|true\b|false\b|null\b|-?\d)""", re.I)

# Structure with no claim of its own: brackets, separators, a lone scalar or
# quoted enum member, an INI/TOML section header.
RE_STRUCT_PUNCT = re.compile(r"^[\[\]{}(),;:]+$")
RE_STRUCT_SCALAR = re.compile(
    r"""^(?:["'][^"']{0,80}["']|true|false|null|-?\d[\d.eE+\-]*)\s*,?$""", re.I)
# `[tool.black]`, `[mcp_servers.pixellab]` -- and, in the whole 224k-event
# corpus, exactly `[Intro]` and `[Tag]`, two lyric-sheet markers worth 3 tokens.
RE_INI_SECTION = re.compile(r"^\[[\w.\-]{1,40}\]$")
# The one thing that makes a run of YAML unambiguous: a fence around it.
RE_FRONTMATTER = re.compile(r"^(?:-{3,}|\.{3})$")
# `key: value`, `key:`, `- key: value`, `key = value`.
RE_KEYVALUE = re.compile(r"^-?\s*[A-Za-z_][\w.\-]{0,40}\s*[:=]\s*(.*)$")
# ...and the half of that which is safe: a value that is one unbroken token or
# an opening bracket. A person writing a labelled sentence ("Note: read this
# first", "Deliverable: add ONE action") writes several words after the colon.
RE_SCALAR_VALUE = re.compile(r"^(?:\S+|[\[{])\s*,?$")

STRUCT_BLANK = -1
STRUCT_PROSE = 0
STRUCT_AMBIGUOUS = 1
STRUCT_GLUE = 2
STRUCT_STRONG = 3

# A frontmatter block is a document header, not a chapter: past this many lines
# a pair of `---` rules is two horizontal rules with prose between them.
FRONTMATTER_MAX_LINES = 40


def struct_class(line: str) -> int:
    """How much does this line prove? See the tiers above."""
    s = (line or "").strip()
    if not s:
        return STRUCT_BLANK
    if (RE_JSON_KEY.match(s) or RE_JSON_ROW.match(s)
            or RE_INI_SECTION.match(s)):
        return STRUCT_STRONG
    if (RE_STRUCT_PUNCT.match(s) or RE_STRUCT_SCALAR.match(s)
            or RE_FRONTMATTER.match(s)):
        return STRUCT_GLUE
    m = RE_KEYVALUE.match(s)
    if m:
        val = m.group(1).strip()
        return STRUCT_GLUE if val and RE_SCALAR_VALUE.match(val) \
            else STRUCT_AMBIGUOUS
    return STRUCT_PROSE


def _frontmatter_spans(lines):
    """Line indices covered by `---` fenced YAML headers.

    The fence is what licenses the YAML rule: `name: x` inside one is a field,
    while the same line loose in a message is a sentence. An opening fence has
    to actually open something -- the top of the message, or a blank line
    before it -- so a `---` horizontal rule mid-paragraph cannot start one.
    """
    dead = set()
    for i, ln in enumerate(lines):
        if not RE_FRONTMATTER.match(ln.strip()):
            continue
        if i in dead:
            continue
        if i and lines[i - 1].strip():
            continue
        for j in range(i + 1, min(len(lines), i + FRONTMATTER_MAX_LINES + 1)):
            body = lines[j].strip()
            if RE_FRONTMATTER.match(body):
                inner = [lines[k] for k in range(i + 1, j)]
                if inner and all(struct_class(x) != STRUCT_PROSE for x in inner) \
                        and any(RE_KEYVALUE.match(x.strip()) for x in inner):
                    dead.update(range(i, j + 1))
                break
            if struct_class(body) == STRUCT_PROSE:
                break
    return dead


def _absorb(run, classes):
    """Which lines of one structured run actually go.

    The run is a maximal stretch of non-prose lines that holds at least one
    STRONG line. Everything decisive or meaningless goes with it. An AMBIGUOUS
    line goes only when unambiguous structure stands on BOTH sides of it inside
    the run -- that is what "inside the block" means, and it is what keeps
    "Note: read this first" alive when it is the line directly above a pasted
    schema. Blank lines ride along; they carry nothing either way.
    """
    hard = [i for i in run if classes[i] in (STRUCT_STRONG, STRUCT_GLUE)]
    if not hard:
        return ()
    lo, hi = hard[0], hard[-1]
    return [i for i in run
            if classes[i] != STRUCT_AMBIGUOUS or lo < i < hi]


def drop_structured_lines(lines):
    """Drop the config/schema regions of a message, keep the prose around them.

    A run is an unbroken stretch of non-prose lines; it is structured when
    something decisive is in it. Blank lines neither start a run nor break one,
    so a schema with an empty line in the middle is still one region.
    """
    classes = [struct_class(ln) for ln in lines]
    dead = _frontmatter_spans(lines)
    run, strong = [], False
    for i, c in enumerate(classes + [STRUCT_PROSE]):
        if c == STRUCT_PROSE:
            if strong:
                dead.update(_absorb(run, classes))
            run, strong = [], False
            continue
        run.append(i)
        strong = strong or c == STRUCT_STRONG
    return [ln for i, ln in enumerate(lines) if i not in dead]


def _drop_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if RE_LS_LONG.match(s) or RE_LS_TOTAL.match(s):
        return True
    if RE_DIFF.match(s) or RE_TREE.match(s) or RE_LOGLINE.match(s):
        return True
    if RE_CODE_LINE.search(s):
        return True
    # Long unbroken tokens: base64, minified payloads, hashes.
    if len(s) > 200 and " " not in s[:200]:
        return True
    # Mostly punctuation / symbols.
    alpha = sum(1 for c in s if c.isalpha())
    if len(s) > 20 and alpha / len(s) < 0.35:
        return True
    return False


def is_injected(text: str) -> bool:
    """True when the message is context injected by the harness, not authored."""
    if not text:
        return False
    head = text.lstrip()[:400].lower()
    if head.startswith(INJECTED_PREFIXES):
        return True
    low = text[:4000].lower()
    return any(m in low for m in INJECTED_MARKERS)


def clean_prose(text: str) -> str:
    """Strip machine content from a prose message, leaving authored language."""
    if not text:
        return ""
    t = RE_ANSI.sub(" ", text)
    for pat in BLOCK_PATTERNS:
        t = pat.sub(" ", t)
    # Regions first, then lines: drop_structured_lines() needs to see a schema
    # block whole, and _drop_line() would have already punched holes in it.
    lines = [ln for ln in drop_structured_lines(t.splitlines())
             if not _drop_line(ln)]
    t = "\n".join(lines)
    t = RE_URL.sub(" ", t)
    t = RE_EMAIL.sub(" ", t)
    t = RE_UUID.sub(" ", t)
    t = RE_B64.sub(" ", t)
    t = RE_HEX.sub(" ", t)
    # Before RE_PATH: on `C:/Users/jane/x` the POSIX rule would eat the tail
    # and leave a bare `C:` behind, which reads as a token.
    t = RE_WINPATH.sub(" ", t)
    t = RE_PATH.sub(" ", t)
    t = RE_TAG.sub(" ", t)
    t = RE_NUM.sub(" ", t)
    # Last, and deliberately so: the shape-based rules above have already
    # removed everything they can recognise, and what is left is ordinary
    # language plus whatever the installed redactor knows by name.
    t = active_redactor().scrub(t)
    return RE_WS.sub(" ", t).strip()


def prose_text(ev: dict) -> str:
    """Authored prose for an event, or "" when it contributes none."""
    if ev.get("kind") not in PROSE_KINDS:
        return ""
    if ev.get("role") == "system":
        return ""
    text = ev.get("text") or ""
    if is_injected(text) or looks_like_code(text):
        return ""
    return clean_prose(text)


# =============================================================================
# Self-redaction: masking literals that only the local machine can identify.
# =============================================================================

# The placeholder a facet KEY collapses to. Prose gets a space instead -- a
# word cloud has nowhere to put a marker, and a run of "redacted" tokens would
# itself become a top term.
LABEL_PLACEHOLDER = "redacted"

# Auto-derived identities shorter than this are refused. This is the cheap half
# of the common-word guard and it is doing most of the work: the logins that
# collide with English are overwhelmingly short (mark, will, sam, art, max,
# rob, bill, jack, dawn, ray, joy, ivy, rose, sky). Anything the guard refuses
# is still redactable by hand with --redact.
MIN_IDENTITY_LEN = 5
# ...but the length gate is a proxy for "we are GUESSING", and for two sources
# we are not guessing at all: `[user] name` and the local part of `[user]
# email` are the person, typed in by the person. A two-word display name is
# where this bit: "Jane Doe" split into `jane` and `doe`, both four letters,
# both thrown back by MIN_IDENTITY_LEN -- and the terminal still said the name
# was masked. On this machine the same gate refused `chen` and `sagi`, and
# vocab.json shipped chen x21 and sagi x3 out of "Hey Chen" and "Chen Sagi's
# projects".
#
# So these two sources are gated by COMMON_WORDS alone, which is the guard that
# was always doing the real work: a person called Art, Sky or Rose still keeps
# the word, because the word is on the list. The floor below exists only to
# stop a one-letter middle initial ("Jane Q Doe") becoming a literal that
# matches every token starting with q.
MIN_PERSONAL_LEN = 2
# Literals shorter than this match at BOTH token edges rather than as a
# run-on prefix -- see Redactor._pattern.
EDGE_ANCHOR_LEN = MIN_IDENTITY_LEN
# A branch name with no separator in it is just a word, so it has to clear a
# higher bar than a compound like `combat-engine` before it is masked bare.
MIN_BARE_BRANCH_LEN = 6

# Words a login or a branch name may legitimately BE. Redacting one of these
# would delete real vocabulary from every cloud in exchange for no privacy, so
# an auto-derived identity that matches is skipped and reported. Deliberately
# generous -- the cost of an entry is only that the user has to pass --redact
# for that one string, while the cost of a missing entry is silently eating a
# word out of the whole corpus. Three groups: frequent English, given names
# that are also nouns, and the vocabulary of branch names and software.
COMMON_WORDS = frozenset("""
about above after again against alone along already also although always among
another answer around because become before begin being below better between
beyond bring build built cannot change check clean clear close could course
create data does done down during each early enough even ever every example
first found from full give given going great group hand have hear help here
high hold home hour house however human idea important inside instead into
issue just keep kind know large last late later learn least leave less letter
level life light like line list little live local long look love made make
many matter maybe mean might mind more most move much must name near need
never next night nothing notice number often once only open order other over
own part people perhaps person place plan play point power press probably
problem public quite rather reach read ready real reason remember report rest
right room round said same school second seem sense several short should show
side simple since small some sound space speak start state still stop story
study such sure system take talk teach thing think three through time today
together took toward town tree true turn under until upon used using usual
value very view voice wait walk want watch water week well were what when
where which while white whole will wish with within without word work world
would write wrong year young your
mark marc will sam samuel art arthur max rob bill jack grace hope faith ray
dawn drew tim ben dan jay joy june may april august summer autumn river brook
sky rose lily daisy jasmine olive ruby pearl amber jade ivy holly heather iris
violet hunter chase cash king earl duke prince blaze storm rain star angel
frank rich don guy hardy noble price stone wood ford banks brown white green
gray grey black young long short small best fair love bell page reed hall
ward cook fisher miller baker taylor turner walker parker carter cooper hayes
eve kit pat sue dot tab hue rue bud van dale glen lane wade grant forest
colt ace jet field shore moss vale bloom dove robin drake fox wolf bear
hawk dean bay fern sage gene cane cliff heath ruth pearl noel wren lark
root runner guest node owner host stack trunk rock reid
main master trunk dev devel develop development staging stage prod production
release releases hotfix feature features bugfix fix fixes test tests testing
next canary beta alpha trunk integration sandbox experiment experimental wip
temp tmp draft backup legacy refactor cleanup docs doc documentation build
builds deploy deployment upgrade migration migrate revert patch base work
working current latest stable edge preview demo example sample default review
api web app apps core admin user users site sites blog shop store team teams
dashboard telemetry spec specs scripts script assets asset config configs
server client mobile desktop backend frontend database search login signup
auth payment payments billing profile settings account accounts notification
notifications analytics report reports export import upload download image
images video audio chart charts graph graphs table tables form forms button
modal sidebar header footer layout theme themes style styles design designs
editor viewer player engine parser router cache queue worker service services
module modules package packages library plugin plugins widget component
components landing pricing about contact support docs help faq legal privacy
terms blog news press careers jobs about
""".split())

# Only these characters can sit inside one "token" for redaction purposes.
# Deliberately the same tail set wcstats.tokenize.RE_TOKEN accepts, MINUS the
# slash: in `chensagi/finn` the handle is the leak and `finn` is a project name
# the dashboard already publishes as a label, so the slug splits and only the
# left half goes.
RE_TOKENISH = re.compile(r"[0-9A-Za-z_.+#'’\-]+")
# ...as a set, for walking outward from a match to the token edges. Derived
# from the pattern rather than spelled twice, so the two cannot drift.
_TOKENISH_SET = frozenset(
    c for c in list(map(chr, range(32, 127))) + ["’"]
    if RE_TOKENISH.fullmatch(c))

# The separators one identity may be spelled with. A display name arrives from
# git as "Jane Doe" and the same person's address as `jane.doe@`, so a literal
# that was written with a SPACE has to be able to match every other spelling
# too -- see Redactor._pattern for why only that direction is widened.
RE_SEP = r"[._\-\s]+"

# A GitHub/GitLab/Bitbucket-style remote, in either spelling:
#   https://github.com/OWNER/repo.git      git@github.com:OWNER/repo.git
RE_REMOTE_OWNER = re.compile(
    r"^(?:[a-z][\w+.\-]*://(?:[^/@\s]+@)?[^/\s]+/|[\w.\-]+@[\w.\-]+:)"
    r"([\w.\-]+)/", re.I)

# `12345678+handle@users.noreply.github.com` -- GitHub's privacy address. The
# handle is exactly the string that shows up in `handle/repo` slugs, so this
# one shape is worth special-casing.
RE_GH_NOREPLY = re.compile(r"^(\d+)\+(.+)$")


def is_common_word(s: str) -> bool:
    """Would redacting this string cost real vocabulary?"""
    return s.strip().lower() in COMMON_WORDS


def identity_verdict(lit, min_len=MIN_IDENTITY_LEN):
    """(accept, reason) for one auto-derived literal.

    Explicit --redact strings never come through here: if somebody says their
    handle is `sam`, that is a decision they are entitled to make about their
    own corpus. This guard only governs what we redact WITHOUT being asked,
    where a false positive silently deletes a word from every cloud.
    """
    s = (lit or "").strip().lower()
    if not s:
        return False, "empty"
    if not any(c.isalpha() for c in s):
        return False, "no letters"
    if len(s) < min_len:
        return False, f"shorter than {min_len} characters"
    if is_common_word(s):
        return False, "a common word"
    # `a-b` where either half is itself a common word is still fine (that is
    # what `combat-engine` is); only the whole string has to be distinctive.
    return True, ""


class Redactor:
    """A set of known literals, masked wherever they appear at a token edge.

    Matching rule, and the reason for it: a literal counts when it starts a
    token or follows a non-alphanumeric character inside one, and then the
    WHOLE token goes. That covers the three positions the leak actually takes
    -- bare (`chensagi`), in a slug (`chensagi/finn`), and inside a compound
    (`worktree-combat-engine`, `combat-engine-handoff.md`, `graphify-out`,
    `graphify's`) -- plus the run-on forms a plain word-boundary rule misses
    (`chensagics`, `graphifyignore`). It deliberately does NOT match in the
    middle of a word, so a hypothetical identity `river` cannot eat `driver`.
    """

    __slots__ = ("_lits", "_probe", "_anchor", "_span", "_cache")

    def __init__(self, literals=()):
        self._lits = set()
        self._probe = None
        self._anchor = None
        self._span = None
        # hits() is called once per surviving token -- millions of times over a
        # real corpus, against a vocabulary of only tens of thousands of
        # distinct strings. Memoizing turns the regex into a dict lookup.
        self._cache = {}
        self.add_all(literals)

    # -- construction --------------------------------------------------------

    def add(self, lit) -> bool:
        lit = (lit or "").strip().lower()
        if not lit or lit in self._lits:
            return False
        self._lits.add(lit)
        self._compile()
        return True

    def add_all(self, lits) -> int:
        n = 0
        for lit in lits or ():
            s = (lit or "").strip().lower()
            if s and s not in self._lits:
                self._lits.add(s)
                n += 1
        if n:
            self._compile()
        return n

    @staticmethod
    def _spans_whitespace(lit):
        """True when this literal can only match across a token boundary."""
        return bool(re.search(r"\s", lit or ""))

    @classmethod
    def _pattern(cls, lit):
        """One literal, spelled with any separator.

        `chen.sagi.cs`, `chen_sagi_cs` and `chen-sagi-cs` are one identity
        written three ways, and the corpus contained two of them. Same for a
        branch: `combat-engine` and `combat_engine` name the same branch.

        A literal that arrives WITH a space -- git's `[user] name = Jane Doe`
        is the one that matters -- also matches `jane.doe`, `jane-doe` and
        `jane_doe`, because those are the same identity typed by the same
        person. The widening is one-directional on purpose: `combat-engine`
        must NOT start eating the two ordinary words "combat engine" out of
        prose, so only a literal that was written with whitespace gains the
        whitespace spelling.
        """
        parts = re.split(RE_SEP, lit.strip()) if lit else []
        if len(parts) > 1 and all(parts):
            sep = RE_SEP if cls._spans_whitespace(lit) else r"[._\-]+"
            body = sep.join(re.escape(p) for p in parts)
        else:
            body = re.escape(lit)
        # A SHORT literal is anchored at BOTH edges. The run-on rule -- match
        # `chensagi` inside `chensagics` and take the whole token -- is right
        # for a distinctive handle and catastrophic for a four-letter forename:
        # `li` would eat list/line/link/library, `cs` would eat csv, `ana`
        # would eat analysis. Requiring the far edge too keeps exactly the
        # spellings a name actually takes in prose -- `chen`, `Chen's`,
        # `chen-sagi`, `Hey Chen.` -- and nothing else. Long literals keep the
        # run-on rule; they are distinctive enough to afford it.
        if len(lit or "") < EDGE_ANCHOR_LEN:
            body += r"(?![0-9A-Za-z])"
        return body

    def _compile(self):
        # Longest first so an alternation prefers `graphify-ab-control` over
        # `graphify`; with whole-token removal the outcome is the same either
        # way, but it keeps the match spans honest for anyone debugging.
        ordered = sorted(self._lits, key=lambda s: (-len(s), s))
        alt = "|".join(self._pattern(x) for x in ordered)
        self._probe = re.compile(alt, re.I)
        self._anchor = re.compile(
            r"(?:^|(?<=[^0-9A-Za-z]))(?:" + alt + r")", re.I)
        # scrub() replaces one TOKENISH run at a time and a run never contains
        # a space, so a literal that spans a token boundary could never fire
        # there: `Redactor(['jane doe']).scrub('signed by Jane Doe')` used to
        # return its input unchanged while the terminal reported `jane doe` as
        # masked. Those literals get a pass of their own, over spans.
        multi = [x for x in ordered if self._spans_whitespace(x)]
        self._span = re.compile(
            r"(?:^|(?<=[^0-9A-Za-z]))(?:"
            + "|".join(self._pattern(x) for x in multi) + r")", re.I
        ) if multi else None
        self._cache = {}

    @property
    def literals(self):
        return sorted(self._lits)

    def __len__(self):
        return len(self._lits)

    def __bool__(self):
        return bool(self._lits)

    # -- use -----------------------------------------------------------------

    def hits(self, token) -> bool:
        """True when this token carries one of the literals at a token edge."""
        if not self._anchor or not token:
            return False
        got = self._cache.get(token)
        if got is None:
            got = bool(self._anchor.search(token))
            if len(self._cache) > 200_000:
                self._cache.clear()
            self._cache[token] = got
        return got

    def _scrub_spans(self, text, placeholder):
        """The pass for literals that cross a token boundary.

        Same promise as the token pass -- the WHOLE token goes -- so each match
        is grown outward to the edges of the tokens it touches before it is
        replaced. `foo-jane doe.md` loses all of it, not just the middle.
        """
        out, pos, n = [], 0, len(text)
        for m in self._span.finditer(text):
            s, e = m.span()
            if s < pos:
                continue                       # inside a span already taken
            while s > pos and text[s - 1] in _TOKENISH_SET:
                s -= 1
            while e < n and text[e] in _TOKENISH_SET:
                e += 1
            out.append(text[pos:s])
            out.append(placeholder)
            pos = e
        out.append(text[pos:])
        return "".join(out)

    def scrub(self, text: str, placeholder: str = " ") -> str:
        """Text in, text out, with every offending token replaced.

        The default replacement is a space, which is what prose wants: a word
        cloud has nowhere to render a marker and a repeated "REDACTED" token
        would climb the chart. Error signatures pass one in, because they are
        already written in that idiom (PATH, HOST, EMAIL, MSG) and a gap in the
        middle of a failure message reads as a bug.

        The whole-text probe first: a plain literal alternation over a message
        is one C-level scan, and the overwhelming majority of messages mention
        nobody, so the per-token pass runs on the few that do.
        """
        if not text or not self._probe or not self._probe.search(text):
            return text
        if self._span is not None:
            text = self._scrub_spans(text, placeholder)
        return RE_TOKENISH.sub(
            lambda m: placeholder if self._anchor.search(m.group(0))
            else m.group(0),
            text)

    def scrub_label(self, label, placeholder=LABEL_PLACEHOLDER) -> str:
        """A facet KEY in, a safe key out.

        A cloud keyed on the empty string is worse than useless, so a label
        that redacts away entirely becomes the placeholder rather than
        vanishing -- the bucket still exists and still carries its counts.
        """
        if not label:
            return label
        if not self._probe or not self._probe.search(label):
            return label
        out = " ".join(self.scrub(label).split()).strip(" -_/.")
        return out or placeholder


# The redactor clean_prose() consults. Empty by default: importing this module
# must never start masking things on its own, and every test that does not opt
# in sees the old behaviour exactly.
_ACTIVE = Redactor()


def active_redactor() -> Redactor:
    return _ACTIVE


def install_redactor(redactor) -> Redactor:
    """Make `redactor` the one clean_prose() applies. Returns the previous one."""
    global _ACTIVE
    prev = _ACTIVE
    _ACTIVE = redactor if redactor is not None else Redactor()
    return prev


# --- deriving the identities -------------------------------------------------

def git_config_pairs(text):
    """((section, subsection, key), value) for a git-config file's contents.

    A tiny parser rather than `git config --list`: it works on a HOME that is
    not this process's, which is what makes the derivation testable, and it
    costs no subprocess per repository when reading dozens of .git/config
    files looking for remotes.
    """
    section = sub = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("["):
            head = line[1:].split("]")[0].strip()
            if '"' in head:
                section, _, rest = head.partition('"')
                section = section.strip().lower()
                sub = rest.rsplit('"', 1)[0]
            else:
                section, sub = head.lower(), ""
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        yield (section, sub, key.strip().lower()), val.strip().strip('"')


def _git_config_files(home):
    return [os.path.join(home, ".gitconfig"),
            os.path.join(home, ".config", "git", "config")]


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def identities_from_email(addr):
    """Every plausible handle inside an address's local part."""
    local = (addr or "").split("@")[0].strip()
    if not local:
        return []
    m = RE_GH_NOREPLY.match(local)
    if m:
        # 71827484+chensagi -> chensagi, and never the numeric id.
        local = m.group(2)
    out = [local]
    for piece in re.split(r"[+._\-]+", local):
        if piece and not piece.isdigit() and piece not in out:
            out.append(piece)
    return out


def owner_from_remote_url(url):
    """The account a git remote belongs to, or None."""
    m = RE_REMOTE_OWNER.match((url or "").strip())
    return m.group(1) if m else None


def owners_from_git_config(text):
    """Remote owners named in one .git/config."""
    out = []
    for (section, _sub, key), val in git_config_pairs(text):
        if section == "remote" and key == "url":
            owner = owner_from_remote_url(val)
            if owner:
                out.append(owner)
    return out


def derive_identities(home=None, repo_configs=(), extra_git_configs=()):
    """Candidate identity literals for the person running this tool.

    Sources, in the order they are trusted, unioned rather than ranked -- each
    one is wrong on some machine and they corroborate each other on most:

      1. the local account name (the home directory's basename). Always
         present, and it is already inside every ingest root.
      2. `[user] name` and `[user] email` from the user's git config, plus the
         handles hiding in the address's local part -- including GitHub's
         `<id>+<handle>@users.noreply.github.com` privacy form.
      3. the account half of every git remote in `repo_configs` (the
         `.git/config` contents of the repositories the corpus mentions).
         This is what catches an `owner/repo` slug whose owner is spelled
         differently from the local login.

    Source 2 is the person, spelled by the person, so it is gated by
    COMMON_WORDS alone (MIN_PERSONAL_LEN). Sources 1 and 3 are inferences --
    a home directory can be `/root` and a remote owner can be an ORGANISATION
    (`vercel`, `expo`, `npm`) rather than a human -- so they keep the length
    gate as a second guard against masking an ordinary word.

    Returns (accepted, skipped) where skipped is [(literal, reason)] -- see
    identity_verdict for why anything is ever skipped.
    """
    home = home or os.path.expanduser("~")
    cands = []          # (literal, min_len)

    def take(vals, min_len):
        for v in vals:
            cands.append((v, min_len))

    base = os.path.basename(str(home).rstrip("/"))
    if base:
        take([base], MIN_IDENTITY_LEN)
    texts = [_read(p) for p in _git_config_files(home)]
    texts.extend(extra_git_configs or ())
    for text in texts:
        for (section, _sub, key), val in git_config_pairs(text):
            if section != "user":
                continue
            if key == "name":
                # A display name is often "Jane Doe". Both the whole name and
                # each word are candidates: the words are what appears in
                # prose ("Hey Chen"), and the whole name is the only thing
                # left when one word is a COMMON_WORD ("Rose Smith" -> `rose`
                # is refused, `rose smith` still masks the signature line).
                take([val], MIN_PERSONAL_LEN)
                take(val.split(), MIN_PERSONAL_LEN)
            elif key == "email":
                take(identities_from_email(val), MIN_PERSONAL_LEN)
        take(owners_from_git_config(text), MIN_IDENTITY_LEN)
    for text in repo_configs or ():
        take(owners_from_git_config(text), MIN_IDENTITY_LEN)

    # One literal, one verdict: when a string arrives from two sources, the
    # strongest (lowest gate) wins -- `janedoe` is both the home directory and
    # the address's local part, and the address is the one that knows.
    gate = {}
    order = []
    for c, min_len in cands:
        s = (c or "").strip().lower()
        s = " ".join(s.split())
        if not s:
            continue
        if s not in gate:
            order.append(s)
            gate[s] = min_len
        else:
            gate[s] = min(gate[s], min_len)

    accepted, skipped = [], []
    for s in order:
        ok, why = identity_verdict(s, min_len=gate[s])
        (accepted if ok else skipped).append(s if ok else (s, why))
    return accepted, skipped


# --- deriving the branch names -----------------------------------------------

def _segments(name):
    return [s for s in re.split(r"[-_]+", name or "") if s]


def branch_redaction_terms(pairs, min_bare=MIN_BARE_BRANCH_LEN):
    """Worktree branch names -> (bare, decorated, skipped).

    `pairs` is what ingest saw: (branch, repo) for every worktree path it
    normalised, repo possibly "".

    * `decorated` are always safe. `worktree-<branch>` is not a word in any
      language; the only way that string exists is that somebody wrote it
      about this branch. These go in unconditionally, whatever the branch is
      called, which is what makes a branch named `docs` or `main` survivable.
    * `bare` is the branch name itself, and it only goes in when it cannot be
      mistaken for vocabulary: a compound (`combat-engine`, `native-ota`)
      always qualifies, a single word has to clear MIN_BARE_BRANCH_LEN and the
      common-word list.
    * ...plus the SHARED STEM of two or more sibling branches in one repo.
      `graphify-ab-control`, `graphify-ab-graph`, `graphify-ab2-control` and
      `graphify-ab2-graph` are four arms of one experiment, and the thing the
      owner does not want published is the experiment: `graphify`. A stem
      shared by siblings is a feature-family name; a leading segment of a
      LONE branch is not (that rule would offer up `native`, `combat` and
      `ios`), so two siblings is the bar.
    """
    branches, repos = {}, {}
    for item in pairs or ():
        branch, repo = (item if isinstance(item, (tuple, list)) else (item, ""))
        b = (branch or "").strip().strip("/").lower()
        if not b or b in ("worktrees", "workspaces"):
            continue
        branches[b] = True
        repos.setdefault((repo or "").strip().lower(), set()).add(b)

    decorated, bare, skipped = set(), set(), []
    for b in branches:
        for marker in ("worktree", "worktrees", "workspace", "workspaces"):
            decorated.add(f"{marker}-{b}")
        segs = _segments(b)
        if len(segs) > 1:
            ok, why = identity_verdict(b, min_len=MIN_IDENTITY_LEN)
        else:
            ok, why = identity_verdict(b, min_len=min_bare)
        if ok:
            bare.add(b)
        else:
            skipped.append((b, why))

    for _repo, sibs in repos.items():
        if len(sibs) < 2:
            continue
        stems = {}
        for b in sibs:
            segs = _segments(b)
            for i in range(1, len(segs)):
                stems.setdefault("-".join(segs[:i]), set()).add(b)
        for stem, owners in stems.items():
            if len(owners) < 2 or stem in bare:
                continue
            ok, why = identity_verdict(stem, min_len=min_bare)
            if ok:
                bare.add(stem)
            else:
                skipped.append((stem, why))

    return sorted(bare), sorted(decorated), skipped


def read_redact_file(path):
    """User-supplied literals: one per line, `#` starts a comment."""
    out = []
    for raw in _read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


REDACT_FILE_ENV = "VIBECHECKUP_REDACT_FILE"
REDACT_FILE_DEFAULT = os.path.join("~", ".config", "vibecheckup", "redact")

REDACT_HELP = (
    "extra literal to mask everywhere (repeatable). Use it for a handle, "
    "codename or client the tool cannot derive on its own, or for one it "
    "skipped as a common word. Also read from " + REDACT_FILE_DEFAULT +
    " (or $" + REDACT_FILE_ENV + "), one string per line, # for comments."
)

BRANCH_MODES = ("full", "decorated", "off")


def redact_file_path(env=None):
    env = os.environ if env is None else env
    return env.get(REDACT_FILE_ENV) or os.path.expanduser(REDACT_FILE_DEFAULT)


def build_redaction(home=None, repo_configs=(), branch_pairs=(), extra=(),
                    branch_mode="full", read_file=True, env=None, sidecar=None):
    """The one place a Redactor is assembled, so both stages agree.

    Returns (redactor, report). The report is for the TERMINAL -- it names the
    literals so the user can see what is being masked on their own machine.
    It must never be written into stats.json or vocab.json, which are the
    files that get shared: a list of exactly the strings somebody wanted
    hidden is the leak, spelled out.
    """
    explicit = [s.strip() for s in (extra or ()) if s and s.strip()]
    if read_file:
        explicit += read_redact_file(redact_file_path(env))

    identities, skipped = derive_identities(home=home, repo_configs=repo_configs)
    bare, decorated, branch_skipped = branch_redaction_terms(branch_pairs)
    if branch_mode == "off":
        bare, decorated = [], []
    elif branch_mode == "decorated":
        bare = []

    # ingest.py's sidecar carries what only the filesystem could answer -- the
    # git remotes of the repositories the sessions ran in. It has already been
    # through the guard once, so it is merged into the buckets it came from
    # rather than being passed off as something the user typed.
    def merge(base, key):
        extra_vals = (sidecar or {}).get(key) or []
        if branch_mode == "off" and key in ("branches", "decorated"):
            return base
        if branch_mode == "decorated" and key == "branches":
            return base
        return sorted(set(base) | {v.strip().lower() for v in extra_vals
                                   if isinstance(v, str) and v.strip()})

    identities = merge(identities, "identities")
    bare = merge(bare, "branches")
    decorated = merge(decorated, "decorated")
    explicit = sorted({s.lower() for s in explicit}
                      | {v.strip().lower() for v in
                         (sidecar or {}).get("explicit") or []
                         if isinstance(v, str) and v.strip()})

    r = Redactor()
    r.add_all(identities)
    r.add_all(bare)
    r.add_all(decorated)
    # Explicit strings bypass the guard entirely -- see identity_verdict.
    r.add_all(explicit)
    report = {
        "identities": identities,
        "branches": bare,
        "decorated": decorated,
        "explicit": explicit,
        "skipped": skipped + branch_skipped,
        "branch_mode": branch_mode,
    }
    # A report that lies is worse than no report: anything the matcher cannot
    # actually apply is moved out of the masked lists and named as NOT masked.
    demote_unmatchable(report)
    return r, report


# --- the report has to be true -----------------------------------------------

# The buckets format_redaction() presents as "masked".
MASKED_BUCKETS = ("identities", "branches", "decorated", "explicit")

UNMATCHABLE_REASON = "the matcher cannot apply it"


def literal_is_applicable(lit) -> bool:
    """Can the matcher actually mask this literal anywhere?

    The check the whole self-redaction promise rests on, and it runs both code
    paths, because they are different: hits() consults the anchor directly and
    is what tokenize.keep() calls, while scrub() rewrites TOKENISH runs and is
    what clean_prose() calls. A literal containing a SPACE used to pass the
    first and silently fail the second, which is how a two-word `user.name`
    came to be printed as masked and published on the share card anyway.
    """
    s = (lit or "").strip()
    if not s:
        return False
    probe = Redactor([s])
    if not probe.hits(s):
        return False
    carrier = "zqx " + s + " zqx"
    return probe.scrub(carrier) != carrier


def unmatchable_literals(report):
    """[(literal, bucket)] for everything the report claims but cannot mask."""
    out = []
    for bucket in MASKED_BUCKETS:
        for lit in report.get(bucket) or ():
            if not literal_is_applicable(lit):
                out.append((lit, bucket))
    return out


def demote_unmatchable(report):
    """Move unmaskable literals out of the masked lists and into `skipped`.

    Returns the offenders. Mutates `report` in place: the terminal, the tests
    and any caller reading the report all see the same, true, answer.
    """
    bad = unmatchable_literals(report)
    if not bad:
        return bad
    dead = {lit for lit, _ in bad}
    for bucket in MASKED_BUCKETS:
        if report.get(bucket):
            report[bucket] = [x for x in report[bucket] if x not in dead]
    known = {s for s, _ in report.get("skipped") or ()}
    report["skipped"] = list(report.get("skipped") or []) + [
        (lit, UNMATCHABLE_REASON) for lit in sorted(dead) if lit not in known]
    return bad


def format_redaction(report, width=6):
    """A few terminal lines describing what will be masked, and what was not."""
    def show(items):
        items = list(items)
        head = ", ".join(items[:width])
        return head + (f" (+{len(items) - width} more)" if len(items) > width else "")

    # Self-check, deliberately repeated here rather than trusted from
    # build_redaction(): this function is what the user reads, so it is the
    # last place that can stop it claiming something untrue. A report handed in
    # from anywhere else -- a test, a sidecar, a future caller -- is verified
    # too, and offenders come out under NOT masked instead.
    report = dict(report)
    report["skipped"] = list(report.get("skipped") or [])
    demote_unmatchable(report)

    lines = []
    n = (len(report["identities"]) + len(report["branches"])
         + len(report["decorated"]) + len(report["explicit"]))
    lines.append(f"redaction        {n} literal{'' if n == 1 else 's'} masked "
                 f"in prose and in every facet")
    if report["identities"]:
        lines.append(f"  identities     {show(report['identities'])}")
    if report["branches"]:
        lines.append(f"  branches       {show(report['branches'])}")
    if report["decorated"]:
        lines.append(f"  worktree forms {len(report['decorated'])} decorated "
                     f"spellings (worktree-<branch> and friends)")
    if report["explicit"]:
        lines.append(f"  --redact       {show(report['explicit'])}")
    # The other half of the promise. A short literal is masked at both token
    # edges (see Redactor._pattern), so `chen` covers `chen`, `Chen's` and
    # `chen-sagi` and deliberately does NOT cover a longer word built on it.
    # That is what stops `cs` deleting `csv` -- and it means a repo, org or
    # product named after the person survives. Saying so is the whole F1
    # lesson: a limitation the user is told about is one they can close.
    short = sorted({x for b in MASKED_BUCKETS for x in (report.get(b) or ())
                    if len(x) < EDGE_ANCHOR_LEN})
    if short:
        lines.append(f"  whole word only {show(short)}")
        lines.append("                 short literals mask as whole words "
                     "(name, name's, name-two) but never inside a longer one, "
                     "or `cs` would delete `csv`.")
        lines.append("                 A repo, org or product built on one "
                     "(<name>site, <name>corp) is NOT masked -- pass --redact "
                     "STR for it.")
    if report["skipped"]:
        pairs = ", ".join(f"{s} ({why})" for s, why in report["skipped"][:width])
        more = len(report["skipped"]) - width
        lines.append(f"  NOT masked     {pairs}{f' (+{more} more)' if more > 0 else ''}")
        lines.append("                 redacting these would delete real words; "
                     "pass --redact STR to force one.")
        if any(why == UNMATCHABLE_REASON for _s, why in report["skipped"]):
            lines.append("                 (a literal the matcher cannot apply is "
                         "listed here rather than claimed as masked.)")
    return "\n".join(lines)
