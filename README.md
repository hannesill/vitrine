# Vitrine

**Stay in the loop of autonomous research agents. Vitrine provides a powerful, interactive research log your agents can easily write to.**

Every `show()` call adds a card to a persistent, browsable record — tables, charts, decisions, reasoning. Open the browser to see where an analysis stands, review past decisions, trace provenance. Studies persist on disk, survive restarts, and export as self-contained HTML.

[![Watch the demo](https://img.shields.io/badge/Watch-Demo_Video-blue?style=for-the-badge)](https://share.descript.com/view/rE8xbiybB44)

```python
from vitrine import show

show(df, title="Patient Demographics", study="sepsis-v1")
show(fig, title="Age Distribution")
show("## Exclusion Decision\nRemoving patients with ICU stay < 24h.")
```

Three lines. A browser tab opens. Your analysis has a permanent, shareable record.

---

## Why Vitrine

Research happens in code. But results scatter across terminal output, notebook cells, and lost matplotlib windows. The gap between "got results" and "published paper" is enormous — and it's the part researchers hate most.

Jupyter is the de facto tool for computational research, and it's terrible for reproducibility, collaboration, and publication. Vitrine is what Jupyter should have been for the age of AI-assisted research: structured provenance instead of cell execution order, blocking checkpoints instead of free-form code cells, publishable output instead of notebook soup.

As AI agents become capable of running entire research pipelines, the bottleneck shifts from *"can the agent do the analysis?"* to *"can we trust the analysis?"* Vitrine captures provenance as a side effect of the workflow, not as an afterthought. The researcher doesn't do extra work to get reproducibility — they get it by using the tool.

## Install

```bash
pip install vitrine
```

**Optional:** The built-in agent actions (Reproduce, Report, Paper) spawn headless CLI agent sessions. Supported backends: [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`), [Gemini CLI](https://github.com/google-gemini/gemini-cli) (`gemini`), and [Codex CLI](https://github.com/openai/codex) (`codex`). Install any of these and select your preferred backend from the agent config form before running.

## Quick Start

```python
from vitrine import show
import pandas as pd

# Any DataFrame becomes an interactive, paginated table
df = pd.DataFrame({"age": [45, 67, 32], "score": [12, 8, 15]})
show(df, title="Cohort Overview")

# Plotly charts render interactively
import plotly.express as px
fig = px.histogram(df, x="age")
show(fig, title="Age Distribution")

# Markdown documents your reasoning
show("## Key Finding\nMortality rate is **23%** after exclusions.")

# Dicts become formatted key-value cards
show({"patients": 4238, "mortality": "23%", "p_value": 0.003})
```

The server auto-starts on first `show()` and opens a browser tab. No setup, no config.

## Core Concepts

### One Function, Many Types

DataFrames become paginated tables with server-side sort and search via DuckDB. Plotly figures render interactively. Matplotlib figures convert to SVG. Strings render as markdown. Dicts become key-value cards. Images display natively.

### Studies, Not Sessions

Group cards by research question with `study="sepsis-v1"`. Studies persist on disk across server restarts, forming a provenance trail you can revisit weeks later. Each study is a dated directory with card descriptors, metadata, and artifact files.

```python
from vitrine import show, list_studies, export, study_context

show(df, title="Baseline", study="sepsis-v1")
show(fig, title="Survival Curve", study="sepsis-v1")

# Browse all studies
for s in list_studies():
    print(f"{s['label']}: {s['card_count']} cards")

# Export as self-contained HTML
export("sepsis-v1-report.html", study="sepsis-v1")

# Re-orient after a break
ctx = study_context("sepsis-v1")
print(ctx["card_count"], "cards,", len(ctx["decisions"]), "decisions made")
```

Deep links: `http://localhost:7741/#study=sepsis-v1` opens directly to a study.

Studies live in a `.vitrine/` directory: the nearest one at or above the directory you run from, or a new one there. Every git worktree of a repository shares the main checkout's store, so work on a branch lands in the same study as the rest of the project; a worktree that already has its own `.vitrine/` keeps it. Set `VITRINE_DATA_DIR` to put the store somewhere else.

Each study is `.vitrine/studies/<study>/`, and its `index.json` is rewritten whenever a card is added or changed. A tool that embeds Vitrine can watch those files to notice new cards without a running server; `vitrine status --json` reports the store as `data_dir`.

### Human-in-the-Loop Decisions

Block execution until a researcher confirms, chooses, or steers:

```python
from vitrine import show, confirm, ask

# Simple confirmation gate
if confirm("Exclude patients with ICU stay < 24h?", study="sepsis-v1"):
    df = df[df.icu_hours >= 24]

# Multiple choice
score = ask("Which severity score?", ["SOFA", "APACHE III", "Both"])

# Rich decision card with data context
response = show(
    df,
    title="Review Cohort",
    wait=True,
    prompt="Approve this cohort or narrow further?",
    actions=["Approve", "Narrow further", "Add exclusion"],
)
print(response.action)   # "Approve"
print(response.message)  # Free-text steering from the researcher
```

Decision cards freeze into permanent records of what was decided and when — provenance by default.

### Passive Selection Tracking

Researchers select table rows and chart points while browsing. The agent reads selections whenever it needs them:

```python
from vitrine import get_selection

selected = get_selection(card_id)  # Returns a DataFrame of selected rows
```

No polling, no callbacks, no event loops. Just pull when ready.

### Structured Input

Collect structured responses with `Form` and `Question`:

```python
from vitrine import show, Form, Question

form = Form([
    Question(
        name="severity_score",
        question="Which severity score to use?",
        options=["SOFA", "APACHE III", "SAPS II"],
    ),
    Question(
        name="focus",
        question="Analysis focus areas?",
        options=["Mortality", "Length of stay", "Readmission"],
        multiple=True,
    ),
])

response = show(form, title="Analysis Parameters", study="sepsis-v1")
print(response.values)  # {"severity_score": "SOFA", "focus": ["Mortality", "Readmission"]}
```

### Progress Tracking

```python
from vitrine import progress

with progress("Building cohort", study="sepsis-v1") as status:
    load_data()
    status("Applying exclusions...")
    apply_exclusions()
    status("Computing features...")
    compute_features()
# Card auto-updates to ✓ complete (or ✗ failed on exception)
```

## Architecture

```
Agent writes Python    →    show(obj)    →    Artifact Store    →    Browser tab
                            localhost         + WebSocket            live render
```

Large objects are persisted as Parquet/JSON/SVG on disk. The WebSocket sends lightweight card references. Tables are paged server-side via DuckDB on Parquet — no browser crashes, no data size limits. The frontend is a single self-contained HTML file with vendored JS (Plotly.js, marked.js). No build step. Works offline. Works on air-gapped networks.

## CLI

```bash
vitrine start                          # Start server, open browser
vitrine start --port 7742              # Custom port
vitrine start --no-open                # Start without opening browser
vitrine start --owner-pid 4242         # Stop once process 4242 has exited
vitrine status                         # Show server status
vitrine stop                           # Stop server (studies persist on disk)
vitrine restart                        # Restart server
vitrine studies                        # List all studies
vitrine clean 7d                       # Remove studies older than 7 days
vitrine export report.html --study sepsis-v1
```

### Host applications

An app that runs Vitrine for its users drives the server with `--json`. `vitrine start`, `status`, `stop` and `restart --json` print one JSON object on one line, lifecycle contract 1 (`vitrine doctor --json` reports the contract version):

| Field | Meaning |
|---|---|
| `status` | `running`, `stopped` or `failed` |
| `url`, `api_url`, `pid`, `port`, `session_id` | the running server, `null` without one |
| `owner_pids` | the processes the server stops with (see below); `[]` for a server without owners, `null` without a server |
| `data_dir` | the study store |
| `version` | the running server's Vitrine version, else the installed one |
| `error` | what went wrong when `status` is `failed` |

The exit code is 0, 1 for `failed`, and 3 for `status --json` when no server runs. New fields can appear within a contract version; ignore the ones you do not know. `VITRINE_OPEN_BROWSER=0` keeps every server started in that environment from opening the system browser.

**Owner processes.** A server runs detached and outlives the process that started it until `vitrine stop`, and the ten ports from 7741 to 7750 limit how many can run. A host that may not get to run `vitrine stop` when it exits (it is killed, it crashes, or, on Windows, it is ended with `TerminateProcess`) names itself the owner:

- `vitrine start --owner-pid <pid>`, or `VITRINE_OWNER_PID=<pid>` in the environment. The variable also reaches `show()` in that environment: a server it starts by itself gets the owner, and so does a running server it finds, as with `start` below. Vitrine versions that predate owners ignore it.
- Every two seconds the server checks its owners. Once the last one has exited it stops the way `vitrine stop` stops it: the studies stay on disk and `.server.json` goes.
- `start` with an owner that finds a server running adds the owner to it, also to a server that was started without one, which from then on stops with its owners. A server stops when its last owner exits, so one host exiting does not take the board from another; `vitrine stop` stops it whatever its owners.
- `owner_pids` lists the owners. A server from a Vitrine version that predates owners keeps running without taking one, so check that your PID is there, and stop that server yourself or restart it.
- `start` fails when the owner is not running, rather than starting a server nothing would stop; `show()` then falls back to a server inside its own process. When it finds a server that is already stopping because its owners exited, it waits for it to exit and starts a new one.
- `restart` gives the new server the old one's owners that are still running, and the one it is given.
- An owner that has exited counts as gone even while it lingers: as a zombie its parent has not reaped yet on Linux and macOS, or held open by another process's handle on Windows.
- A PID can be reused once its process has exited. The server records each owner's start time on Linux, macOS and Windows and does not mistake a later process with the same PID for the owner. Elsewhere it compares PIDs only, and a PID reused within the two seconds between checks keeps the server running until that process exits.

## Python API

```python
# Display
show(obj, title=None, description=None, *, study=None, source=None,
     replace=None, wait=False, prompt=None, timeout=600,
     actions=None) -> DisplayHandle | DisplayResponse
section(title, study=None) -> None

# Decisions
confirm(message, *, study=None, timeout=600) -> bool
ask(question, options, *, study=None, timeout=600) -> str
wait_for(card_id, timeout=600) -> DisplayResponse

# Progress
progress(title, *, study=None) -> ProgressContext

# Server
start(port=7741, open_browser=True, mode="thread") -> None
stop() -> None
stop_server() -> bool
server_status() -> dict | None

# Studies
list_studies() -> list[dict]
delete_study(study) -> bool
clean_studies(older_than="7d") -> int
study_context(study) -> dict
export(path, format="html", study=None) -> str
register_output_dir(path=None, study=None) -> Path

# Interaction
get_selection(card_id) -> pd.DataFrame
get_card(card_id) -> CardDescriptor | None
on_event(callback) -> None
list_annotations(study=None) -> list[dict]
```

## Roadmap

Vitrine is a production-ready research journal today. Where it's going is **the system of record for agentic research** — the way GitHub became the system of record for code.

- **Living Paper** — Export studies as manuscript skeletons in IMRAD structure with auto-generated methods from the decision trail. Go from finished study to paper draft, not just a card dump.
- **Review Mode** — Structured collaborative review: PIs approve, flag, or request revisions on any card. Reviews produce actionable items the agent picks up directly.
- **Validation Overlays** — Automatic statistical sanity checks on every card: sample size warnings, class imbalance, missing data rates, effect size assessment. The clinical intelligence layer that makes Vitrine trustworthy for research.
- **Study Branching** — Fork a study at any decision point to explore alternatives. Side-by-side comparison of branches. Git for research studies.
- **Cross-Study Memory** — Surface patterns across a lab's entire body of work. "Across all sepsis studies, we consistently found X."
