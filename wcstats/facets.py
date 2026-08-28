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


def error_signature(text: str) -> str:
    """Collapse a failure message to a comparable signature.

    A signature is meant to be the *shape* of a failure, so anything
    author-specific riding along is a bug, not a detail: the dashboard renders
    these under "What went wrong most" and in every facet's errors dropdown.
    Masked here, in order: URLs, addresses, quoted free text, Windows and POSIX
    paths, relative paths and owner/slug pairs, @handles, hostnames, hashes,
    commit subjects, numbers -- and then only the first sentence is kept, which
    is what drops a vendor's paragraph or a trailing working directory.
    """
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
