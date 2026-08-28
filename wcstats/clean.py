"""Content-based filtering: what counts as prose, and what is machine noise.

This is the highest-leverage module in the project. A typical Claude Code
session holds a handful of real prompts against hundreds of tool results and
hook attachments -- unfiltered, the corpus is overwhelmingly machine text and
every cloud reads "skill, important, file, the".

Adapters classify records by type; every judgement about *content* lives here.
"""
from __future__ import annotations

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
)

# --- line-level noise --------------------------------------------------------

RE_URL = re.compile(r"https?://\S+")
RE_PATH = re.compile(r"(?:/[\w.\-@+]+){2,}/?")
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


def _drop_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
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
    lines = [ln for ln in t.splitlines() if not _drop_line(ln)]
    t = "\n".join(lines)
    t = RE_URL.sub(" ", t)
    t = RE_UUID.sub(" ", t)
    t = RE_B64.sub(" ", t)
    t = RE_HEX.sub(" ", t)
    t = RE_PATH.sub(" ", t)
    t = RE_TAG.sub(" ", t)
    t = RE_NUM.sub(" ", t)
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
