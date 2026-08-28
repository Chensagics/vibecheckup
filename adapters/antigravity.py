"""Antigravity: ~/.gemini/antigravity-cli/conversations/<uuid>.db

Each conversation is its own SQLite DB whose `steps.step_payload` column holds
schema-less protobuf. Text is recovered with the generic wire walker in
protoscan.py, so every event here is confidence="heuristic".

step_type -> role/kind was established empirically by extracting text across a
40-conversation sample and reading what each type actually contained.
"""
from __future__ import annotations

import os
import sqlite3
from glob import glob

import re

from .base import Event, mtime_iso, project_from_cwd
from .protoscan import extract_strings

NAME = "antigravity"
ROOT = os.path.expanduser("~/.gemini/antigravity-cli/conversations")

# Empirically labelled; see module docstring.
STEP_TYPES = {
    14:  ("user", "prompt"),        # typed instruction
    23:  ("user", "prompt"),        # typed instruction (variant)
    132: ("assistant", "reply"),    # {"Message": "..."} final prose
    15:  ("assistant", "thinking"), # reasoning summary
    21:  ("assistant", "tool_call"),# {"CommandLine": ..., "Cwd": ...}
    9:   ("assistant", "tool_call"),# {"DirectoryPath": ..., "toolAction": ...}
    17:  ("assistant", "tool_call"),# permission / tool invocation envelope
    127: ("assistant", "tool_call"),# subagent dispatch
    5:   ("tool", "tool_result"),   # file content
    8:   ("tool", "tool_result"),   # file content
    7:   ("tool", "tool_result"),   # search / listing output
    90:  ("system", "meta"),        # EPHEMERAL_MESSAGE reminders (injected)
    98:  ("system", "meta"),        # conversation-history injection
    101: ("system", "meta"),        # inter-agent messages / hook notices
    139: ("system", "meta"),        # AGENTS.md injection
    33:  ("system", "meta"),        # skills documentation injection
    25:  ("assistant", "tool_call"),# {"Pattern": ..., "SearchDirectory": ...}
    91:  ("assistant", "tool_call"),# image generation
    138: ("assistant", "tool_call"),# structured question to the user
    28:  ("tool", "error"),         # cascade step failure
    31:  ("tool", "error"),         # document fetch failure
}

# Steps whose payloads are file bodies get truncated hard: they are never
# tokenized as prose and dominate runtime otherwise.
BULK_TYPES = {5, 8, 7}


def discover():
    return sorted(glob(os.path.join(ROOT, "*.db")))


def _open(path):
    """Open read-only. `immutable=1` is faster but makes SQLite ignore the
    write-ahead log, which for a live-written DB looks like corruption -- so
    fall back to a plain read-only handle that honours the WAL.
    """
    for uri in (f"file:{path}?mode=ro&immutable=1", f"file:{path}?mode=ro"):
        try:
            con = sqlite3.connect(uri, uri=True)
            con.execute("select count(*) from steps").fetchone()
            return con
        except sqlite3.Error:
            try:
                con.close()
            except Exception:
                pass
    return None


def _cwd(con):  # noqa: D401
    try:
        rows = con.execute("select data from trajectory_metadata_blob").fetchall()
    except sqlite3.Error:
        return None
    for (blob,) in rows:
        for _, s in extract_strings(blob):
            if s.startswith("file:///"):
                return s[len("file://"):]
            if s.startswith("/Users/") or s.startswith("/home/"):
                return s
    return None


RE_UUID_ONLY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
RE_PERMS = re.compile(r"^(?:\w+\(\*\)\s*)+$")


def _is_envelope(s):
    """Protobuf steps carry routing metadata beside the actual message."""
    t = s.strip()
    if not t or RE_UUID_ONLY.match(t) or RE_PERMS.match(t):
        return True
    low = t[:40].lower()
    return (low.startswith("sessionid")
            or low.startswith("unsupported mime type")
            or '"step_index"' in t[:200]
            or t.startswith("file:///")
            or (t.startswith("{") and '"toolAction"' in t[:400]))


def _pick_text(strings, kind):
    """One authored message per step, not every string in the blob.

    Joining all extracted strings drags session IDs, permission lists and step
    metadata into the prose, which is what poisoned the first pass.
    """
    cands = [s for _, s in strings if not _is_envelope(s)]
    if not cands:
        return ""
    if kind in ("prompt", "reply", "thinking"):
        best = max(cands, key=len)
        if best.lstrip().startswith("{"):
            import json
            try:
                obj = json.loads(best)
                if isinstance(obj, dict):
                    for k in ("Message", "message", "Prompt", "text"):
                        if isinstance(obj.get(k), str) and obj[k].strip():
                            return obj[k]
            except (json.JSONDecodeError, ValueError):
                pass
        return best
    return "\n".join(cands)


def _command(text):
    """Pull the shell command out of a {"CommandLine": "..."} payload."""
    if '"CommandLine"' not in text:
        return None
    import json
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("CommandLine"), str):
        return obj["CommandLine"]
    return None


def iter_events(path, report):
    sid = os.path.splitext(os.path.basename(path))[0]
    ts = mtime_iso(path)
    con = _open(path)
    if con is None:
        report.bad_file(NAME)
        return
    try:
        project = project_from_cwd(_cwd(con))
        try:
            rows = con.execute(
                "select idx, step_type, step_payload from steps order by idx")
        except sqlite3.Error:
            report.bad_file(NAME)
            return
        for idx, st, payload in rows:
            mapping = STEP_TYPES.get(st)
            if mapping is None:
                report.unknown(NAME, f"step_type/{st}")
                role, kind = "system", "meta"
            else:
                role, kind = mapping
            if not payload:
                continue
            try:
                strings = extract_strings(payload)
            except Exception:
                report.bad_line(NAME)
                continue
            if not strings:
                continue
            if st in BULK_TYPES:
                text = strings[0][1][:2000]
            else:
                text = _pick_text(strings, kind)
            if not text:
                continue
            cmd = _command(text) if kind == "tool_call" else None
            tool_name = None
            if st == 21:
                tool_name = "shell"
            elif st == 9:
                tool_name = "list_dir"
            elif st == 127:
                tool_name = "subagent"
            elif st == 25:
                tool_name = "search"
            elif st == 91:
                tool_name = "generate_image"
            elif st == 138:
                tool_name = "ask_question"
            yield Event(ts, NAME, sid, project, role, kind, text,
                        tool_name=tool_name, cmd=cmd, ts_exact=False,
                        confidence="heuristic")
    finally:
        con.close()
