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

**What it does:** When the run crashes (here `evaluate` raises `RuntimeError`), the CLI prints the error type and returns 1.

**Why it's there:** A crash should be a clear message and a failing exit code that a scheduler or script can act on, not a traceback. An Ollama outage used to be the example. Since the review on #12, an outage ends in fallback alerts instead of a crash (`test_model_unreachable_still_alerts`), so the test now crashes the run elsewhere.

## `test_cli_bad_date`

**What it does:** `--date 2026-13-01` exits with argparse's usage error (code 2).

**Why it's there:** A mistyped date should fail before anything touches the database.

## `test_cli_shows_sync_errors`

**What it does:** When the sync reports an error, the report says `sync: 1 item(s), errors: InvalidToken (data may be stale)`.

**Why it's there:** It was found in the TPC-19 review: a failed sync used to finish as a plain `status: ok`, so someone running this daily would never learn their spending was out of date.

## `test_cli_shows_sync_skipped`

**What it does:** With `--no-sync`, the report says `sync: skipped`.

**Why it's there:** It makes the report state whether the data was refreshed at all.

## `test_cli_awaiting_user`

**What it does:** On the gym user, with a scripted `adjust_next_budget`, the CLI prints `status: awaiting_user (waiting for alert(s) …)`. Its checkpoints go to a file under `tmp_path`.

**Why it's there:** A run paused for approval isn't finished, and the CLI has to say what it's waiting on. The CLI keeps its checkpoints in `BUDGET_CHECKPOINT_PATH`, so the API can resume the run later.

## `test_tests_never_use_the_real_checkpoint_file`

**What it does:** Checks that, under pytest, `BUDGET_CHECKPOINT_PATH` and `CHAT_CHECKPOINT_PATH` aren't the real `backend/data/` files.

**Why it's there:** `main()` opens `config.BUDGET_CHECKPOINT_PATH`, and only one CLI test pointed it elsewhere. The others wrote to the developer's real file, and since the in-memory test database hands out run ids 1, 2 and so on, `run_budget` called `delete_thread("budget-1")` there, wiping a real paused run's checkpoint. The autouse `checkpoint_paths` fixture in `conftest.py` now gives every test its own files. The review on #13 found it.
