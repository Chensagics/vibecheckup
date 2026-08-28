"""Claude Code: ~/.claude/projects/<dash-encoded-cwd>/<session-uuid>.jsonl"""
from __future__ import annotations

import json
import os
from glob import glob

from .base import (Event, argv_head, blocks_text, iso, project_from_encoded_dir,
                   read_jsonl, usage)

NAME = "claude_code"
ROOT = os.path.expanduser("~/.claude/projects")

# Allowlist, not denylist: Claude Code adds housekeeping record types over
# time, so anything outside this set is ignored and reported rather than
# chased individually.
TEXT_TYPES = {"user", "assistant"}
# Injected context: hook output, SessionStart payloads, CLAUDE.md, etc.
META_TYPES = {"attachment", "system"}

# Tools whose input carries a shell command worth counting.
CMD_TOOLS = {"Bash", "BashOutput"}


def _usage(msg):
    """(model, usage) for one assistant record, or (None, None).

    `input_tokens` here already EXCLUDES both cache figures -- they are three
    disjoint buckets billed at three different rates, so they stay separate.
    """
    u = msg.get("usage")
    if not isinstance(u, dict):
        return None, None
    got = usage(input=u.get("input_tokens"), output=u.get("output_tokens"),
                cache_read=u.get("cache_read_input_tokens"),
                cache_write=u.get("cache_creation_input_tokens"))
    if got is None:
        return None, None
    model = msg.get("model")
    return (model if isinstance(model, str) and model else None), got


def discover():
    """Top-level session transcripts plus nested subagent transcripts:
    <enc-cwd>/<uuid>.jsonl and <enc-cwd>/<uuid>/subagents/agent-*.jsonl
    """
    return sorted(glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True))


def iter_events(path, report):
    session_id = os.path.splitext(os.path.basename(path))[0]
    rel = os.path.relpath(path, ROOT).split(os.sep)
    project = project_from_encoded_dir(rel[0])
    is_subagent = "subagents" in rel
    tool_names = {}  # tool_use_id -> tool name, to label results
    # One API response is written as one record PER CONTENT BLOCK, and every
    # one of those records repeats the whole message.usage. Measured on this
    # machine: 53,667 usage records for 26,133 real responses -- billing the
    # raw records would roughly double the Claude Code line. Ids never repeat
    # across files (verified over the full corpus), so a per-file set is enough.
    billed = set()

    for lineno, d in read_jsonl(path):
        if d is None:
            report.bad_line(NAME)
            continue
        t = d.get("type")
        ts = iso(d.get("timestamp"))
        sid = d.get("sessionId") or session_id
        if is_subagent:
            # Keep subagent turns distinct from the parent session's turns.
            sid = session_id

        if t in META_TYPES:
            yield Event(ts, NAME, sid, project, "system", "meta")
            continue

        if t not in TEXT_TYPES:
            report.ignored(NAME, str(t))
            continue

        msg = d.get("message") or {}
        content = msg.get("content")

        model, u = _usage(msg)
        if u is not None:
            key = msg.get("id") or d.get("requestId")
            if key is not None:
                if key in billed:
                    model, u = None, None
                else:
                    billed.add(key)

        if isinstance(content, str):
            if t == "user":
                yield Event(ts, NAME, sid, project, "user", "prompt", content,
                            model=model, usage=u)
            else:
                yield Event(ts, NAME, sid, project, "assistant", "reply", content,
                            model=model, usage=u)
            continue

        if not isinstance(content, list):
            if u is not None:
                # Usage with no content to hang it on: keep the tokens.
                yield Event(ts, NAME, sid, project, "system", "meta",
                            model=model, usage=u)
            continue

        out = []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                role = "user" if t == "user" else "assistant"
                kind = "prompt" if t == "user" else "reply"
                out.append(Event(ts, NAME, sid, project, role, kind,
                                 b.get("text") or ""))
            elif bt == "thinking":
                out.append(Event(ts, NAME, sid, project, "assistant", "thinking",
                                 b.get("thinking") or b.get("text") or ""))
            elif bt == "tool_use":
                name = b.get("name") or "unknown"
                tool_names[b.get("id")] = name
                inp = b.get("input") or {}
                cmd = inp.get("command") if isinstance(inp, dict) else None
                if not isinstance(cmd, str):
                    cmd = None
                text = ""
                if isinstance(inp, dict):
                    # File-touching tools: keep the path so extensions can be mined.
                    text = inp.get("file_path") or inp.get("path") or ""
                out.append(Event(ts, NAME, sid, project, "assistant", "tool_call",
                                 text, tool_name=name, cmd=cmd))
            elif bt == "tool_result":
                name = tool_names.get(b.get("tool_use_id"))
                is_err = bool(b.get("is_error"))
                tur = d.get("toolUseResult")
                exit_code = None
                if isinstance(tur, dict):
                    for k in ("exitCode", "exit_code", "returnCode"):
                        if isinstance(tur.get(k), int):
                            exit_code = tur[k]
                            break
                    if exit_code is None and tur.get("success") is False:
                        is_err = True
                out.append(Event(ts, NAME, sid, project, "tool",
                                 "error" if is_err else "tool_result",
                                 blocks_text(b.get("content")), tool_name=name,
                                 exit_code=exit_code))

        if u is not None:
            if out:
                out[0].model, out[0].usage = model, u
            else:
                out.append(Event(ts, NAME, sid, project, "system", "meta",
                                 model=model, usage=u))
        yield from out
