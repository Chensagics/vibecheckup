"""Cursor: <app-data>/Cursor/User/globalStorage/state.vscdb (SQLite key/value)

Cursor keeps every chat in ONE global SQLite database, not a file per session.
Two tables: `ItemTable` (workbench settings) and `cursorDiskKV`, which is a
plain key -> JSON-text store. Three key prefixes matter:

    composerData:<composerId>            the conversation (a "composer")
    bubbleId:<composerId>:<bubbleId>     one message
    checkpointId / codeBlockDiff / ...   editor state, no authored text

`composerData.conversation` is an empty list in every record here -- the
messages were moved out to their own rows -- so the order comes from
`fullConversationHeadersOnly`, a list of {bubbleId, type} in rendered order.
It disagreed with a createdAt sort in 3 of 7 local conversations, so the header
list wins and only bubbles missing from it fall back to createdAt.

Bubble `type` is the record type: 1 = user, 2 = assistant. The header list
repeats the type per bubble and agreed with the stored bubble on all 399 here,
which is what makes this mapping `exact` rather than empirical -- contrast
antigravity, whose step types had to be read off their payloads.

An assistant bubble is sub-classified by `capabilityType`, and the three values
are disjoint, each owning a different payload field (counts from this machine):

    absent -> `text`                prose reply            (88)
    15     -> `toolFormerData`      tool call + result    (189)
    30     -> `thinking.text`       reasoning summary      (94)

NOT recovered, and why:

  * `usageData` is `{}` on every composer and `tokenCount` is
    {inputTokens: 0, outputTokens: 0} on all but 19 bubbles, so spend covers a
    fraction of the traffic rather than all of it.
  * Cursor reports one input figure with no cached subset, so cache_read and
    cache_write are always 0 here -- not zero-cost, just not broken out.
  * The workspace `aiService.prompts` lists are a verbatim duplicate of the
    type-1 bubbles with no timestamps, so they are deliberately skipped; taking
    both would double every prompt.
  * `agentKv:` holds the assembled request payloads (system prompt, injected
    rules, tool schemas) -- and a quarter of them are binary blobs. None of it
    is authored by either side, so none of it is read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse

from .base import (OBSERVED, UNKNOWN, Event, iso, mtime_iso, normalize_project,
                   project_from_cwd, project_from_encoded_dir, usage)

NAME = "cursor"

# Cursor is an Electron app, so the store sits wherever the platform puts
# application data. Every root that exists is read; a machine normally has one.
ROOTS = (
    "~/Library/Application Support/Cursor/User",   # macOS
    "~/.config/Cursor/User",                       # Linux
    "~/AppData/Roaming/Cursor/User",               # Windows
)

# type -> role. See the module docstring for how this was confirmed.
BUBBLE_ROLES = {1: "user", 2: "assistant"}

# capabilityType -> kind, for an assistant bubble.
CAPABILITIES = {
    None: "reply",
    15: "tool_call",
    30: "thinking",
}

# Tool arguments that name the file or directory a call touched. Same idea as
# claude_code's file_path/path: the text of a tool_call event is the path, so
# extensions can be mined from it.
PATH_ARGS = ("target_file", "file_path", "target_directory", "path")

# "default" is Cursor's Auto setting, not a model. Emitting it would put a UI
# placeholder in the unpriced-model report, where it would look like a price
# that needs adding rather than a model Cursor declined to name.
PLACEHOLDER_MODELS = {"default", "auto"}


def discover():
    """The global store for each Cursor installation on this machine."""
    out = []
    for root in ROOTS:
        db = os.path.join(os.path.expanduser(root), "globalStorage", "state.vscdb")
        if os.path.exists(db):
            out.append(db)
    return sorted(out)


def _open(path, probe):
    """Open read-only. `immutable=1` is faster but makes SQLite ignore the
    write-ahead log, which for a live-written DB looks like corruption -- and
    Cursor is very likely running while this reads. Fall back to a plain
    read-only handle that honours the WAL. Never opened writable.
    """
    for uri in (f"file:{path}?mode=ro&immutable=1", f"file:{path}?mode=ro"):
        con = None
        try:
            con = sqlite3.connect(uri, uri=True)
            con.execute(probe).fetchone()
            return con
        except sqlite3.Error:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    return None


def _load(value):
    """A cursorDiskKV value as a dict, or None.

    Values are usually JSON text, but the store also holds NULLs and binary
    blobs (every `agentKv:` row is bytes, a quarter of them undecodable), so
    this never assumes it was handed a string.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        obj = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _nested(raw):
    """Both halves of a tool call -- `rawArgs` and `result` -- are JSON encoded
    a second time, as a string inside the bubble's own JSON."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


# --- project labels -------------------------------------------------------
#
# A composer is not stored with a project, so the folder has to be recovered.
# Four independent signals exist and they agreed on all 7 local conversations;
# they are tried best-first because they degrade differently:
#
#   1. composer workspaceIdentifier.uri.fsPath   an absolute path, verbatim
#   2. bubble workspaceUris[0]                   a file:// URI, written per turn
#   3. workspaceStorage join                     <hash>/workspace.json `folder`,
#                                                reached by finding the composer
#                                                id in that workspace's own
#                                                `composer.composerData`
#   4. bubble workspaceProjectDir                ~/.cursor/projects/<dash-encoded
#                                                cwd>, lossy like Claude Code's
#
# The workspace hash is NEVER a fallback. It is an opaque local id that names
# nothing, and publishing it as a project would put a meaningless key in
# stats.json while claiming the folder was resolved.


def _uri_path(uri):
    """`file:///Users/a/My%20Repo` -> `/Users/a/My Repo`, or None."""
    if not isinstance(uri, str) or not uri:
        return None
    if uri.startswith("file://"):
        got = urllib.parse.unquote(urllib.parse.urlsplit(uri).path)
        return got or None
    return uri if uri.startswith("/") else None


def _label(cwd):
    """Project label for a recovered absolute path.

    Routed through project_from_cwd (worktree folding, tmp and home rules) and
    then normalize_project, so a Cursor label is repaired the same way a label
    read back out of an older events.ndjson is. Real directories are handed to
    OBSERVED so ingest can read their git remotes for the redaction list.
    """
    if not cwd:
        return UNKNOWN
    try:
        if os.path.isdir(cwd):
            OBSERVED.directory(cwd)
    except (OSError, ValueError):
        pass
    return normalize_project(project_from_cwd(cwd))


def _workspace_folders(db_path):
    """{composerId: folder path} from the per-workspace databases.

    Sibling of globalStorage: workspaceStorage/<hash>/ holds a workspace.json
    naming the folder and a state.vscdb whose `composer.composerData` lists the
    composers opened in it. That pairing is the only link from a conversation to
    a project when the composer and its bubbles carry no path of their own.

    Best-effort: a missing, locked or malformed workspace DB just contributes
    nothing, because signals 1, 2 and 4 are still available.
    """
    root = os.path.join(os.path.dirname(os.path.dirname(db_path)),
                        "workspaceStorage")
    out = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for name in names:
        wdir = os.path.join(root, name)
        try:
            with open(os.path.join(wdir, "workspace.json"), "r",
                      encoding="utf-8", errors="replace") as fh:
                folder = _uri_path((json.load(fh) or {}).get("folder"))
        except (OSError, json.JSONDecodeError, ValueError, AttributeError):
            continue
        if not folder:
            continue
        con = _open(os.path.join(wdir, "state.vscdb"),
                    "select count(*) from ItemTable")
        if con is None:
            continue
        try:
            row = con.execute(
                "select value from ItemTable where key='composer.composerData'"
            ).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            con.close()
        data = _load(row[0]) if row else None
        for c in ((data or {}).get("allComposers") or []):
            if isinstance(c, dict) and isinstance(c.get("composerId"), str):
                out.setdefault(c["composerId"], folder)
    return out


def _project(cid, composer, bubbles, folders):
    """Project label for one composer. See the block comment above for order."""
    ident = composer.get("workspaceIdentifier")
    if isinstance(ident, dict):
        uri = ident.get("uri")
        if isinstance(uri, dict):
            got = _uri_path(uri.get("fsPath") or uri.get("path")
                            or uri.get("external"))
            if got:
                return _label(got)

    for b in bubbles:
        for u in (b.get("workspaceUris") or []):
            got = _uri_path(u)
            if got:
                return _label(got)

    if folders.get(cid):
        return _label(folders[cid])

    for b in bubbles:
        pdir = b.get("workspaceProjectDir")
        if isinstance(pdir, str) and pdir:
            # ~/.cursor/projects/<dash-encoded cwd>: the same lossy encoding
            # Claude Code uses for its own directory names, so the same
            # filesystem-backed decoder resolves it.
            return normalize_project(
                project_from_encoded_dir(os.path.basename(pdir.rstrip("/"))))

    return UNKNOWN


# --- messages -------------------------------------------------------------


def _model(bubble, composer):
    """The model that produced a bubble, or None if Cursor did not name one."""
    for src in (bubble.get("modelInfo"), composer.get("modelConfig")):
        if isinstance(src, dict):
            name = src.get("modelName")
            if isinstance(name, str) and name.strip():
                if name.strip().lower() in PLACEHOLDER_MODELS:
                    return None
                return name
    return None


def _usage(bubble):
    """Normalized usage for one bubble, or None.

    `tokenCount` is present on every bubble and zero on most of them; usage()
    turns the all-zero case into None so it never reaches the spend report.
    Cursor gives one undifferentiated input figure -- no cached subset is
    broken out -- so cache_read and cache_write stay 0.
    """
    tc = bubble.get("tokenCount")
    if not isinstance(tc, dict):
        return None
    return usage(input=tc.get("inputTokens"), output=tc.get("outputTokens"))


def _order(composer, bubbles):
    """Bubble ids in conversation order.

    `fullConversationHeadersOnly` is what Cursor renders, so it is the order.
    Anything it does not mention -- 3 bubbles of 399 here, mid-stream rows the
    header list never picked up -- is appended by createdAt rather than dropped.
    """
    ordered, seen = [], set()
    for h in (composer.get("fullConversationHeadersOnly") or []):
        if not isinstance(h, dict):
            continue
        bid = h.get("bubbleId")
        if bid in bubbles and bid not in seen:
            seen.add(bid)
            ordered.append(bid)
    rest = [b for b in bubbles if b not in seen]
    rest.sort(key=lambda b: (bubbles[b].get("createdAt") or "", b))
    return ordered + rest


def _tool_events(ts, sid, project, bubble):
    """The call and its result for one capabilityType-15 bubble.

    Cursor stores both halves in one row: the arguments in `rawArgs` and the
    outcome in `result`/`error`, so a tool call yields two events the way it
    does everywhere else.
    """
    tf = bubble.get("toolFormerData")
    if not isinstance(tf, dict):
        return
    name = tf.get("name")
    if not isinstance(name, str) or not name:
        # A nameless {"additionalData": {...}} stub. It carries no call.
        return
    args = _nested(tf.get("rawArgs"))
    cmd = args.get("command") if isinstance(args.get("command"), str) else None
    text = ""
    for key in PATH_ARGS:
        if isinstance(args.get(key), str) and args[key]:
            text = args[key]
            break
    yield Event(ts, NAME, sid, project, "assistant", "tool_call", text,
                tool_name=name, cmd=cmd)

    err = tf.get("error")
    result = tf.get("result")
    if isinstance(err, dict) and err:
        # modelVisibleErrorMessage is the sentence the agent actually read, so
        # it is the one an error signature should be mined out of.
        msg = err.get("modelVisibleErrorMessage") or err.get(
            "clientVisibleErrorMessage") or json.dumps(err, ensure_ascii=False)
        yield Event(ts, NAME, sid, project, "tool", "error",
                    msg if isinstance(msg, str) else "", tool_name=name)
        return
    if not isinstance(result, str) or not result:
        return
    exit_code = None
    got = _nested(result)
    for key in ("exitCodeV2", "exitCode", "exit_code"):
        if isinstance(got.get(key), int):
            exit_code = got[key]
            break
    yield Event(ts, NAME, sid, project, "tool",
                "error" if tf.get("status") == "error" else "tool_result",
                result, tool_name=name, exit_code=exit_code)


def _bubble_events(ts, ts_exact, sid, project, composer, bubble, report):
    """Every event one bubble carries, in order."""
    role = BUBBLE_ROLES.get(bubble.get("type"))
    if role is None:
        report.unknown(NAME, f"type/{bubble.get('type')}")
        return
    text = bubble.get("text")
    text = text if isinstance(text, str) else ""

    if role == "user":
        if text.strip():
            yield Event(ts, NAME, sid, project, "user", "prompt", text,
                        ts_exact=ts_exact)
        return

    cap = bubble.get("capabilityType")
    kind = CAPABILITIES.get(cap)
    if kind is None:
        # A capability Cursor added since this was written. Reported by value,
        # and read as a reply so its prose is not silently dropped.
        report.unknown(NAME, f"capabilityType/{cap}")
        kind = "reply"

    if kind == "tool_call":
        for ev in _tool_events(ts, sid, project, bubble):
            ev.ts_exact = ts_exact
            yield ev
        return

    if kind == "thinking":
        think = bubble.get("thinking")
        body = think.get("text") if isinstance(think, dict) else think
        if isinstance(body, str) and body.strip():
            yield Event(ts, NAME, sid, project, "assistant", "thinking", body,
                        ts_exact=ts_exact)
        return

    if text.strip():
        yield Event(ts, NAME, sid, project, "assistant", "reply", text,
                    ts_exact=ts_exact)


def _conversation(cid, composers, turns, folders, fallback_ts, report):
    """Every event of one composer, in conversation order."""
    # A conversation whose composerData was pruned still has its messages, and
    # they are the part worth reading. Only the header ordering and the
    # composer-level model are lost, and both have fallbacks.
    composer = composers.get(cid)
    if composer is None:
        report.unknown(NAME, "composerData/missing")
        composer = {}
    project = _project(cid, composer, list(turns.values()), folders)
    started = iso(composer.get("createdAt")) or fallback_ts

    for bid in _order(composer, turns):
        bubble = turns[bid]
        ts = iso(bubble.get("createdAt"))
        ts_exact = ts is not None
        ts = ts or started
        out = list(_bubble_events(ts, ts_exact, cid, project, composer,
                                  bubble, report))
        u = _usage(bubble)
        if u is not None:
            model = _model(bubble, composer)
            # One usage figure per bubble: attach it once, never per event.
            if out:
                out[0].model, out[0].usage = model, u
            else:
                out.append(Event(ts, NAME, cid, project, "system", "meta",
                                 model=model, usage=u, ts_exact=ts_exact))
        yield from out


def iter_events(path, report):
    con = _open(path, "select count(*) from cursorDiskKV")
    if con is None:
        report.bad_file(NAME)
        return
    folders = _workspace_folders(path)
    fallback_ts = mtime_iso(path)
    try:
        try:
            composers = {}
            for key, value in con.execute(
                    "select key, value from cursorDiskKV "
                    "where key like 'composerData:%'"):
                cid = key.split(":", 1)[1] if ":" in key else ""
                data = _load(value)
                if data is None:
                    report.bad_line(NAME)
                    continue
                if cid:
                    composers[cid] = data

            # One conversation is held at a time rather than the whole store:
            # the bubbles are the part that grows with use (4 MB of the 11 MB
            # here), and `bubbleId:<composerId>:<bubbleId>` sorts into composer
            # groups, so ordering by key is enough to stream them.
            cur, turns = None, {}
            for key, value in con.execute(
                    "select key, value from cursorDiskKV "
                    "where key like 'bubbleId:%' order by key"):
                parts = key.split(":")
                if len(parts) < 3:
                    report.bad_line(NAME)
                    continue
                data = _load(value)
                if data is None:
                    report.bad_line(NAME)
                    continue
                if parts[1] != cur:
                    if turns:
                        yield from _conversation(cur, composers, turns, folders,
                                                 fallback_ts, report)
                    cur, turns = parts[1], {}
                turns[parts[2]] = data
        except sqlite3.Error:
            report.bad_file(NAME)
            return
    finally:
        con.close()
    if turns:
        yield from _conversation(cur, composers, turns, folders, fallback_ts,
                                 report)
