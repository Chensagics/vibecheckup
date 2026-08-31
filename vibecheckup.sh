#!/usr/bin/env sh
# vibecheckup — one command from a fresh machine to an open dashboard.
#
#   From a checkout:   ./vibecheckup.sh [--demo] [--scrub] [--tool X] [--limit N]
#                                       [--no-open]
#   One-liner:         curl -fsSL <raw-url>/vibecheckup.sh | sh
#
# Reads the AI coding-session logs already on this machine, analyzes them, and
# opens a single self-contained dashboard.html. Python 3 is the only
# requirement; nothing is installed and nothing is uploaded. Re-running simply
# refreshes. Local modes make no network calls at all -- the one exception is
# the curl bootstrap below, which downloads this repo when there is no checkout.
set -eu

# Must match the public GitHub repo (owner/name) that serves this script.
GH_REPO="${VIBECHECKUP_REPO:-chensagics/vibecheckup}"
GH_BRANCH="${VIBECHECKUP_BRANCH:-main}"
PREFIX="${VIBECHECKUP_HOME:-$HOME/.vibecheckup}"

die() { printf 'vibecheckup: %s\n' "$1" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
vibecheckup — word clouds, spend and Agent Wrapped for your AI coding sessions.

  ./vibecheckup.sh                 ingest real logs, analyze, build, open
  ./vibecheckup.sh --demo          use a synthetic corpus instead (no real logs)

Options:
  --demo            generate a deterministic sample corpus via samples/generate.py
  --force, -f       with --demo, overwrite data/events.ndjson without asking
  --scrub           also build dashboard-shareable.html: the same page without
                    project names, error text or shell commands, and open that
                    one instead. dashboard.html is still written, unchanged.
  --tool NAME       limit ingest to one source (repeatable):
                    claude_code, codex, grok, gemini_cli, antigravity, cursor
  --limit N         cap files per source (quick smoke run)
  --no-open         build the dashboard but do not open a browser
  -h, --help        this message

Everything runs locally. Re-run any time to refresh.
dashboard.html holds your project names and error text -- keep it private, and
share the --scrub copy instead.
EOF
}

# Answer --help before anything else, so `curl ... | sh -s -- --help` neither
# downloads a repo nor insists on a working python3 just to print usage.
for a in "$@"; do
  case "$a" in -h|--help) usage; exit 0 ;; esac
done

# --- python3 -----------------------------------------------------------------

py_install_hint() {
  if [ "$(uname -s)" = "Darwin" ]; then
    echo "  On macOS, the Command Line Tools include it:" >&2
    echo "      xcode-select --install" >&2
    echo "  (or install from python.org / 'brew install python3')" >&2
  else
    echo "  Debian/Ubuntu:  sudo apt install python3" >&2
    echo "  Fedora:         sudo dnf install python3" >&2
    echo "  Arch:           sudo pacman -S python" >&2
  fi
}

# `command -v python3` is not enough. A Mac without the Command Line Tools
# still carries a /usr/bin/python3 shim, so that check passed and the user got
# Apple's install dialog instead of this message. Actually run python3, and
# read the version it reports: the adapters need 3.9, which is what the CLT
# installs.
PY_VERSION="$(python3 -V 2>/dev/null)" || PY_VERSION=""
if [ -z "$PY_VERSION" ]; then
  echo "vibecheckup: python3 (3.9+) is required, but there is no working python3 on your PATH." >&2
  py_install_hint
  exit 1
fi

PY_NUM="${PY_VERSION#Python }"
PY_MAJOR="${PY_NUM%%.*}"
PY_MINOR="${PY_NUM#*.}"
PY_MINOR="${PY_MINOR%%.*}"
case "$PY_MAJOR:$PY_MINOR" in
  *[!0-9:]*|:*|*:)
    echo "vibecheckup: python3 (3.9+) is required, but 'python3 -V' printed '$PY_VERSION', which is not a version I can read." >&2
    py_install_hint
    exit 1 ;;
