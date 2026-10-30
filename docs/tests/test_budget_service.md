# Tests: `backend/tests/test_budget_service.py`

These tests cover the budget tables (TPC-18, architecture section 5) and `app/budget_service/initialize.py`, which builds the user's first budget from history and stores it (architecture 6.3):
- `budget_periods` holds one budget per user per calendar month.
- `budget_lines` holds one limit per category in a period.
- Each period and line keeps the evidence behind it in `fit_json`, so the agent can explain the budget.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_service.py -v
```

## Shared setup

Uses the `db` fixture from `conftest.py` (see [test_plaid.md](test_plaid.md#shared-setup-backendtestsconftestpy)) and `add_user`, `add_account` and `add_txn` from `tests/factories.py`.

- `period(user_id, start)` builds an unsaved October 2026 `BudgetPeriod`.
- `TODAY` is Oct 15 2026, so the fit window is July, August and September 2026.
- `user(s, user_id)` adds a user with one checking account and returns the account.
- `history(s, acct, travel_merchant)` adds three months of food ($400 / $450 / $500), rent ($1,500 on the 1st) and travel ($200, then $1,200 + $700 in August, then $250).
- `counts(db)` returns the number of periods and lines, read in a fresh session.

---

## `test_one_period_per_user_month`

**What it does:** Saving two October periods for the same user raises `IntegrityError`.

**Why it's there:** `initialize_budget` and period close must never open two budgets for one month. The unique `(user_id, start_date)` constraint makes that a database guarantee, which also covers two runs racing each other.

## `test_one_line_per_category`

**What it does:** Saving two FOOD_AND_DRINK lines in one period raises `IntegrityError`.

**Why it's there:** Pace, alerts and period close all look a line up by category. A duplicate would make "the" food limit ambiguous and double count it in totals.

## `test_period_defaults`

**What it does:** A new period is `open` with no `fit_json`. A new line starts at `spent_cents=0` and `last_pace=None`, and is reachable through `period.lines`.

**Why it's there:** These are the states the budget graph (TPC-19) starts from before its first `update_totals`. A period opened by period close has no fit evidence, so `fit_json` must be allowed to be empty.

## `test_alert_proposed_limit`

**What it does:** An alert can be saved without a proposed limit, and with one (16000¢). Both read back correctly.

**Why it's there:** TPC-19 stores the code-computed limit for an `adjust_next_budget` alert here, and TPC-22's close applies exactly that number when the user accepts. Every other alert leaves it empty.

## `test_initialize_creates_period_and_lines`

**What it does:** With the shared history, `initialize_budget` opens an October period. Read back in a new session, it has three `init` lines: food $472.50 (flexible), rent $1,500 (fixed) and travel $262.50 (flexible). Travel's `fit_json` holds its monthly totals and August as an outlier, with both August purchases as ISO dates and integer cents. The period's `fit_json` lists the three window months.

**Why it's there:** It's the done-when: a user with history gets a sensible first budget. Reading in a fresh session matters because a `date` left inside the JSON would only fail when written, and the identity map would hide it.

## `test_skipped_recorded`

**What it does:** A single $300 general-merchandise purchase in August is listed in the period's `skipped` and gets no line.

**Why it's there:** The agent can only explain "Shopping was left out, you spent there in 1 of 3 months" if the skipped categories are stored.

## `test_load_txns_mapping`

**What it does:** Of seven rows, `load_txns` returns the four that belong to the user, aren't removed and fall in the date range, including both boundary days. A row with `our_category="TRAVEL"` comes back as TRAVEL with its detailed category and merchant, and a row with no category comes back as `UNCATEGORIZED`.

**Why it's there:** Removed rows are soft-deleted Plaid transactions and would double count. Another user's spending must never leak into a budget. Once the `categorize` node writes `our_category`, its choice has to win over Plaid's.

## `test_current_month_ignored`

**What it does:** Initializing on Aug 1 with $100 of food each in May, June and July and $5,000 on Aug 1 gives a $105 food line.

**Why it's there:** The current month is partial, even when today is its first day. A purchase on the 1st must not reach the fit.

## `test_second_call_returns_same_period`

**What it does:** Calling `initialize_budget` twice returns the same period, and there is still one period with three lines.

**Why it's there:** The budget graph runs every day and calls this first. It must be safe to call again.

## `test_existing_older_period_returned`

**What it does:** With a closed September period already stored, the call returns it and opens no October period.

**Why it's there:** Architecture 6.2: the budget is initialized only if none exists. Opening the next month belongs to period close (TPC-22), which carries limits forward instead of re-fitting.

## `test_no_history_returns_none`

**What it does:** A user whose only transaction is in the current month gets `None`, and nothing is written.

**Why it's there:** There's no complete month to fit. An empty period would block every later attempt, because the next run would find a budget and stop.

## `test_only_excluded_history_returns_none`

**What it does:** Three months of only paychecks and savings transfers give `None` and no rows.

**Why it's there:** Income and transfers aren't spending. Their history must not count as "history exists" and produce an empty budget.

## `test_all_skipped_returns_none`

**What it does:** History with only one occasional purchase (every category skipped) gives `None` and no rows.

**Why it's there:** Same reason as no history: a period with no lines would stop the next run from trying again once there's real spending.

## `test_outlier_without_merchant`

**What it does:** When the outlier month's purchases have no merchant name, the stored top transaction has `"merchant": null`.

**Why it's there:** Plaid often has no merchant name. Storing the evidence must not crash on it.

## `test_race_returns_existing_period`

**What it does:** An October period already exists, but the first existence check is made to miss it, as if another run committed it just after the check. The commit hits the unique constraint, and the call returns the existing period. Afterwards there's one period and no stray lines.

**Why it's there:** The scheduler and a manual run can start together. The loser must roll back cleanly and use the winner's budget, not crash or leave half a budget behind.

## `test_other_integrity_error_raises`

**What it does:** The commit fails on the unique constraint, but no period can be found afterwards (the lookup is patched to miss). `initialize_budget` re-raises the `IntegrityError` instead of returning `None`.

**Why it's there:** `None` means "not enough history yet", so the daily run would retry forever and nobody would see the real error. Only a race, where another run's period now exists, is safe to swallow (found in the TPC-18 final review).

## Not covered

- **The Alembic migration** is checked by hand, not by pytest: on a scratch SQLite file, `alembic upgrade head`, `alembic check` (no drift from the models), `alembic downgrade -1`, then `upgrade head` again.

## `test_last_pace_is_double`

**What it does:** Checks that `budget_lines.last_pace` is mapped as `Double`.

**Why it's there:** The migration creates the column as `sa.Double()`, but the model left the type to SQLAlchemy. That gives `Double` on 2.1 (installed here) but `Float` on 2.0, which `requirements.txt` (`sqlalchemy>=2`) still allows, and autogenerate or `alembic check` would then report drift. The type is now pinned, the same way `Liability.apr` pins `Float`. The test passes on 2.1 either way; it's a guard against an install that resolves 2.0. The review on #11 found it.
