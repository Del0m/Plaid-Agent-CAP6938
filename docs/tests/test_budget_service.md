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

Uses the `db` fixture from `conftest.py` (see [test_plaid.md](test_plaid.md#shared-setup-backendtestsconftestpy)) and `add_user` from `tests/factories.py`.

- `period(user_id, start)` builds an unsaved October 2026 `BudgetPeriod`.

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

## Not covered

- **The Alembic migration** is checked by hand, not by pytest: on a scratch SQLite file, `alembic upgrade head`, `alembic check` (no drift from the models), `alembic downgrade -1`, then `upgrade head` again.
