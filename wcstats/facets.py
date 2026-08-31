"""Facet accumulation: one bucket per global / tool / project / month slice."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .clean import HOSTNAME_TLDS, RE_ANSI, RE_EMAIL, RE_WINPATH
from .score import log_odds, pmi_phrases, top_n

# --- file extensions ---------------------------------------------------------

# What may legitimately be called a "file type" when the token carries no
# directory of its own. Anything outside this set has to prove itself by
# sitting on a path -- see RE_EXT.
FILE_EXTS = """
py pyi pyx pyc pyo ipynb js mjs cjs jsx ts tsx mts cts json json5 jsonl
ndjson html htm xhtml css scss sass less styl svg vue svelte astro
md mdx rst txt text adoc tex bib
sh bash zsh fish ps1 psm1 bat cmd awk sed
c h cc cpp cxx hpp hh m mm swift kt kts java scala clj cljs cljc
go rs rb erb php pl pm lua r jl dart ex exs erl hs ml mli fs fsx vb cs
zig nim cr elm purs res hx sol move wgsl glsl frag vert metal cu asm
sql prisma graphql gql proto thrift avsc
yml yaml toml ini cfg conf config env lock plist
xml xsd xsl csv tsv parquet avro
png jpg jpeg gif webp bmp ico icns tiff heic avif
mp3 mp4 wav ogg flac aac m4a mov avi mkv webm
pdf doc docx xls xlsx ppt pptx pages numbers epub
zip tar gz tgz bz2 xz zst rar dmg pkg deb rpm apk ipa aab
log out output err diff patch bak tmp swp orig rej snap
mk cmake gradle bzl sum mod
tres tscn gd import uid shader
maestro example sample dist min map
ttf otf woff woff2 eot wasm so dylib dll exe bin obj pdb
db sqlite sqlite3
""".split()

# analyze.py hands this the whole trailing whitespace token and reads group(1),
# so the pattern has to judge the *whole* token, not just its tail. Two ways to
# qualify:
#   * the token carries a directory separator, so it is a path and any
#     extension-shaped suffix is a real file type; or
#   * it is a bare name whose suffix is a known extension.
# The old tail-only rule accepted the last label of anything, so bare hostnames
# landed in the "File types touched" cloud: `help.etoro` shipped `etoro` (a
# COMPANY NAME rendered as a file type), `grok.com`/`bearbulltraders.com` shipped
# `com`, `grok.me` shipped `me`, and `com.chencorp.finn` shipped `finn`.
RE_EXT = re.compile(
    r"^(?![^\s]*://)"                                   # never a URL
    r"(?:[^\s]*[/\\][^\s]*"                             # ...a path, or
    r"|[^\s/\\]*(?=\.(?:" + "|".join(FILE_EXTS) + r")$))"   # a known extension
    r"\.([A-Za-z][A-Za-z0-9]{0,6})$",
    re.I,
)

# --- error signatures --------------------------------------------------------

RE_MASK_NUM = re.compile(r"\d+")
# Stops at a quote or bracket: a URL is very often quoted inside an error, and
# a greedy \S+ would swallow the closing quote and the clause after it.
RE_MASK_URL = re.compile(r"\b\w+://[^\s'\"`<>)\]}]+")
RE_MASK_PATH = re.compile(r"(?:/[\w.\-@+]+)+")
# Relative paths and owner/slug pairs. Both sides need three characters, so
# "and/or" and "24/7" are safe. This is what leaves the identity behind when it
# is missing: `chensagi/vibecheckup` used to mask as `chensagiPATH`, publishing
# a Vercel account name, and `src/components/x.tsx` as `srcPATH`.
RE_MASK_RELPATH = re.compile(r"\b\w[\w.@+\-]{2,}(?:/[\w.@+\-]{3,})+/?")
RE_MASK_HANDLE = re.compile(r"(?<![\w/])@[A-Za-z][\w.\-]{1,38}")
# Reverse-DNS bundle ids name the organisation that ships the app
# (`com.chencorp.finn`), which is the same class of identity as a hostname.
RE_MASK_BUNDLE = re.compile(
    r"\b(?:com|org|io|net|dev|app|me|co)(?:\.[\w\-]+){2,}\b", re.I)
RE_MASK_HOST = re.compile(
    r"\b[A-Za-z0-9][\w\-]*(?:\.[\w\-]+)*\.(?:"
    + "|".join(sorted(HOSTNAME_TLDS)) + r")\b", re.I)
RE_MASK_HEX = re.compile(r"\b[0-9a-f]{8,}\b", re.I)
RE_MASK_QUOTE = re.compile(r"'[^']{1,80}'|\"[^\"]{1,80}\"")
# `error: could not apply <rev>... <commit subject>` -- git prints the author's
# own commit message, verbatim, as part of a rebase failure.
RE_COMMIT_TAIL = re.compile(r"(HEX|\b[0-9a-f]{6,}\b)\.\.\.\s+\S.*$")
# ...and the same subject on its own, in the conventional-commit shape.
RE_CONV_COMMIT = re.compile(
    r"\b(?:feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)"
    r"(?:\([^)]{0,40}\))?:\s+\S.*$")
# A sentence boundary: a full stop between a word character and a capital.
# `HEX... feat` is deliberately not one -- three dots, no word char before the
# last of them.
RE_SENTENCE = re.compile(r"(?<=[\w)\]'\"])\.\s+(?=[A-Z])")

SHELL_SUBCMD = {"git", "npm", "pnpm", "yarn", "docker", "cargo", "go", "kubectl",
                "brew", "pip", "pip3", "vercel", "gh", "expo", "eas", "supabase"}


# --- picking the line that IS the failure -------------------------------------
#
# The old rule was "the first line containing error|exception|failed|fatal|
# traceback|cannot|not found|permission denied|refused", anywhere in the line,
# over the whole body of any event an adapter had flagged. Those words are
# ordinary English, and the shipped dashboard.html carried the result under
# "What went wrong most": a product-curriculum sentence ("*(If you cannot lose
# small, you cannot stay in the game.)*"), an agent-doctrine table row, a
# skill's frontmatter `description:`, a JSON issue title, four lines of
# application source (`if (errors.length) {`), a .gitignore entry
# (`yarn-error.log*`) and a git push confirmation.
#
# A keyword now only counts where a machine puts one. Seven shapes, each of
# them something no prose sentence does by accident.

# Leading noise a diagnostic may still carry: indent, bullet, heading, glyph.
_LEAD = r"[\s>*+\-#~•·▸►×✗✖❌⚠]{0,4}"
# 1. the marker heads the line.
RE_ERR_HEAD = re.compile(
    r"^" + _LEAD + r"(?:error|errors|err|exception|failure|failed|fail|fatal|panic|"
    r"panicked|traceback|critical|abort|aborted|refused|"
    r"cannot|could\s+not|unable\s+to|no\s+such|not\s+found)\b", re.I)
# ...and the STATUS words, which are only ever a fallback. `Exit code 1` heads
# the body of a shell failure whose real diagnostic is ten lines further down,
# so treating it as a marker would replace `SyntaxError: ...`, `fatal: not a
# git repository` and `rg: PATH: No such file` with "Exit code N" -- 17
# signatures collapsed onto one in a first pass over the real corpus.
RE_ERR_STATUS = re.compile(
    r"^" + _LEAD + r"(?:warning|warn|denied|timeout|timed\s+out|usage|"
    r"exit\s+(?:code|status)|missing|invalid|no\s+text|nothing)\b", re.I)
# 2. a program name, then the marker: `npm error code ENOTFOUND`, `rg: ...`.
#    `err` and `fail` are left out on purpose -- with them, node's stack-trace
#    preamble `  throw err;` outranked the `Error: Cannot find module` two
#    lines below it.
#    The program name is lower-case by convention (`npm`, `rg`, `task.sh`),
#    which is what keeps a docstring's "Raises error if neither is available."
#    out. Scoped flags rather than re.I so that stays true.
RE_ERR_PROG = re.compile(
    r"^\s*[a-z][\w.\-/]{0,19}[:\s]\s*(?i:error|errors|warning|fatal|failed)\b")
# 3. a file:line:col or positional prefix, then the marker (eslint, make).
RE_ERR_POS = re.compile(
    r"^[\s\d:.\-]{1,24}(?:error|warning|fatal|failed|fail)\b", re.I)
# 4. an exception CLASS name. Case-sensitive on purpose: `TypeError`,
#    `ParserError`, `java.lang.ExceptionInInitializerError` are identifiers,
#    while "the sanctioned exception to doctrine" is a sentence.
RE_ERR_CLASS = re.compile(r"\b[A-Za-z_][\w.]*(?:Error|Exception)\b")
# 5. a log level, bracketed or standing alone in caps. `COL_ERROR` is safe:
#    `_` is a word character, so there is no boundary in front of ERROR.
RE_ERR_LEVEL = re.compile(
    r"[\[<(:]\s*(?:ERROR|ERR|FATAL|WARN|WARNING|CRITICAL|PANIC)\s*[\]>):]"
    r"|\b(?:ERROR|FATAL|PANIC|ABORTED|CRITICAL|FAILED|FAIL|ERR)\b"
    r"|\bERR!")
# 6. fixed diagnostic idioms and codes -- compiler codes (`error TS2724`),
#    `(os error 2)`, an errno, an `<...error...>` tag, and the shell's four.
#    The errno list is spelled out rather than matched as E[A-Z]+, which would
#    have read ENGINE and EVERY in a shouty prose line as failures.
RE_ERR_IDIOM = re.compile(
    r"\berror\s*[\[(]?[A-Z]{1,5}\d{2,}"
    r"|\(\s*(?:os\s+)?error\s+\d+\s*\)"
    r"|\bE(?:ACCES|ADDRINUSE|AGAIN|BADF|BUSY|CANCELED|CHILD|CONNREFUSED|"
    r"CONNRESET|EXIST|FAULT|HOSTUNREACH|INTR|INVAL|IO|ISDIR|MFILE|NETUNREACH|"
    r"NFILE|NOENT|NOEXEC|NOMEM|NOSPC|NOTDIR|NOTEMPTY|NOTFOUND|NOTSUP|NXIO|"
    r"PERM|PIPE|PROTO|RANGE|RESOLVE|ROFS|SPIPE|SRCH|TIMEDOUT|USAGE)\b"
    r"|<[\w:.\-]*error[\w:.\-]*>"
    r"|\bnot\s+found\b|\bno\s+such\s+file\b|\bpermission\s+denied\b"
    r"|\bconnection\s+refused\b|\boperation\s+not\s+permitted\b"
    # `awk: syntax error at source line 1`, `(eval):26: parse error near`
    r"|\b(?:syntax|parse|type|runtime|internal|compile|compilation|io|os)"
    r"\s+error\b", re.I)
# 7. an `error:` LABEL. SINGULAR only, and that is the whole trick: a machine
#    writes `error:` / `fatal:` / `Parse Error:`, while prose that mentions
#    failure writes the plural -- "...to collect current errors:",
#    "- gaps: 88 (errors: 88, warnings: 0)", `"errors": []`.
RE_ERR_LABEL = re.compile(
    r"\b(?:error|exception|failure|fatal|warning|traceback|panic)\s*:", re.I)

# Lines that are SOURCE rather than output. A grep hit, a stack-trace preamble
# and a file the agent read all arrive inside a tool result, and no keyword
# rule can tell `if (errors.length) {` or `const COL_ERROR: Color = ...` from a
# failure. Deliberately narrow: a general "looks like markup" test would eat
# `<tool_use_error>...`, which is the single most common real diagnostic here.
RE_SOURCE_LINE = re.compile(
    r"^\s*(?:const|let|var|function|def|class|import|export|from|return|throw|"
    r"raise|if|elif|else|for|while|try|except|catch|finally|switch|case|await|"
    r"async|public|private|protected|static|package|impl|fn|struct|enum)\b"
    r"|[;{]\s*$"
    r"|^\s*(?://|/\*|\*\s)")

# Two passes, not one: the whole body is searched for a real diagnostic before
# any status line is considered, because they are not in reading order. A tool
# result opens with `Exit code 1` and only then prints the traceback.
ERR_TESTS_STRONG = (RE_ERR_HEAD, RE_ERR_PROG, RE_ERR_POS, RE_ERR_CLASS,
                    RE_ERR_LEVEL, RE_ERR_IDIOM, RE_ERR_LABEL)
ERR_TESTS_WEAK = (RE_ERR_STATUS,)

# With no marker anywhere, the event can still be a real failure: the harness's
# own refusals say so in plain English ("This session is isolated in the
# worktree ...", "The user doesn't want to proceed with this tool use.",
# "Exit code 1") and they are the single largest group in the corpus. What
# separates them from a document that merely mentions errors is length -- they
# are a sentence or two, not a file. A non-zero exit_code attests the same
# thing and lifts the limit.
FALLBACK_MAX_LINES = 40
FALLBACK_MAX_CHARS = 2000

# ...and the fallback line has to look like a MESSAGE. Every harness refusal in
# the corpus is a plain sentence; none of them is a markdown heading, a table
# row, a bullet, a numbered step or a JSON fragment. Requiring that is what
# stops the first line of a design note ("# Day trading, week 2") standing in
# for a failure when the rest of the note has no marker either.
RE_NOT_A_MESSAGE = re.compile(
    r"^(?:#{1,6}\s|\||>\s|[-*+]\s|\d+[.)]\s|[\"'`\[{])"
    r"|\*\*")


def looks_machine(line: str, weak: bool = True) -> bool:
    """True when this line carries a diagnostic marker where a machine puts one.

    `weak` includes the status lines (`Exit code 1`, `usage: ...`), which say
    that something failed without saying what.
    """
    s = (line or "").strip()
    if not s or RE_SOURCE_LINE.search(s):
        return False
    tests = ERR_TESTS_STRONG + ERR_TESTS_WEAK if weak else ERR_TESTS_STRONG
    return any(rx.search(s) for rx in tests)


def error_line(text: str, exit_code=None) -> str:
    """The line of `text` that IS the failure, or "" when none of it is."""
    if not text:
        return ""
    body = RE_ANSI.sub(" ", text)
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    candidates = [s for s in lines if not RE_SOURCE_LINE.search(s)]
    for tests in (ERR_TESTS_STRONG, ERR_TESTS_WEAK):
        for s in candidates:
            if any(rx.search(s) for rx in tests):
                return s
    if not candidates:
        return ""
    attested = isinstance(exit_code, int) and exit_code != 0
    if not attested and (len(body) > FALLBACK_MAX_CHARS
                         or len(lines) > FALLBACK_MAX_LINES):
        return ""
    head = candidates[0]
    return "" if RE_NOT_A_MESSAGE.search(head) else head


def error_signature(text: str, exit_code=None) -> str:
    """Collapse a failure message to a comparable signature.

    A signature is meant to be the *shape* of a failure, so anything
    author-specific riding along is a bug, not a detail: the dashboard renders
    these under "What went wrong most" and in every facet's errors dropdown.
    Masked here, in order: URLs, addresses, quoted free text, Windows and POSIX
    paths, relative paths and owner/slug pairs, @handles, hostnames, hashes,
    commit subjects, numbers -- and then only the first sentence is kept, which
    is what drops a vendor's paragraph or a trailing working directory.

    `exit_code` is optional attestation from the event: a non-zero one means
    something really did fail, and lifts the size limit on the fallback line.
    """
    line = error_line(text, exit_code)
    if not line:
        return ""
    line = RE_ANSI.sub(" ", line)
    # Quotes first: quoted free text is already the most volatile part of a
    # message, and collapsing it whole keeps the later rules off its insides.
    line = RE_MASK_QUOTE.sub("'X'", line)
    line = RE_MASK_URL.sub("URL", line)
    line = RE_EMAIL.sub("EMAIL", line)
    line = RE_WINPATH.sub("PATH", line)
    # Before RE_MASK_PATH, which would otherwise consume the tail of a
    # relative path and strip the owner name off the front of it.
    line = RE_MASK_RELPATH.sub("PATH", line)
    line = RE_MASK_PATH.sub("PATH", line)
    line = RE_MASK_HOST.sub("HOST", line)
    line = RE_MASK_BUNDLE.sub("BUNDLE", line)
    line = RE_MASK_HANDLE.sub("@USER", line)
    line = RE_MASK_HEX.sub("HEX", line)
    line = RE_COMMIT_TAIL.sub(r"\1... MSG", line)
    line = RE_CONV_COMMIT.sub("MSG", line)
    line = RE_MASK_NUM.sub("N", line)
    m = RE_SENTENCE.search(line)
    if m:
        line = line[:m.start() + 1]
    line = " ".join(line.split())
    return line[:110]


# --- how much one message may say -------------------------------------------
#
# Deduplication answers "the same prompt fired 400 times"; it has no answer for
# "one message said the same word 400 times". A single pasted translation table
# put its Arabic UI copy at rank 10 of the owner's most-used words, ahead of
# `data` and `files`, and ten such messages were a third of the whole corpus.
# He had typed none of it.
#
# clean.py now recognises that particular paste, but the general problem is not
# a missing pattern -- it is that the cloud is a raw sum, so any leak class the
# cleaner has not learned yet can still buy the headline outright.
#
# So a term counts once per message, however often that message repeats it.
# "Most used" becomes "used in the most separate prompts", which is both the
# more truthful reading of the question and structurally immune to the whole
# class of bug: no single document can ever outvote another, whatever it holds
# and whatever the cleaner failed to notice about it.
#
# prose_user_raw is deliberately left uncapped -- it is the "show me the
# unfiltered counts" toggle, and a filtered raw count would be a lie.
MAX_TERM_PER_DOC = 1


def capped(items, cap=MAX_TERM_PER_DOC):
    """One document's contribution, with no term counted more than `cap`."""
    c = Counter(items)
    if cap is None:
        return c
    for term, n in c.items():
        if n > cap:
            c[term] = cap
    return c


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
        self.prose_user.update(capped(toks))
        self.phrases_user.update(capped(phrases))

    def add_assistant(self, toks, phrases):
        self.words += len(toks)
        self.prose_asst.update(capped(toks))
        self.phrases_asst.update(capped(phrases))

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
