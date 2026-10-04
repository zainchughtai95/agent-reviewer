# Agent Reviewer

A human-in-the-loop agent that finds inefficient **SQL**, **Python**, and **Spark/PySpark** code in a GitHub repository, explains *why* it is costly, assigns a severity, waits for a person to approve the rewrite, and only then opens a pull request.

The agent never pushes a fix on its own. GitHub writes happen only after a reviewer clicks **Approve** and then **Create PR**.

## What it does

1. Connects to GitHub with a personal access token and walks the repo tree.
2. Keeps `.sql`, `.py`, and Spark (`.scala` plus Python files that import PySpark) files.
3. Parses SQL with [SQLGlot](https://github.com/tobymao/sqlglot) and Python/PySpark with Python's `ast` module. It looks for `SELECT *`, `collect()`, `iterrows()`, Python UDFs, cartesian joins, and similar shapes in the tree.
4. Sends each candidate snippet to the OpenAI API for:
   - a plain-language explanation of why it is inefficient
   - production impact (driver OOM, full scan, shuffle, cost)
   - severity (`critical` / `high` / `medium` / `low` / `info`)
   - a proposed rewrite
   - a false-positive check so a parsed shape that is fine in context can be dropped
5. Stores findings in a local SQLite queue.
6. Serves a small review UI so a human can approve, edit, or reject each finding.
7. After approval, creates a branch, commit, and pull request on the original repo. The PR body lists severity, reasoning, and the human-signed-off fix.

Local-only scans are supported for demos and CI (`--local` and `--skip-llm`).

## Architecture

```
GitHub repo ──► scanner ──► SQLGlot / Python AST ──► OpenAI reviewer ──► SQLite findings
                                                                  │
                                                                  ▼
                                                         review UI (human)
                                                                  │
                                                          approved snippets
                                                                  ▼
                                                         Git Data API + PR
```

| Piece | Role |
| --- | --- |
| `sql_scan.py` / `python_scan.py` | SQLGlot and Python AST passes so the model only sees real statements and calls |
| `openai_reviewer.py` | Structured JSON review: reasoning, severity, rewrite, false-positive filter |
| `store.py` | Durable queue (`pending` → `approved` / `rejected` → `applied`) |
| `web/` | Reviewer UI |
| `pr_creator.py` | Applies approved snippets, creates a branch + PR via the GitHub API |

## Setup

Python 3.11+ is required.

```bash
cd agent-reviewer
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env`:

| Variable | Required | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | For GitHub scan + PR | Fine-grained or classic token with **Contents: Read and write** and **Pull requests: Write** on the target repo |
| `OPENAI_API_KEY` | For reasoning / severity / rewrites | Standard OpenAI API key |
| `OPENAI_MODEL` | No | Defaults to `gpt-4o-mini` |
| `MAX_FILES` / `MAX_FILE_BYTES` | No | Caps how much of a large monorepo is pulled |

## Usage

Scan a GitHub repo (uses the default branch ref unless you pass another):

```bash
agent-reviewer scan owner/repo --ref main
```

The command prints as it goes: each file, the parser that read it, the line it flagged, then each OpenAI result (`kept high` or `dropped as a false positive`). `--quiet` leaves only the final table.

Scan the bundled inefficient examples without GitHub or OpenAI:

```bash
agent-reviewer scan local/examples --local examples/inefficient --skip-llm
```

Open the human review queue:

```bash
agent-reviewer serve
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). For each finding you can:

- read the reasoning, impact, and severity
- edit the suggested snippet
- leave notes
- **Approve for PR** or **Reject**

When at least one finding on a GitHub scan is approved, the queue shows **Create PR for approved findings**. That is equivalent to:

```bash
agent-reviewer create-pr owner/repo
agent-reviewer status
```

The PR branch is named `agent-reviewer/optimizations-<timestamp>` and the description includes every approved finding so another reviewer can still treat it as a suggestion, not a merge-on-sight patch.

## What the parsers look for

SQLGlot walks the SQL tree. A match inside a comment or a function wrapped around a literal, such as `LOWER('OPEN')`, is not a finding. Python's AST does the same for calls, loops, and assignments. The code is never executed.

**SQL:** `SELECT *` in the select list, leading-wildcard `LIKE`, a function wrapped around a filtered column, `NOT IN (SELECT …)`, `SELECT DISTINCT`, `CROSS JOIN`, `IN (SELECT …)`, correlated subqueries (an outer column referenced inside), `ORDER BY` without `LIMIT`.

**Python:** `iterrows()` / `itertuples()`, `apply()` only on a pandas object this file created, `+=` on a string inside a loop, a nested loop over the same collection, `read_text()` / `read_bytes()` / an unbounded `read()` on a file, `time.sleep`.

**Spark / PySpark:** `.collect()`, `.toPandas()`, zero-argument `.count()` inside a loop on a DataFrame, Python `udf` / `pandas_udf`, RDD `map` / `flatMap` / `foreach`, `coalesce(1)`, `crossJoin` / `cartesian`, `repartition(n)` with no key, `.take(n)` for n of 1000 or more.

SQL written as a string in Python is parsed with SQLGlot too. Scala files are scanned for SQL string literals only, because Python has no Scala parser.

These are candidates, not verdicts. OpenAI is asked to drop false positives before a human ever sees the queue.

## Safety model

- Findings start as `pending`. Nothing is committed.
- A human must approve the exact snippet that will be applied (and can edit it).
- PRs are only opened for `approved` items on a GitHub scan.
- Local example scans cannot open a PR, so you can demo the UI without write access.
- File size and file count caps avoid dumping huge generated trees into the model.
- Vendor dirs (`.venv`, `node_modules`, `__pycache__`, …) are skipped.

Treat every PR as a suggested optimization. Run tests and check query plans on production-shaped data before merging.

## Project layout

```
src/agent_reviewer/
  cli.py              # scan, serve, create-pr, status
  github_client.py    # repo tree, file fetch, git blobs, PRs
  heuristics.py       # sends each file to SQLGlot or Python's AST
  sql_scan.py         # SQLGlot rules
  python_scan.py      # ast rules for Python and PySpark
  openai_reviewer.py  # structured OpenAI review
  pipeline.py         # scan orchestration
  pr_creator.py       # apply approved snippets and open a PR
  store.py            # SQLite finding queue
  web/                # human review UI
examples/inefficient/ # sample SQL, pandas, and PySpark anti-patterns
tests/                # heuristics, local scan, review UI
```

## Tests

```bash
pytest
```

Tests stay offline: they scan `examples/inefficient` and drive the review UI against a temp SQLite file. They do not call GitHub or OpenAI.

## Suggested next steps

- Add Slack or email when a scan finishes so the on-call reviewer gets a queue link.
- Persist a suppression file (path + rule + snippet hash) so rejected findings do not come back every week.
- Wire `agent-reviewer scan` to a nightly GitHub Action on the repos you care about.
- Expand Spark coverage to `.ipynb` notebooks if that is where most jobs live.
