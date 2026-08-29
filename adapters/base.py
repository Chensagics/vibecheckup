"""Normalized event model shared by every session-log adapter.

Adapters classify: they map a source record onto a (role, kind) pair and skip
records that carry no text at all. They never judge text by its content --
every content-based decision lives in wcstats/clean.py.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
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


# No slots=True: that argument arrived in 3.10, and macOS ships 3.9.6 with the
# Command Line Tools -- which is exactly the Python vibecheckup.sh tells people to
# install. The memory it saved is not worth failing at import on a stock Mac.
@dataclass
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


# --- project labels -------------------------------------------------------
#
# Every adapter funnels through project_from_cwd(): a session's project is the
# directory it ran in. Three kinds of directory are NOT a project, and each one
# used to both leak and miscount:
#
#   worktree  <repo>/.claude/worktrees/<branch> and its equivalents. The leaf
#             is a BRANCH name -- unshipped features, live A/B arms -- so using
#             it publishes the name verbatim AND splits one repo into a dozen
#             fake projects. Attributed to <repo>; the branch is dropped, since
#             anything kept here becomes a facet key and reaches stats.json.
#             `git worktree add ../name` parks the same thing NEXT TO the repo,
#             where the path carries no marker at all -- see
#             _sibling_worktree_repo() for the one rule that needs the disk.
#   temp dir  /tmp, /var/tmp, $TMPDIR. Labelled "tmp".
#   home      the user's home directory (labelling it leaks the username) and
#             "/". Labelled "~" and "unknown".
UNKNOWN = "unknown"
TMP_LABEL = "tmp"
HOME_LABEL = "~"

# Directory names under which a tool pools git worktrees or agent workspaces.
# Derived from the local corpus, where four conventions occur:
#   <repo>/.claude/worktrees/<branch>        Claude Code
#   <repo>/.worktrees/<branch>               plain git
#   ~/.codex/worktrees/<id>/<repo>           Codex
#   ~/conductor/workspaces/<repo>/<name>     Conductor
WORKTREE_DIRS = ("worktrees", "workspaces")

# A worktree pool keyed by an opaque id ("~/.codex/worktrees/1860/finn") has
# the repo one level further down. Hex-with-a-digit, or all digits: enough to
# skip ids without swallowing a repo actually named "cafe" or "beta".
RE_OPAQUE_ID = re.compile(r"^(?:(?=.*\d)[0-9a-f]{4,}|\d+)$", re.I)

_TMP_ROOTS = ("/tmp", "/var/tmp", "/var/folders")


class Observed:
    """What ingest learns about this machine while resolving paths.

    Folding a worktree onto its repo means IDENTIFYING the branch name -- and
    a branch name is an unreleased feature or a live experiment arm, which is
    exactly the thing that must not be published. Dropping it from the label
    stopped it becoming a project; it did nothing about the same name written
    out in a sentence, which is where the clouds picked it up. So the names are
    kept here as they are recognised and handed to wcstats.clean, which masks
    them in prose too.

    Bounded on purpose: this is a redaction list, not an inventory, and a
    corpus with tens of thousands of paths must not grow it without limit.
    """

    LIMIT = 5000

    def __init__(self):
        self.branches = {}   # branch -> repo it belongs to ("" when unknown)
        self.dirs = set()    # real directories a session ran in

    def branch(self, name, repo=""):
        if not name or len(self.branches) >= self.LIMIT:
            return
        cur = self.branches.get(name)
        if cur is None or (not cur and repo):
            self.branches[name] = repo or ""

    def directory(self, path):
        if path and len(self.dirs) < self.LIMIT:
            self.dirs.add(path)

    def pairs(self):
        return sorted(self.branches.items())

    def clear(self):
        self.branches.clear()
        self.dirs.clear()


# Module-level because the adapters call project_from_cwd() from a dozen
# places and threading a collector through every one of them would touch every
# adapter for no gain. ingest.py reads it once at the end of the run.
OBSERVED = Observed()


def _depriv(path: str) -> str:
    """macOS reaches the same directory as /tmp and /private/tmp.

    Both spellings show up in real logs; without folding them together one
    directory becomes two projects.
    """
    if path.startswith("/private/"):
        return path[len("/private"):]
    return path


def _home() -> str:
    return _depriv((os.path.expanduser("~") or "").rstrip("/"))


def _is_home_path(parts) -> bool:
    """Do these path segments name somebody's home directory?

    Checked by shape -- /Users/<name>, /home/<name>, /root -- and not only
    against this machine's $HOME. A corpus read on a different machine, or one
    belonging to a different account, has to produce the same labels as it did
    where it was written; keying off the local $HOME made the answer depend on
    who was running the analysis.
    """
    if parts == ["root"]:
        return True
    if len(parts) == 2 and parts[0] in ("Users", "home"):
        return True
    return parts == [p for p in _home().split("/") if p]


def _is_temp(path: str) -> bool:
    roots = list(_TMP_ROOTS)
    try:
        roots.append(_depriv(tempfile.gettempdir().rstrip("/")))
    except (OSError, AttributeError):
        pass
    return any(r and (path == r or path.startswith(r + "/")) for r in roots)


def _repo_from_worktree(parts):
    """The repo a worktree path belongs to, or None if it is not one.

    Two shapes exist and they point in opposite directions:

    * Pooled inside the checkout -- ``<repo>/.claude/worktrees/<branch>``,
      ``<repo>/.worktrees/<branch>``. The pool always hides behind a dot
      directory, so the repo is the first ordinary directory above it.
    * Pooled in a tool's own home -- ``~/.codex/worktrees/<id>/<repo>``,
      ``~/conductor/workspaces/<repo>/<name>``. Nothing above the marker names
      a repo (walking up lands on the home directory), so the repo is the first
      segment *below* it that is not an opaque id.
    """
    for i in range(len(parts) - 1, -1, -1):
        if parts[i].lstrip(".").lower() not in WORKTREE_DIRS:
            continue
        hidden = parts[i].startswith(".")
        j = i - 1
        while j >= 0 and parts[j].startswith("."):
            hidden = True
            j -= 1
        if hidden and j >= 0 and not _is_home_path(parts[:j + 1]):
            # <repo>/.claude/worktrees/<branch>[/...]: the segment right below
            # the pool is the branch, and it is the string that must not be
            # published in prose either.
            if len(parts) > i + 1:
                OBSERVED.branch(parts[i + 1].lower(), parts[j].lower())
            return parts[j]
        for k in range(i + 1, len(parts)):
            seg = parts[k]
            if RE_OPAQUE_ID.match(seg):
                continue
            # ~/conductor/workspaces/<repo>/<name>: the workspace name sits
            # BELOW the repo. Recorded only for that exact shape -- in
            # ~/.codex/worktrees/<id>/<repo>/src the same position holds an
            # ordinary subdirectory, and calling `src` a branch would put a
            # real word on the redaction list.
            if (parts[i].lstrip(".").lower() == "workspaces"
                    and k + 2 == len(parts)):
                OBSERVED.branch(parts[k + 1].lower(), seg.lower())
            return seg
        return UNKNOWN
    return None


# --- linked worktrees parked outside any pool --------------------------------
#
# `git worktree add ../finn-loop-writer` is the ordinary way to make a
# worktree, and it puts the checkout beside the repo rather than in a pool. The
# path then has no `worktrees` segment for _repo_from_worktree() to see, so the
# leaf -- a loop name, a task number, a branch -- became a project of its own:
# one repo published as five, with the internal names attached.
#
# The only thing that distinguishes such a directory from an ordinary checkout
# is on disk. A linked worktree's `.git` is a regular FILE holding
# `gitdir: <repo>/.git/worktrees/<name>`, and that target is the same shape the
# pool rule already decodes -- so resolving it folds the label onto <repo> and
# registers <name> as an observed branch through exactly the same call.
#
# Cost: this is the only label rule that touches the filesystem, so it runs
# only after the pure path rule has declined, it stops at the first `.git` it
# finds, and every answer is memoised per directory. A corpus with a thousand
# sessions in one worktree stats it once.
RE_GITDIR = re.compile(r"^\s*gitdir:\s*(\S.*?)\s*$")
_SIBLING_WALK_UP = 6      # a session may start well inside the worktree
_SIBLING_CACHE_MAX = 20_000
_SIBLING_CACHE = {}       # directory -> repo label, or None
_MISS = object()


def clear_path_cache():
    """Forget what the disk said. For tests that build and tear down trees."""
    _SIBLING_CACHE.clear()


def _gitdir_target(dotgit: str) -> Optional[str]:
    """The path a linked worktree's `.git` FILE points at, or None."""
    try:
        with open(dotgit, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.readline()
    except OSError:
        return None
    m = RE_GITDIR.match(head or "")
    return m.group(1) if m else None


def _worktree_head_branch(admin: str) -> Optional[str]:
    """The branch a linked worktree has checked out, per its admin HEAD.

    The directory name and the branch are two different strings -- the
    worktrees of one repo here are called `finn-loop-writer` while sitting on
    `main_writer` -- and git prints the BRANCH into the hook and rebase
    failures that become error signatures. Both have to reach the redaction
    list or the fold hides the name in one place and publishes it in another.

    A detached HEAD names no branch, and nothing is invented for it.
    """
    try:
        with open(os.path.join(admin, "HEAD"), "r",
                  encoding="utf-8", errors="replace") as fh:
            head = fh.readline().strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):].strip() or None
    return None


