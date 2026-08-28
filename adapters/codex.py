"""Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (+ archived_sessions)"""
from __future__ import annotations

import json
import os
from glob import glob

from .base import Event, blocks_text, iso, project_from_cwd, read_jsonl, usage

NAME = "codex"
ROOTS = [os.path.expanduser("~/.codex/sessions"),
         os.path.expanduser("~/.codex/archived_sessions")]

SKIP_EVENTS = {
    "task_started", "task_complete", "turn_aborted", "context_compacted",
    "agent_reasoning", "agent_reasoning_delta", "agent_message_delta",
    "exec_command_begin", "exec_command_end", "patch_apply_begin",
    "stream_error", "notification", "background_event",
    "thread_settings_applied", "web_search_end", "image_generation_end",
    "sub_agent_activity", "guardian_assessment", "view_image_tool_call",
    "mcp_tool_call_begin", "turn_diff", "entered_review_mode",
    "exited_review_mode", "plan_update", "todo_list",
    "error", "thread_name_updated", "thread_rolled_back", "thread_forked",
}


def discover():
    out = []
    for root in ROOTS:
        out += glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    return sorted(set(out))


MODEL_RECORDS = ("session_meta", "turn_context")


def first_model(path):
    """The model id in effect before the first turn, or None.

    Codex records the model on `turn_context`, not on `session_meta` (which
    only carries `model_provider`). turn_context usually lands around line 7,
    but token_count events can precede it, so scan ahead for it rather than
    attributing early turns to an unknown model. The `"model"` substring test
    keeps this to ~0.2s over 386 rollouts.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"model"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if d.get("type") in MODEL_RECORDS:
                    m = (d.get("payload") or {}).get("model")
                    if isinstance(m, str) and m:
                        return m
    except OSError:
        return None
    return None


def _counts(d):
    """One token_count figure, normalized.

    `cached_input_tokens` is a subset of `input_tokens` and
    `reasoning_output_tokens` a subset of `output_tokens` -- verified on real
    rollouts, where `total_tokens == input_tokens + output_tokens` exactly.
    Adding either subset back in would double-count it.
    """
    if not isinstance(d, dict):
        d = {}

    def g(k):
        v = d.get(k)
        return int(v) if isinstance(v, (int, float)) else 0

    inp, cached, out = g("input_tokens"), g("cached_input_tokens"), g("output_tokens")
    return {"input": inp, "cached": min(cached, inp), "output": out,
            "total": g("total_tokens") or (inp + out)}


def _delta(cur, prev):
    """Per-turn usage from two cumulative snapshots.

    THE TRAP: `token_count` is re-emitted on idle and after a turn ends, and
    `total_token_usage` is cumulative for the whole session. On a long
    multi-hour rollout, summing every `last_token_usage` overcounted input
    tokens by roughly a third from repeats alone -- and the error grows with
    session length. Differencing the cumulative counter telescopes back to the
    final total no matter how often the event repeats (a repeat differences
    to zero), which is why this is the primitive rather than last_token_usage.
    """
    if prev is None or cur["total"] < prev["total"]:
        return cur  # first sample, or the session counter restarted
    return {k: max(cur[k] - prev[k], 0) for k in cur}


def _output_text(o):
    if isinstance(o, str):
        return o
    return blocks_text(o)


def _cmd_from_arguments(name, arguments):
    """Codex passes shell invocations as a JSON string of arguments."""
    if not isinstance(arguments, str):
        return None
    try:
        a = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(a, dict):
        return None
    c = a.get("command")
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    if isinstance(c, str):
        # Unwrap `bash -lc "<real command>"`
        parts = c.split(None, 2)
        if len(parts) == 3 and parts[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh") \
                and parts[1].startswith("-"):
            return parts[2].strip("\"'")
        return c
    return None


def iter_events(path, report):
    session_id = os.path.splitext(os.path.basename(path))[0]
    project = "unknown"
    calls = {}  # call_id -> tool name
    model = first_model(path)
    prev_total = None

    for lineno, d in read_jsonl(path):
        if d is None:
            report.bad_line(NAME)
            continue
        ts = iso(d.get("timestamp"))
        t = d.get("type")
        p = d.get("payload") or {}
        pt = p.get("type")

        if t == "session_meta":
            session_id = p.get("id") or session_id
            project = project_from_cwd(p.get("cwd"))
            if isinstance(p.get("model"), str) and p["model"]:
                model = p["model"]
            continue
        if t == "turn_context":
            if p.get("cwd"):
                project = project_from_cwd(p.get("cwd"))
            # The model can change mid-session; later turns bill at the new one.
            if isinstance(p.get("model"), str) and p["model"]:
                model = p["model"]
            continue

        if t == "event_msg":
            if pt == "user_message":
                # The clean, user-typed prompt. Preferred over the response_item
                # copy below, which is the same turn plus injected context.
                yield Event(ts, NAME, session_id, project, "user", "prompt",
                            p.get("message") or "")
            elif pt == "token_count":
                info = p.get("info") or {}
                cum = info.get("total_token_usage")
                if isinstance(cum, dict):
                    cur = _counts(cum)
                    d_ = _delta(cur, prev_total)
                    prev_total = cur
                else:
                    # Pre-cumulative rollouts carry only the per-turn figure.
                    d_ = _counts(info.get("last_token_usage"))
                u = usage(input=d_["input"] - d_["cached"],
                          cache_read=d_["cached"], output=d_["output"])
                yield Event(ts, NAME, session_id, project, "system", "meta",
                            tokens=d_["total"] or None,
                            model=model if u else None, usage=u)
            elif pt == "item_completed":
                # Plans appear only here; other item types duplicate the
                # response_item stream and would double-count.
                item = p.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "Plan" \
                        and isinstance(item.get("text"), str):
                    yield Event(ts, NAME, session_id, project, "assistant",
                                "reply", item["text"])
            elif pt in ("agent_message", "mcp_tool_call_end", "patch_apply_end"):
                pass  # duplicated by the response_item stream
            elif pt not in SKIP_EVENTS:
                report.unknown(NAME, f"event_msg/{pt}")
            continue

        if t == "world_state":
            # Carries the live environment, including cwd.
            envs = ((p.get("state") or {}).get("environments") or {}).get("environments") or {}
            for env in envs.values():
                if isinstance(env, dict) and env.get("cwd"):
                    project = project_from_cwd(env["cwd"])
                    break
            continue
        if t in ("compacted", "inter_agent_communication_metadata"):
            continue

        if t != "response_item":
            if t:
                report.unknown(NAME, str(t))
            continue

        if pt == "message":
            role = p.get("role")
            text = blocks_text(p.get("content"))
            if role == "assistant":
                yield Event(ts, NAME, session_id, project, "assistant", "reply", text)
            elif role == "developer" or role == "system":
                yield Event(ts, NAME, session_id, project, "system", "meta")
            # role == "user" is intentionally dropped: it duplicates
            # event_msg/user_message and carries injected context.
        elif pt == "reasoning":
            # Usually `encrypted_content` with an empty summary -- no text to mine.
            summ = p.get("summary")
            text = blocks_text(summ) if summ else ""
            if text:
                yield Event(ts, NAME, session_id, project, "assistant", "thinking", text)
        elif pt in ("function_call", "custom_tool_call", "local_shell_call"):
            name = p.get("name") or "unknown"
            calls[p.get("call_id")] = name
            cmd = _cmd_from_arguments(name, p.get("arguments"))
            if cmd is None and isinstance(p.get("input"), str):
                cmd = None
            yield Event(ts, NAME, session_id, project, "assistant", "tool_call",
                        "", tool_name=name, cmd=cmd)
        elif pt in ("function_call_output", "custom_tool_call_output",
                    "local_shell_call_output"):
            name = calls.get(p.get("call_id"))
            text = _output_text(p.get("output"))
            low = text[:400].lower()
            is_err = ("error" in low and "exit" in low) or low.startswith("error")
            yield Event(ts, NAME, session_id, project, "tool",
                        "error" if is_err else "tool_result", text, tool_name=name)
        elif pt == "agent_message":
            yield Event(ts, NAME, session_id, project, "assistant", "reply",
                        p.get("message") or blocks_text(p.get("content")))
        elif pt == "ghost_snapshot":
            continue
        elif pt in ("web_search_call", "tool_search_call", "tool_search_output",
                    "image_generation_call"):
            yield Event(ts, NAME, session_id, project, "assistant", "tool_call",
                        "", tool_name=pt)
        elif pt:
            report.unknown(NAME, f"response_item/{pt}")
