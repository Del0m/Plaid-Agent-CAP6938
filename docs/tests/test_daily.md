# Tests: `backend/tests/test_daily.py`

These tests cover `app/budget_service/daily.py` (TPC-21), the one way a user's daily budget run happens, whoever starts it: the scheduler, the run button, or a simulated day.
- **`user_lock`:** a per-user lock, so those can never overlap.
- **`run_daily`:** takes the lock, or returns `None` if it's held.
- **`run_daily_locked`:** the run itself. It syncs Plaid only for users with a real item.
- **`close_month`:** the month-end slot's placeholder, until TPC-22's close.
- **`sim_today`:** a simulated user's date.

The settings the scheduler and admin routes read are also checked here.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_daily.py -v
```

## Shared setup

- `seed(db, pid, until)` seeds a planted user, as simulated from `until` if given.
- `model()` scripts a `one_time` diagnosis and an `alert`.
- The audit log goes to `tmp_path`.

---

## `test_run_daily_none_when_locked`

**What it does:** While the user's lock is held, `run_daily` returns `None` and writes no run. Once it's free, the run happens and the lock is released afterwards.

**Why it's there:** The scheduler skips a user whose run is already going (from the button or a simulation), instead of starting a second one on the same budget.

## `test_run_daily_locked_syncs_only_real_items`

**What it does:** A seeded user (placeholder token) isn't synced. Once its item has a real-looking token, the run syncs it first.

**Why it's there:** Real users must be checked against fresh Plaid data. A seeded user's fake token would only record a sync error on every simulated day.

## `test_close_month_placeholder_row`

**What it does:** `close_month` records an `agent_runs` row (`close`, `skipped`) with the month and a note that close arrives with TPC-22.

**Why it's there:** The month-end slot runs on schedule and in simulations now, and the record shows it ran. TPC-22 replaces the body without touching the wiring.

## `test_sim_today`

**What it does:** A user seeded as simulated at Sep 30 gets that date, and a normal user gets `None`.

**Why it's there:** The dashboard and the run button use it as "today" for simulated users, and the real date for everyone else.

## `test_config_flags`

**What it does:** `ENABLE_SCHEDULER=Yes` turns the scheduler on, and an empty `ENABLE_ADMIN` leaves admin off. The times and timezone default to 06:00, 05:00 and America/New_York.

**Why it's there:** Both features must be off unless asked for, so a dev server or the test suite never runs jobs or exposes admin routes by surprise.