def _linked_worktree_repo(wt_dir: str, dotgit: str) -> Optional[str]:
    """`<dir>/.git` is a file -> the repo it is a worktree of, or None."""
    target = _gitdir_target(dotgit)
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(wt_dir, target)
    admin = _depriv(os.path.normpath(target).rstrip("/"))
    parts = [p for p in admin.split("/") if p]
    # <repo>/.git/worktrees/<name> is a pooled path, so the pool decoder owns
    # it: same repo answer, same OBSERVED.branch(<name>, <repo>) registration.
    # Anything else -- a submodule's .git/modules/..., a relocated gitdir --
    # is not a worktree and gets no label of its own.
    if not parts or parts[-2:-1] != ["worktrees"]:
        return None
    repo = _repo_from_worktree(parts)
    if not repo or repo == UNKNOWN:
        return None
    branch = _worktree_head_branch(admin)
    if branch:
        OBSERVED.branch(branch.lower(), repo.lower())
    return repo


def _sibling_worktree_repo(path: str) -> Optional[str]:
    """Repo of the linked worktree `path` sits in, or None if it is not one."""
    got = _SIBLING_CACHE.get(path, _MISS)
    if got is not _MISS:
        return got
    repo = None
    cur = path
    for _ in range(_SIBLING_WALK_UP):
        dotgit = os.path.join(cur, ".git")
        try:
            mode = os.stat(dotgit).st_mode
        except (OSError, ValueError):
            mode = None
        if mode is not None:
            # A real .git directory is an ordinary checkout, and calling its
            # parent the project would relabel every subdirectory session in
            # the corpus. Either way the walk stops at the first repository.
            if not stat.S_ISDIR(mode):
                repo = _linked_worktree_repo(cur, dotgit)
            break
        nxt = os.path.dirname(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    if len(_SIBLING_CACHE) >= _SIBLING_CACHE_MAX:
        _SIBLING_CACHE.clear()
    _SIBLING_CACHE[path] = repo
    return repo


def project_from_cwd(cwd: Optional[str]) -> str:
    """Project label for the directory a session ran in.

    The label names the repository the work belongs to. It is never a worktree
    branch name, never a temp directory and never the user's home directory --
    see the block comment above for what each of those leaked.
    """
    if not cwd:
        return UNKNOWN
    path = _depriv(str(cwd).strip()).rstrip("/")
    if not path or path == "/":
        return UNKNOWN
    parts = [p for p in path.split("/") if p]
    if not parts:
        return UNKNOWN

    repo = _repo_from_worktree(parts)
    if repo is not None:
        return repo or UNKNOWN
    # Second, and the only rule that reads the disk: a linked worktree parked
    # outside a pool is invisible in the path. It sits with the pool rule and
    # ahead of the tmp and home rules on purpose -- a worktree is labelled by
    # its repository wherever it happens to be parked, and one repo checked out
    # under $TMPDIR is still that repo rather than "tmp".
    repo = _sibling_worktree_repo(path)
    if repo:
        return repo
    if _is_temp(path):
        return TMP_LABEL
    if path == _home():
        return HOME_LABEL
    return parts[-1] or UNKNOWN


def normalize_project(label: Optional[str]) -> str:
    """Repair a project label that was computed before worktrees were folded in.

    events.ndjson stores the label, not the path, so a corpus ingested by an
    older build still carries strings like
    ``finn--claude-worktrees-native-ota`` -- Claude Code's dash-encoded
    ``<repo>/.claude/worktrees/<branch>`` that the old decoder could not
    resolve. stats.json is the file that gets shared, so the branch is cut out
    there too rather than only at ingest time.

    The doubled dash is the signature: it is the only surviving trace of the
    "/." that starts a dot directory. Requiring it keeps a repo legitimately
    named "my-workspaces-thing" intact.
    """
    if not label:
        return UNKNOWN
    toks = label.split("-")
    for i, t in enumerate(toks):
        if t.lstrip(".").lower() not in WORKTREE_DIRS:
            continue
        head = toks[:i]
        # Whatever follows the pool marker is the branch, and it leaks in prose
        # even once it has been cut out of the label -- see Observed.
        branch = "-".join(x for x in toks[i + 1:] if x).lower()
        if not head:
            OBSERVED.branch(branch)
            return UNKNOWN  # the pool directory itself: no repo to name
        if "" in head:
            head = head[:head.index("")]
        elif head[0].startswith("."):
            OBSERVED.branch(branch)
            return UNKNOWN  # ".claude-worktrees-<branch>": still just the pool
        else:
            break  # a repo whose own name contains the word: leave it alone
        repo = "-".join(x for x in head if x) or UNKNOWN
        OBSERVED.branch(branch, "" if repo == UNKNOWN else repo.lower())
        return repo
    return label


def _dash_segments(name: str):
    """Claude Code's encoded directory name -> path-segment tokens.

    Claude replaces both "/" and "." with "-", so "/finn/.claude/" arrives as
    "-finn--claude-": the empty token between the doubled dashes is the only
    surviving mark of a dot directory. Folding it into the next token keeps
    ".claude" decodable. Without that, the resolver below walked off the
    filesystem at every worktree path and handed the whole encoded string back
    as the project name -- which is exactly how branch names got published.
    """
    toks = []
    dot = False
    for t in name.strip("-").split("-"):
        if not t:
            dot = True
            continue
        toks.append("." + t if dot else t)
        dot = False
    return toks


def _join_tail(toks) -> str:
    """Tokens the filesystem could not confirm, split only where proven.

    Two boundaries survive the encoding: a dot token always begins a segment,
    and a worktree pool is always a directory of its own. Everything else runs
    together as one directory name. The pool matters because without it a path
    that resolves nowhere -- a deleted repo, a corpus copied off the machine
    that wrote it -- fuses ".claude", "worktrees" and the branch into a single
    label, which is the leak this whole module exists to stop.
    """
    segs = []
    boundary = True
    for t in toks:
        marker = t.lstrip(".").lower() in WORKTREE_DIRS
        if boundary or marker or t.startswith("."):
            segs.append(t)
        else:
            segs[-1] += "-" + t
        boundary = marker
    return "/".join(segs)


def decode_dash_path(name: str) -> str:
    """Reconstruct a real path from Claude Code's dash-encoded directory name.

    Claude replaces "/" with "-", which is ambiguous when the directory itself
    contains dashes ("my-web-app" -> "my-web-app" or ".../my/web/app"?).
    Resolve greedily against the filesystem: at each level, prefer the longest
    token run that names a real directory. Whatever is left over -- a pruned
    worktree, a deleted project, a corpus copied off another machine -- keeps
    the old positional guess, but only for the part the filesystem could not
    confirm rather than for the whole path.
    """
    tokens = _dash_segments(name)
    if not tokens:
        return UNKNOWN
    cur = ""
    i = 0
    while i < len(tokens):
        # A dot token starts a segment, so a candidate run may begin with one
        # but must never span one.
        end = len(tokens)
        for k in range(i + 1, len(tokens)):
            if tokens[k].startswith("."):
                end = k
                break
        best = None
        for j in range(end, i, -1):
            cand = cur + "/" + "-".join(tokens[i:j])
            if os.path.isdir(cand):
                best = (cand, j)
                break
        if best is None:
            break
        cur, i = best
    if i >= len(tokens):
        return cur or UNKNOWN
    # Positional guess for the unresolved tail: /<a>/<b>/<c>/<rest-with-dashes>,
    # e.g. /Users/x/Projects/foo-bar.
    rest = list(tokens[i:])
    out = cur
    while rest and i < 3:
        out += "/" + rest.pop(0)
        i += 1
    if rest:
        out += "/" + _join_tail(rest)
    return out


def project_from_encoded_dir(name: str) -> str:
    """Claude Code and Grok both encode the cwd into the directory name.

    Claude:  -Users-alice-Projects-my-web-app
    Grok:    %2FUsers%2Falice%2FProjects%2Fmy-web-app
    """
    cwd = decoded_cwd_from_dir(name)
    # A directory that still exists is a repository we can read a remote out
    # of, which is how ingest learns the `owner/repo` handle the user writes in
    # prose. Only the real ones: a pruned worktree has no .git/config to read.
    if cwd and os.path.isdir(cwd):
        OBSERVED.directory(cwd)
    return project_from_cwd(cwd)


def decoded_cwd_from_dir(name: str) -> Optional[str]:
    if "%2F" in name or "%2f" in name:
        return urllib.parse.unquote(name)
    return decode_dash_path(name)


# How many distinct repositories to read a remote out of. "Be cheap here": a
# corpus can mention hundreds of directories and none of the answers after the
# first few dozen change the redaction list, which is a handful of strings.
GIT_CONFIG_SCAN_LIMIT = 120
_GIT_WALK_UP = 6


def _repo_root(path: str) -> Optional[str]:
    """Nearest ancestor holding a real .git directory, or None.

    A linked worktree's `.git` is a FILE pointing elsewhere; skipped rather
    than followed, because the main checkout is in the same scan and carries
    the same remotes. The same file is what _sibling_worktree_repo() reads --
    there it decides the LABEL, which is a different question.
    """
    cur = path
    for _ in range(_GIT_WALK_UP):
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        nxt = os.path.dirname(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return None


def git_config_texts(dirs, limit: int = GIT_CONFIG_SCAN_LIMIT):
    """`.git/config` contents for the distinct repositories under `dirs`.

    Returned as text so the caller can parse it without this module having to
    know what a remote means -- wcstats.clean owns that.
    """
    out, seen = [], set()
    for d in sorted(dirs or ()):
        if len(out) >= limit:
            break
        root = _repo_root(d)
        if not root or root in seen:
            continue
        seen.add(root)
        try:
            with open(os.path.join(root, ".git", "config"), "r",
                      encoding="utf-8", errors="replace") as fh:
                out.append(fh.read())
        except OSError:
            continue
    return out


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
