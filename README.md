<div align="center">

# vibecheck

**A local vibecheck on your AI coding year.**

One command reads the session logs already on your machine — Claude Code, Codex,
Grok CLI, Gemini CLI, Antigravity — and builds a single self-contained
`dashboard.html`: your **Agent Wrapped**, your **Lexicon**, your **Spend**.

Nothing is installed, nothing is uploaded, `python3` is the only requirement.

[![License: MIT](https://img.shields.io/badge/License-MIT-d97757.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-3fb950.svg)](https://www.python.org/)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-58a6ff.svg)
![Works offline](https://img.shields.io/badge/works-offline-a371f7.svg)

<!-- assets/wrapped-card.png — the Agent Wrapped share card, generated from
     `./vibecheck.sh --demo` so it contains no real vocabulary. -->
<img src="assets/wrapped-card.png" alt="Agent Wrapped — the downloadable share card" width="420">

</div>

---

## Three faces of one file

### ✨ Agent Wrapped — the landing view

A full-screen slide sequence, Spotify-Wrapped style: how many words you typed at
an AI this year, how many prompts and sessions, your top words and top phrase,
your peak hour and weekday, your longest streak and busiest day, how often you
said *please*, which tool you actually live in, what it would have cost. It ends
on a share card drawn to a `<canvas>` with a **Download PNG** button — branded
`VIBECHECK ⚡ AGENT WRAPPED 2026`, and containing **no project names and no local
paths**. Counts and vocabulary only. Slides whose stat is missing are skipped,
not faked.

### Lexicon — what you actually say to an AI

Word clouds over your real prompts: the headline cloud, then what is
*distinctive* about each tool and each project rather than what they have in
common, phrases that survive as phrases (`dev server`, `word cloud`), and terms
rising or fading month over month. The question it answers: **what do I actually
ask AI to do, how has that changed, and how does it differ per tool and per
project?**

### Spend — what your year would cost at list prices

Token usage is extracted natively from the same logs — no Node, no `ccusage` at
runtime — and priced from a bundled table: total, rolling 7 / 30 days, cache-hit
rate, cost per day, spend by model and by tool. Grok CLI and Antigravity log no
usage at all and are badged **no usage data** instead of being counted as zero.

<!-- assets/dashboard.png — the Lexicon and Spend tabs, generated from
     `./vibecheck.sh --demo` so it contains no real vocabulary. -->
<img src="assets/dashboard.png" alt="vibecheck — word clouds and spend" width="860">

## Run it

**One line.** No clone, no pip, no dependencies — it fetches the repo into
`~/.vibecheck` and runs there:

```bash
curl -fsSL https://raw.githubusercontent.com/chensagics/vibecheck/main/vibecheck.sh | sh
```

> The raw URL above points at `chensagics/vibecheck` — **the final public repo
> path**. If you fork or rename, override it with `VIBECHECK_REPO=owner/name`.

### Or let your agent do it

vibecheck turns the session logs your AI coding tools already keep on this
machine into one local dashboard — what you actually ask AI for, when you work,
what it would cost, and a shareable card of your year.

**Copy the box below** and paste it into Claude Code, Codex, or any agent you
drive. It will read the docs, explain the tool to you, and set it up once you
agree.

```text
Read https://github.com/chensagics/vibecheck. Before running anything, tell me
in your own words what vibecheck does, what it reads, and where my data goes,
then ask if I want to proceed. If I say yes, install and run it, then read the
stats it generates and tell me what a year of my prompts says about me.
```

From a checkout:

```bash
./vibecheck.sh            # ingest -> analyze -> build -> open dashboard.html
./vibecheck.sh --demo     # a synthetic corpus instead: no real logs needed
```

Re-run any time to refresh; there is no cache to go stale. A full run re-reads
everything — a few thousand files in well under a minute.

| Flag | Effect |
|---|---|
| `--demo` | Build from a deterministic synthetic corpus (`samples/generate.py`). Never touches your real logs. |
| `--force`, `-f` | With `--demo`, overwrite an existing `data/events.ndjson` without asking. |
| `--tool NAME` | Limit ingest to one source; repeatable. `claude_code`, `codex`, `grok`, `gemini_cli`, `antigravity`. |
| `--limit N` | Cap files per source — a quick smoke run. |
| `--no-open` | Build the dashboard but don't open a browser. |
| `-h`, `--help` | Usage. Answered before anything is downloaded, so `curl … \| sh -s -- --help` costs nothing. |

Environment: `VIBECHECK_REPO` / `VIBECHECK_BRANCH` (what the one-liner fetches)
and `VIBECHECK_HOME` (where it lands, default `~/.vibecheck`).

The script is a thin wrapper over three stages you can also run yourself:

```bash
python3 ingest.py          # all session logs  -> data/events.ndjson
python3 analyze.py         # events            -> data/stats.json
python3 build_dashboard.py # stats + template  -> dashboard.html
```

Python 3 standard library only. `python3` is the single requirement; on a bare
Mac the script points you at `xcode-select --install`.

## Privacy

**Everything runs locally and none of your session data ever leaves your
machine.** The pipeline makes no network calls at all: `ingest.py`,
`analyze.py` and `build_dashboard.py` only read files and write files, and the
dashboard opens over `file://`. The one network call in the project is the
`curl` bootstrap above, which downloads this repo when there is no checkout.

- **The repo ships no user data.** `data/`, `snapshots/` and `dashboard.html`
  are gitignored, because they contain your real vocabulary, project names and
  local paths.
- **The Wrapped card contains no project names and no paths** — that rule is
  enforced in `wcstats/wrapped.py`, not in the page, so it holds for anything
  built from `stats.json`. You choose what to share: the card is a PNG you
  download, not something this tool posts anywhere.
- Politeness counters and every word cloud run over *cleaned* prose, so a
  "please" injected by a hook or a skill definition can never be credited to
  you.
- The built page makes **no** network requests at all: no fonts, no scripts, no
  images fetched from anywhere. It renders identically on a machine that has
  never been online.

## Costs are estimates, not a bill

Every dollar figure is a **list-price estimate** computed here from the token
counts in your own logs. It is not an invoice and it is not fetched from any
billing API. On a subscription plan your real cost is the flat fee — read these
numbers as a relative signal (which days, models and tools are heavy), never as
what you owe.

Rates live in [`wcstats/prices.json`](wcstats/prices.json), one line per model
pattern, matched as a prefix so dated and codenamed variants resolve to their
family. Anthropic rates are published list prices; the OpenAI, Google and xAI
entries are explicitly labelled `ESTIMATE` where a codename has no published
rate card. A model that matches nothing is reported as **unpriced with a null
cost — never silently zeroed**. Rates change: PRs against that file are welcome
and are the cheapest possible contribution.

## Sources

| Tool | Location | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/**/*.jsonl` | includes nested `subagents/` transcripts; priced |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl`, `archived_sessions` | priced; cumulative token totals, see below |
| Grok CLI | `~/.grok/sessions/<enc-cwd>/<id>/chat_history.jsonl` | no per-message timestamps, no usage data |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json` | plain JSON; priced |
| Antigravity | `~/.gemini/antigravity-cli/conversations/*.db` | SQLite + schema-less protobuf; heuristic, no usage data |

## How it works

**`ingest.py`** runs one adapter per tool. Adapters *classify* — they map a
source record type onto a `(role, kind)` pair — and never judge text by content.
Every file is parsed independently: a malformed line is counted and skipped,
never fatal. Each run prints a per-tool report of files parsed, bad lines, and
any record type the adapter did not recognise, so a silent upstream format
change shows up instead of quietly dropping data.

**`analyze.py`** does the content work, and most of the value is in what it
throws away. A typical Claude Code session holds a handful of real prompts against
hundreds of tool results and hook attachments — unfiltered, the corpus is **mostly machine
text**, and every cloud reads *skill, important, file, the*. `wcstats/clean.py`
strips injected context (hook output, skill definitions, agent-history
injections, CLAUDE.md echoes), fenced and unfenced code, file dumps, diffs, and
log lines.

Scoring goes past raw counts:

- **Frequency** for the headline cloud.
- **Log-odds with an informative Dirichlet prior** for every comparison facet.
  Raw counts make each tool and project look identical; this surfaces what is
  *distinctive* about one against the whole corpus.
- **PMI collocations**, so `dev server` and `word cloud` survive as phrases.
- **Month-over-month rate deltas** for rising and fading terms.

Near-identical automation prompts are clustered by shingle key and counted once
by default, with raw counts available via a toggle.

**`wcstats/spend.py`** prices the usage the adapters extracted, per component.
Adapters normalise `input` to be cache-exclusive, so the buckets are disjoint
and simply add; only the *rate* differs by provider. Anthropic bills cache reads
and cache writes as their own lines, while OpenAI/Google-style `cached` input is
a discounted subset of input and is billed at the `cached_input` rate.
Reasoning and thinking tokens price as output. Codex reports `total_token_usage`
cumulatively per session and re-emits it on idle, so per-turn usage is recovered
by differencing consecutive snapshots — a repeat differences to zero. Summing
instead overcounted a long multi-hour rollout by about a third. That trap is
locked down by a regression test.

**`build_dashboard.py`** inlines `stats.json` into the template. The data is
embedded rather than fetched because the `file://` origin policy blocks a
sibling `fetch()`, and a single file opens by double-click — and stays readable
years later, offline, with no server.

### Retention is uneven, and that would lie to you

Retention differs sharply per tool — Codex reaches back to January, Claude Code
and Grok keep about five weeks. A naive time series would report log rotation as
a change in behaviour. Month rates are therefore computed *within* each tool
before comparison, and the dashboard's coverage rail shows exactly which tools
have data in which month.

### Antigravity is best-effort, and says so

Antigravity stores conversations as SQLite DBs whose `steps.step_payload` column
holds protobuf with no available schema. `adapters/protoscan.py` walks the wire
format generically and recovers strings; `step_type` was mapped to roles
empirically by reading extracted text across a 40-conversation sample. Every
event from this adapter carries `confidence: "heuristic"` and is badged as such
in the dashboard.

## Layout

```
vibecheck.sh        bootstrap: check python3, run the three stages, open the page
ingest.py  analyze.py  build_dashboard.py  dashboard_template.html
adapters/   base.py protoscan.py claude_code.py codex.py grok.py
            gemini_cli.py antigravity.py
wcstats/    clean.py tokenize.py score.py facets.py spend.py wrapped.py
            prices.json
samples/    generate.py         deterministic synthetic corpus for --demo
tests/      test_all.py test_bootstrap.py fixtures/
snapshot.py                     keep a dated copy of a built dashboard
data/       events.ndjson stats.json vocab.json     (generated, gitignored)
dashboard.html                                      (generated, gitignored)
```

`data/vocab.json` holds full ungated counts; `stats.json` keeps top-N per facet
so the dashboard loads instantly.

## Development

```bash
python3 -m unittest discover -s tests   # 124 tests, fixtures only, no network
./vibecheck.sh --demo --no-open         # full pipeline on synthetic data
```

Tests run entirely on checked-in fixtures — they never read your real logs. The
fixture suite covers adapter classification, the cleaner, the scoring maths,
pricing (including the Codex cumulative-total trap and the unknown-model null),
the `stats.json` contract, and the bootstrap script's argument handling.

## Roadmap

- Multi-device rollup (each machine publishes a snapshot, one dashboard merges).
- A Cursor adapter.
- Live / auto-refreshing mode.
- Price-table auto-update.

## Acknowledgements

Inspired by [**ccusage**](https://github.com/ryoppippi/ccusage) by
[@ryoppippi](https://github.com/ryoppippi), which showed that local agent logs
are enough to answer the spend question, and by the Spotify Wrapped format,
which showed that a year of data is worth more as a story than as a table.
vibecheck computes its own estimates natively in Python and bundles neither.

## License

[MIT](LICENSE) © Chen Sagi
