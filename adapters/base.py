"""Normalized event model shared by every session-log adapter.

Adapters classify: they map a source record onto a (role, kind) pair and skip
records that carry no text at all. They never judge text by its content --
every content-based decision lives in wcstats/clean.py.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Optional

ROLES = {"user", "assistant", "tool", "system"}
KINDS = {"prompt", "reply", "thinking", "tool_call", "tool_result", "error", "meta"}

# Normalized token-usage slots. Providers spell these five different ways and
# disagree about what nests inside what, so every adapter converts to this one
# shape (see usage() for the invariants).
USAGE_KEYS = ("input", "output", "cache_read", "cache_write")

# Tool results are never tokenized as prose (only mined for error signatures),
# so we keep just enough of the head to recover a failure message.
TOOL_RESULT_CAP = 2000
TEXT_CAP = 200_000


@dataclass(slots=True)
class Event:
    ts: Optional[str]
    tool: str
    session_id: str
    project: str
    role: str
    kind: str
    text: str = ""
    tool_name: Optional[str] = None
    cmd: Optional[str] = None
    exit_code: Optional[int] = None
    ts_exact: bool = True
    confidence: str = "exact"
    tokens: Optional[int] = None
    model: Optional[str] = None
    usage: Optional[dict] = None

    def to_json(self) -> str:
        d = asdict(self)
        # Usage lives on a small minority of events; omitting the empty slots
        # keeps events.ndjson roughly the size it was before pricing existed.
        if d["model"] is None:
            del d["model"]
        if d["usage"] is None:
            del d["usage"]
        if d["kind"] == "tool_result" and len(d["text"]) > TOOL_RESULT_CAP:
            d["text"] = d["text"][:TOOL_RESULT_CAP]
        elif len(d["text"]) > TEXT_CAP:
            d["text"] = d["text"][:TEXT_CAP]
        return json.dumps(d, ensure_ascii=False)


def usage(input=0, output=0, cache_read=0, cache_write=0) -> Optional[dict]:
    """Normalized token counts, or None when the record carries no usage.

    Two invariants every adapter must honour before calling this:

    * ``input`` is *uncached* input only. Anthropic already reports it that
      way; Codex and Gemini report a cached count that is a **subset** of
      input, so those adapters subtract it and pass it as ``cache_read``.
    * ``output`` includes reasoning/thinking tokens, which every provider
      bills at the output rate. Codex's ``output_tokens`` already contains
      them; Gemini's ``thoughts`` sits outside ``output`` and is added in.

    An all-zero record (Claude Code's ``<synthetic>`` messages, for one) is
    not usage -- returning None keeps it out of the unpriced-model report.
    """
    vals = (input, output, cache_read, cache_write)
    d = {k: max(int(v or 0), 0) for k, v in zip(USAGE_KEYS, vals)}
    return d if any(d.values()) else None


def iso(ts) -> Optional[str]:
    """Normalize assorted timestamp spellings to UTC ISO-8601, or None."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # Heuristic: values past year ~2001 in ms are >1e12.
        if ts > 1e12:
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(ts, str):
        s = ts.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def mtime_iso(path: str) -> Optional[str]:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat()
    except OSError:
        return None


def project_from_cwd(cwd: Optional[str]) -> str:
    """Basename of a working directory, used as the project label."""
    if not cwd:
        return "unknown"
    cwd = cwd.rstrip("/")
    return os.path.basename(cwd) or "unknown"


def decode_dash_path(name: str) -> str:
    """Reconstruct a real path from Claude Code's dash-encoded directory name.

    Claude replaces "/" with "-", which is ambiguous when the directory itself
    contains dashes ("my-web-app" -> "my-web-app" or ".../my/web/app"?).
    Resolve greedily against the filesystem: at each level, prefer the longest
    token run that names a real directory. Falls back to a positional guess
    when the path no longer exists on disk.
    """
    tokens = [t for t in name.strip("-").split("-")]
    if not tokens:
        return "unknown"
    cur = ""
    i = 0
    resolved = True
    while i < len(tokens):
        best = None
        for j in range(len(tokens), i, -1):
            cand = cur + "/" + "-".join(tokens[i:j])
            if os.path.isdir(cand):
                best = (cand, j)
                break
        if best is None:
            resolved = False
            break
        cur, i = best
    if resolved and cur:
        return cur
    # Fallback: assume /<a>/<b>/<c>/<rest-with-dashes>, e.g. /Users/x/Projects/foo-bar
    if len(tokens) > 3:
        return "/" + "/".join(tokens[:3]) + "/" + "-".join(tokens[3:])
    return "/" + "/".join(tokens)


def project_from_encoded_dir(name: str) -> str:
    """Claude Code and Grok both encode the cwd into the directory name.

    Claude:  -Users-alice-Projects-my-web-app
    Grok:    %2FUsers%2Falice%2FProjects%2Fmy-web-app
    """
    if "%2F" in name or "%2f" in name:
        return project_from_cwd(urllib.parse.unquote(name))
    return project_from_cwd(decode_dash_path(name))


def decoded_cwd_from_dir(name: str) -> Optional[str]:
    if "%2F" in name or "%2f" in name:
        return urllib.parse.unquote(name)
    return decode_dash_path(name)


def read_jsonl(path: str):
    """Stream a .jsonl file, yielding (lineno, obj). Malformed lines yield None."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except (json.JSONDecodeError, ValueError):
                yield i, None


def blocks_text(content) -> str:
    """Flatten an Anthropic-style content array (or bare string) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, str):
            out.append(b)
        elif isinstance(b, dict):
            if isinstance(b.get("text"), str):
                out.append(b["text"])
            elif isinstance(b.get("content"), (str, list)):
                out.append(blocks_text(b["content"]))
    return "\n".join(x for x in out if x)


def argv_head(cmd: Optional[str]) -> Optional[str]:
    """First real token of a shell command, skipping env assignments."""
    if not cmd:
        return None
    for tok in cmd.strip().split():
        if "=" in tok and not tok.startswith("-") and tok.split("=")[0].isidentifier():
            continue
        tok = tok.strip("(){}`'\"")
        return tok.rsplit("/", 1)[-1] or None
    return None
