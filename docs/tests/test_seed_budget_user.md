# Tests: `backend/tests/test_seed_budget_user.py`

These tests cover `backend/scripts/seed_budget_user.py` (TPC-19). It writes one of the synthetic budget users from `eval/budget_users.py` into `dev.db` as `budget-<profile>`, and prints the command that runs the budget agent on it. With `--replace`, it first deletes the user and everything it owns. This is how the done-when demo gets a planted overspend into `dev.db`.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_seed_budget_user.py -v
```

## Shared setup

- `main` takes a `session_factory`, so the tests seed the test database instead of `dev.db`.
- `PID` is `overspend-one-time`, `UID` is its user id, and `N` is its number of generated transactions.
- `count(db, cls, **filters)` counts rows in a fresh session.

---

## `test_seed_writes_rows`

**What it does:** Seeding writes every generated transaction for `budget-overspend-one-time`, and prints the run command with `--date 2026-10-20`.

**Why it's there:** The printed command is the demo's second step, so it has to carry the profile's run date.

## `test_seed_refuses_existing`

**What it does:** Seeding the same profile again returns 1, points to `--replace`, and leaves the row count unchanged.

**Why it's there:** A second seed would otherwise double every transaction, and every budget built on it.

## `test_replace_deletes_and_reseeds`

**What it does:** After a budget run has created a period, an alert, advice and a run record, `--replace` deletes all of them and re-seeds the same number of transactions.

**Why it's there:** A clean re-run of the demo needs the agent's own rows gone too, not just the transactions.

## `test_unknown_profile`

**What it does:** An unknown profile returns 2 and lists the valid ones.

**Why it's there:** A typo should say what to type instead.

## Not covered

- **Foreign-key order on `--replace`:** the test database doesn't enforce foreign keys, so the delete order is checked on `dev.db`, where they're on, by the demo run (plan Task 9).
