"""Grok CLI: ~/.grok/sessions/<url-encoded-cwd>/{chat_history,prompt_history}.jsonl

Gotcha: chat_history.jsonl records carry no timestamp. User prompts are dated
exactly by joining prompt_history.jsonl on session_id; everything else inherits
the file mtime and is marked ts_exact=False.
"""
from __future__ import annotations

import os
from glob import glob

from .base import (Event, blocks_text, iso, mtime_iso, project_from_encoded_dir,
                   read_jsonl)

NAME = "grok"
ROOT = os.path.expanduser("~/.grok/sessions")


def discover():
    """chat_history.jsonl lives at <enc-cwd>/<session-id>/, one level deeper
    than prompt_history.jsonl, which sits at <enc-cwd>/."""
    return sorted(set(
        glob(os.path.join(ROOT, "*", "chat_history.jsonl"))
        + glob(os.path.join(ROOT, "*", "*", "chat_history.jsonl"))))


def _prompt_times(dirpath):
    """prompt -> earliest exact timestamp, from prompt_history.jsonl.

    It may sit beside chat_history.jsonl or one directory up, depending on
    whether the session is nested under a per-cwd folder.
    """
    out = {}
    ph = None
    for cand in (os.path.join(dirpath, "prompt_history.jsonl"),
                 os.path.join(os.path.dirname(dirpath), "prompt_history.jsonl")):
        if os.path.exists(cand):
            ph = cand
            break
    if ph is None:
        return out
    for _, d in read_jsonl(ph):
        if not isinstance(d, dict):
            continue
        prompt = d.get("prompt")
        ts = iso(d.get("timestamp"))
        if isinstance(prompt, str) and ts:
            out.setdefault(prompt.strip()[:200], ts)
    return out


def iter_events(path, report):
    dirpath = os.path.dirname(path)
    dirname = os.path.basename(dirpath)
    parent = os.path.basename(os.path.dirname(dirpath))
    # The encoded cwd is whichever of the two levels carries the encoding.
    enc = dirname if "%2F" in dirname or "%2f" in dirname else parent
    project = project_from_encoded_dir(enc)
    session_id = dirname[-60:]
    fallback_ts = mtime_iso(path)
    ptimes = _prompt_times(dirpath)
    calls = {}

    for lineno, d in read_jsonl(path):
        if d is None:
            report.bad_line(NAME)
            continue
        t = d.get("type")
        content = d.get("content")
        text = blocks_text(content) if content is not None else ""

        if t == "user":
            # Injected, not typed by the user.
            if d.get("synthetic_reason"):
                yield Event(fallback_ts, NAME, session_id, project, "system",
                            "meta", ts_exact=False)
                continue
            ts = ptimes.get(text.strip()[:200], fallback_ts)
            yield Event(ts, NAME, session_id, project, "user", "prompt", text,
                        ts_exact=ts is not fallback_ts)
        elif t == "assistant":
            for tc in (d.get("tool_calls") or []):
                if isinstance(tc, dict):
                    fn = (tc.get("function") or {}) if isinstance(tc.get("function"), dict) else {}
                    name = fn.get("name") or tc.get("name") or "unknown"
                    calls[tc.get("id")] = name
                    yield Event(fallback_ts, NAME, session_id, project, "assistant",
                                "tool_call", "", tool_name=name, ts_exact=False)
            if text:
                yield Event(fallback_ts, NAME, session_id, project, "assistant",
                            "reply", text, ts_exact=False)
        elif t == "reasoning":
            # `encrypted_content` -- no plaintext to mine.
            yield Event(fallback_ts, NAME, session_id, project, "assistant",
                        "meta", ts_exact=False)
        elif t == "tool_result":
            name = calls.get(d.get("tool_call_id"))
            yield Event(fallback_ts, NAME, session_id, project, "tool",
                        "tool_result", text, tool_name=name, ts_exact=False)
        elif t == "backend_tool_call":
            # Server-side tool (web_search etc). The query is user-relevant text.
            kind_obj = d.get("kind") if isinstance(d.get("kind"), dict) else {}
            name = kind_obj.get("tool_type") or "backend_tool"
            action = kind_obj.get("action") if isinstance(kind_obj.get("action"), dict) else {}
            query = action.get("query") if isinstance(action.get("query"), str) else ""
            yield Event(fallback_ts, NAME, session_id, project, "assistant",
                        "tool_call", query, tool_name=name, ts_exact=False)
        elif t == "system":
            yield Event(fallback_ts, NAME, session_id, project, "system", "meta",
                        ts_exact=False)
        elif t:
            report.unknown(NAME, str(t))