esac
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
  echo "vibecheckup: the python3 on your PATH is $PY_NUM, but vibecheckup needs 3.9+ — install a newer Python 3 and re-run." >&2
  py_install_hint
  exit 1
fi

# --- locate the checkout, or fetch one ---------------------------------------

# A file called ingest.py is not proof of a checkout: plenty of repos have one,
# and mistaking the user's for ours ran their code and wrote our data/ into
# their tree. Require a combination only this repo has.
is_checkout() {
  [ -n "$1" ] && [ -f "$1/ingest.py" ] && [ -f "$1/adapters/base.py" ] &&
    [ -f "$1/wcstats/prices.json" ]
}

# "$0" is only a path when it has a slash in it. Piped to sh (`curl ... | sh`)
# it is just "sh", and dirname would hand back "." -- wherever the user happens
# to be standing, which is nobody's checkout. Without a slash it can still be
# `sh vibecheckup.sh` run from inside one, which a file of that name next to a
# real checkout tells apart.
case "$0" in
  */*) SELF_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo "")" ;;
  *)   if [ -f "./$0" ] && is_checkout "."; then SELF_DIR="$(pwd)"; else SELF_DIR=""; fi ;;
esac

fetch_repo() {
  # curl | sh path: no checkout next to us, so download the tarball.
  if [ "${VIBECHECKUP_BOOTSTRAPPED:-0}" = "1" ]; then
    die "bootstrap ran twice — $PREFIX looks incomplete, remove it and retry"
  fi
  url="https://codeload.github.com/$GH_REPO/tar.gz/refs/heads/$GH_BRANCH"
  printf 'vibecheckup: no checkout here, fetching %s -> %s\n' "$GH_REPO" "$PREFIX"
  command -v tar >/dev/null 2>&1 || die "tar is required to unpack the download"
  mkdir -p "$PREFIX"
  tgz="$PREFIX/.vibecheckup-download.tar.gz"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$tgz" || die "download failed: $url"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$tgz" "$url" || die "download failed: $url"
  else
    die "curl or wget is required to download $GH_REPO"
  fi
  tar -xzf "$tgz" -C "$PREFIX" --strip-components=1 || die "could not unpack $tgz"
  rm -f "$tgz"
  is_checkout "$PREFIX" ||
    die "the download does not look like vibecheckup — check GH_REPO=$GH_REPO"
}

if ! is_checkout "$SELF_DIR"; then
  fetch_repo
  VIBECHECKUP_BOOTSTRAPPED=1
  export VIBECHECKUP_BOOTSTRAPPED
  exec sh "$PREFIX/vibecheckup.sh" "$@"
fi
ROOT="$SELF_DIR"

# --- arguments ---------------------------------------------------------------

DEMO=0
FORCE=0
OPEN=1
SCRUB=0
LIMIT=""
TOOLS=""

need_value() {
  [ "$2" -gt 1 ] || die "$1 needs a value (see --help)" 2
}

add_tool() { # keep the value to a safe charset: it is re-parsed by eval below
  case "$1" in
    ""|*[!A-Za-z0-9_-]*) die "--tool wants a source name, got '$1'" 2 ;;
  esac
  TOOLS="$TOOLS --tool $1"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --demo)     DEMO=1 ;;
    --force|-f) FORCE=1 ;;
    --scrub)    SCRUB=1 ;;
    --no-open)  OPEN=0 ;;
    --tool)     need_value --tool "$#"; shift; add_tool "$1" ;;
    --tool=*)   add_tool "${1#--tool=}" ;;
    --limit)    need_value --limit "$#"; shift; LIMIT="$1" ;;
    --limit=*)  LIMIT="${1#--limit=}" ;;
    -h|--help)  usage; exit 0 ;;
    *)          printf 'vibecheckup: unknown option %s\n\n' "$1" >&2
                usage >&2; exit 2 ;;
  esac
  shift
done

case "$LIMIT" in
  "") ;;
  *[!0-9]*) die "--limit wants a whole number, got '$LIMIT'" 2 ;;
esac

EVENTS="$ROOT/data/events.ndjson"

# --- stages ------------------------------------------------------------------

stage() { # stage <label> <command...>
  label="$1"; shift
  printf '\n==> %s\n' "$label"
  rc=0                      # not `status`: zsh reserves that name
  "$@" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '\nvibecheckup: %s failed (exit %s).\n' "$label" "$rc" >&2
    printf 'Nothing further was run — the output above shows what went wrong.\n' >&2
    exit 1
  fi
}

cd "$ROOT"
mkdir -p "$ROOT/data"   # a fresh tarball has no data/ (it is gitignored)

if [ "$DEMO" = "1" ]; then
  if [ -f "$EVENTS" ] && [ "$FORCE" != "1" ]; then
    printf 'vibecheckup: %s already exists and --demo will replace it\n' "$EVENTS" >&2
    printf '           with synthetic data (your real ingest is one re-run away).\n' >&2
    if [ -t 0 ]; then
      printf '           overwrite? [y/N] ' >&2
      read -r answer || answer=""
      case "$answer" in
        y|Y|yes|YES) ;;
        *) die "aborted — nothing was changed" ;;
      esac
    else
      die "aborted — re-run with --force to overwrite"
    fi
  fi
  stage "demo corpus (samples/generate.py)" \
    python3 "$ROOT/samples/generate.py" "$EVENTS"
else
  # TOOLS holds "--tool a --tool b"; eval turns it back into separate words in
  # every shell (plain $TOOLS would not split under zsh). add_tool has already
  # restricted each value to [A-Za-z0-9_-], so there is nothing to inject.
  eval "set -- $TOOLS"
  if [ -n "$LIMIT" ]; then set -- "$@" --limit "$LIMIT"; fi
  stage "ingest (session logs -> data/events.ndjson)" \
    python3 "$ROOT/ingest.py" "$@"
fi

stage "analyze (events -> data/stats.json)" python3 "$ROOT/analyze.py"

DASH="$ROOT/dashboard.html"
SHARE="$ROOT/dashboard-shareable.html"

if [ "$SCRUB" = "1" ]; then
  stage "build (stats + template -> dashboard.html + dashboard-shareable.html)" \
    python3 "$ROOT/build_dashboard.py" --scrub
  [ -f "$SHARE" ] || die "expected $SHARE to exist after the build"
else
  stage "build (stats + template -> dashboard.html)" python3 "$ROOT/build_dashboard.py"
fi

[ -f "$DASH" ] || die "expected $DASH to exist after the build"

printf '\n✓ vibecheckup is ready: %s\n' "$DASH"
# After --demo the page is built from the synthetic corpus, so the usual
# warning would be telling you to guard invented projects.
if [ "$DEMO" = 1 ]; then
  printf '  Synthetic corpus — none of this is your data.\n'
else
  printf '  Keep that file private: it carries your project names and error text.\n'
fi

# --scrub is a request to inspect the copy you are about to hand over, so that
# is the one that opens. dashboard.html is written either way.
TO_OPEN="$DASH"
if [ "$SCRUB" = "1" ]; then
  printf '\n  share copy: %s\n' "$SHARE"
  printf '  No project names, error text or shell commands — but the clouds are\n'
  printf '  still your own words, so read them before you hand it over.\n'
  TO_OPEN="$SHARE"
fi

if [ "$OPEN" = "1" ]; then
  case "$(uname -s)" in
    Darwin) open "$TO_OPEN" ;;
    Linux)
      if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$TO_OPEN" >/dev/null 2>&1 &
      else
        printf '  (no xdg-open found — open the file above in a browser)\n'
      fi ;;
    *) printf '  (open the file above in a browser)\n' ;;
  esac
fi
