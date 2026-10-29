# Tests: `backend/tests/test_budget_graph_cli.py`

These tests cover the budget agent's command line, `python -m app.agents.budget_graph --user <id> [--date YYYY-MM-DD] [--no-sync] [--model NAME]` (TPC-19). It runs `run_budget` once and prints:
- the run's status and period;
- every line's spending, limit and pace;
- each flag's cause, action, message and proposed limit;
- the alerts written, the unbudgeted categories and any LLM failures.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_graph_cli.py -v
```

## Shared setup

- `seeded(db)` writes the `overspend-one-time` planted user into the test database.
- `replies()` scripts a `one_time` diagnosis and an `alert` action.
- `main` takes `session_factory` and `model` arguments, so the tests use the test database and a scripted model instead of `dev.db` and Ollama.
- An autouse fixture points the prompt audit log at `tmp_path`.

---

## `test_cli_prints_report`

**What it does:** A run on Oct 20 with `--no-sync` returns 0. It prints the status, the general-merchandise flag, its cause, action and message, and "alerts written: 1".

**Why it's there:** The done-when is a CLI run whose output shows the right cause and an action, so the report has to carry them.

## `test_cli_error_exit`

**What it does:** When the model raises a connection error, the CLI prints the error type and returns 1.

**Why it's there:** An Ollama outage should be a clear message and a failing exit code that a scheduler or script can act on, not a traceback.

## `test_cli_bad_date`

**What it does:** `--date 2026-13-01` exits with argparse's usage error (code 2).

**Why it's there:** A mistyped date should fail before anything touches the database.

## `test_cli_shows_sync_errors`

**What it does:** When the sync reports an error, the report says `sync: 1 item(s), errors: InvalidToken (data may be stale)`.

**Why it's there:** It was found in the TPC-19 review: a failed sync used to finish as a plain `status: ok`, so someone running this daily would never learn their spending was out of date.

## `test_cli_shows_sync_skipped`

**What it does:** With `--no-sync`, the report says `sync: skipped`.

**Why it's there:** It makes the report state whether the data was refreshed at all.
