# Tests: `backend/tests/test_simulate_cli.py`

These tests cover `backend/scripts/simulate.py` (TPC-21), the terminal way to advance a simulated user. It prints one line per day (date, transactions injected, run status, flags, alerts written) and a summary at the end.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_simulate_cli.py -v
```

## Shared setup

- `main` takes a `session_factory` and a model, so the tests use the test database and `SchemaReplyModel` (`alert` once per category per month, then `no_op`).
- The prompt audit log and the checkpoint file go to `tmp_path`.
- `seed(db, until)` seeds the one-time user, simulated from Oct 9 by default.

---

## `test_cli_prints_days_and_summary`

**What it does:** Advancing 3 days from Oct 9 prints lines for Oct 10, 11 and 12. Oct 12's line shows the one injected purchase and the general-merchandise flag. The last line reads "simulated 3 day(s): 2026-10-09 → 2026-10-12, 3 runs, …".

**Why it's there:** A month with the real model takes minutes. The per-day lines show progress, and they show the day the planted overspend is caught.

## `test_cli_not_simulated_exit_1`

**What it does:** A user seeded without a start date exits 1 with "not a simulated user; seed it with --start".

**Why it's there:** The message names the fix, and the exit code lets a script notice.

## `test_cli_locked_exit_1`

**What it does:** While the user's run lock is held, the CLI exits 1 with "A budget run is already in progress.".

**Why it's there:** Two simulations in one process must not advance the same user at once.

## Not covered

- **The lock is in-process.** A CLI run doesn't see an API server's locks in another process. Don't simulate a user from the terminal while the API is advancing it.
