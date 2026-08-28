"""Gemini CLI: ~/.gemini/tmp/<project-hash>/chats/session-*.json (plain JSON)"""
from __future__ import annotations

import json
import os
from glob import glob

from .base import Event, argv_head, iso, project_from_cwd, usage

NAME = "gemini_cli"
ROOT = os.path.expanduser("~/.gemini/tmp")


def discover():
    return sorted(glob(os.path.join(ROOT, "*", "chats", "*.json")))


def _project_label(path, project_hash):
    """The hash is opaque; recover a name from a sibling file if one exists."""
    base = os.path.dirname(os.path.dirname(path))
    for cand in ("cwd", "project.json", "metadata.json"):
        fp = os.path.join(base, cand)
        if os.path.exists(fp):
            try:
                raw = open(fp, encoding="utf-8", errors="replace").read().strip()
                try:
                    obj = json.loads(raw)
                    raw = obj.get("cwd") or obj.get("path") or raw
                except (json.JSONDecodeError, ValueError):
                    pass
                if raw.startswith("/"):
                    # Same normalization as every other adapter: a recovered
                    # cwd can be a worktree or a temp dir just as easily.
                    return project_from_cwd(raw)
            except OSError:
                pass
    return f"gemini:{(project_hash or '')[:8]}"


def _usage(m):
    """(model, usage) for one assistant message, or (None, None).

    Verified across every local session: `total == input + output + thoughts`,
    so `cached` is a subset of `input` (moved to cache_read, billed at the
    cached-input rate) while `thoughts` sits OUTSIDE `output` and has to be
    added to it -- Google bills thinking tokens as output. `tool` is
    prompt-side and folds into input; it was 0 in every record here.
    """
    tk = m.get("tokens")
    if not isinstance(tk, dict):
        return None, None

    def g(k):
        v = tk.get(k)
        return int(v) if isinstance(v, (int, float)) and v > 0 else 0

    inp, cached = g("input") + g("tool"), g("cached")
    cached = min(cached, inp)
    got = usage(input=inp - cached, cache_read=cached,
                output=g("output") + g("thoughts"))
    if got is None:
        return None, None
    model = m.get("model")
    return (model if isinstance(model, str) and model else None), got


def iter_events(path, report):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            doc = json.load(fh)
    except (json.JSONDecodeError, ValueError, OSError):
        report.bad_file(NAME)
        return
    if not isinstance(doc, dict):
        report.bad_file(NAME)
        return

    sid = doc.get("sessionId") or os.path.splitext(os.path.basename(path))[0]
    project = _project_label(path, doc.get("projectHash"))
    start = iso(doc.get("startTime"))

    for m in doc.get("messages") or []:
        if not isinstance(m, dict):
            continue
        ts = iso(m.get("timestamp")) or start
        mt = m.get("type")
        text = m.get("content") or ""

        if mt == "user":
            yield Event(ts, NAME, sid, project, "user", "prompt", text)
        elif mt in ("gemini", "assistant", "model"):
            model, u = _usage(m)
            out = []
            if text:
                out.append(Event(ts, NAME, sid, project, "assistant", "reply", text))
            for tc in (m.get("toolCalls") or []):
                if not isinstance(tc, dict):
                    continue
                name = tc.get("name") or "unknown"
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                cmd = args.get("command") if isinstance(args.get("command"), str) else None
                fpath = args.get("file_path") or args.get("path") or ""
                out.append(Event(ts, NAME, sid, project, "assistant", "tool_call",
                                 fpath if isinstance(fpath, str) else "",
                                 tool_name=name, cmd=cmd))
                res = tc.get("result")
                if res is not None:
                    from .base import blocks_text
                    out.append(Event(ts, NAME, sid, project, "tool", "tool_result",
                                     blocks_text(res) if not isinstance(res, str) else res,
                                     tool_name=name))
            if u is not None:
                # One usage figure per message: attach it once, never per block.
                if out:
                    out[0].model, out[0].usage = model, u
                else:
                    out.append(Event(ts, NAME, sid, project, "system", "meta",
                                     model=model, usage=u))
            yield from out
        elif mt:
            report.unknown(NAME, str(mt))
