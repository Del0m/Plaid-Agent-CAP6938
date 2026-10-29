# Tests: `backend/tests/test_scheduler.py`

These tests cover `app/scheduler.py` (TPC-21), the budget agent's own schedule:
- **Every morning** (`DAILY_RUN_TIME`, default 06:00), a budget check for each eligible user: anyone with a budget or a real Plaid item, but not simulated users.
- **On the 1st** (`CLOSE_RUN_TIME`, default 05:00), the month-end slot for the month that just ended.

The scheduler only starts when `ENABLE_SCHEDULER` is on. The jobs are plain functions, so the tests call them directly instead of waiting for a clock.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_scheduler.py -v
```

## Shared setup

The `users` fixture seeds four planted users:
- **budgeted:** has a budget, from a run on Oct 2;
- **real:** has a real-looking Plaid item but no budget;
- **both:** has a budget and a real item;
- **simulated:** seeded from Nov 30, with its own budget.

`SchemaReplyModel` answers the model calls, and the audit log goes to `tmp_path`.

---

## `test_eligible_users`

**What it does:** The budgeted, real and both users are eligible, sorted. The simulated user isn't.

**Why it's there:** The scheduler checks real people every day. Simulated users move on their own date, and a real-date run would put an alert on the wrong day.

## `test_daily_job_runs_eligible`

**What it does:** The job runs all three eligible users for the given date, and writes nothing for the simulated user.

**Why it's there:** It's the autonomous part of the agent: nobody presses a button.

## `test_daily_job_skips_locked_and_survives_failure`

**What it does:** One user's run lock is held, and another's run raises. The job skips the first, records the second as failed, and still runs the third.

**Why it's there:** A user mid-simulation, or mid-click, isn't run twice. One user's Plaid outage mustn't cost everyone else their morning check.

## `test_month_end_job_closes_previous_month`

**What it does:** On Nov 1, the job records the month-end slot for October for each of the three eligible users.

**Why it's there:** Close has to run for the month that just ended, before that day's budget check. TPC-22 fills in what it does.

## `test_build_scheduler_jobs`

**What it does:** The scheduler has a `daily_budget` job at 06:00 and a `month_end` job on the 1st at 05:00, both in America/New_York. Both have one instance at a time, missed runs coalesced, and a one-hour grace. It isn't started.

**Why it's there:** These settings decide when users get alerts. A missed run within the hour still happens once, and a server that was down all morning doesn't replay stale days.

## `test_lifespan_starts_only_when_enabled`

**What it does:** With `ENABLE_SCHEDULER` off, starting the app builds no scheduler. With it on, the scheduler starts with the app and shuts down with it.

**Why it's there:** A dev server or the test suite must never run the daily job by surprise. A real deployment must not leave it running after shutdown.
