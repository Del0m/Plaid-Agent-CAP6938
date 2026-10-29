# Tests: `backend/tests/test_simulate.py`

These tests cover `app/budget_service/simulate.py` (TPC-21). It moves a synthetic user's simulated date forward one day at a time:
1. it injects that day's generated transactions;
2. on the 1st, it records the month-end slot for the month that just ended;
3. it runs the real budget agent for that day;
4. only then does it save the new date.

This lets a planted month be replayed and watched.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_simulate.py -v
```

## Shared setup

- `seed(db, pid, until)` seeds a planted user as simulated from `until`; the default is the one-time user from Sep 30.
- `advance(db, uid, days)` runs `advance_days` with `SchemaReplyModel` (`tests/fakes.py`) and collects each day's report. That fake model answers any structured call: a `one_time` diagnosis, then `alert` the first time a category is flagged that month, and `no_op` once it was.
- `runs(db, graph)` lists the run rows of one kind.

---

## `test_thirty_days_thirty_runs`

**What it does:** Advancing the one-time user 30 days from Sep 30 runs one budget check per day, Oct 1 to Oct 30: 30 runs in `agent_runs`, and 30 run ids in the progress. The simulated date ends at Oct 30.

**Why it's there:** It's TPC-21's done-when, checked with a fake model so it takes seconds.

## `test_each_day_injects_its_transactions`

**What it does:** Each simulated day injects exactly the transactions generated for that date. After 12 days, the $600 ElectroMart purchase is in, and nothing after Oct 12 is.

**Why it's there:** Data must arrive day by day, as from a bank, so the agent never sees a purchase before it happens.

## `test_month_boundary_closes_first`

**What it does:** Advancing from Oct 30 across Nov 1 records the month-end slot for October before Nov 1's budget run.

**Why it's there:** Close must finish a month before the new month's first check. That's what TPC-22 will rely on.

## `test_close_across_new_year`

**What it does:** Advancing the seasonal user from Dec 30 across Jan 1 records the close for December 2026, before Jan 1's run.

**Why it's there:** The previous month at New Year is December of the year before, an easy off-by-one (plan Review Focus 1).

## `test_failed_day_stops_and_resumes`

**What it does:** When the third day's run fails, the advance stops with the date at the second day. The next advance starts from the third day.

**Why it's there:** A model outage mid-simulation must not skip a day or leave the date ahead of the runs that actually happened.

## `test_runs_past_profile_end`

**What it does:** Past the profile's last generated day, days inject nothing and still run.

**Why it's there:** "To month end", or a long advance, must not fail just because the synthetic data has run out.

## `test_not_simulated_raises`

**What it does:** A user seeded without a start date can't be advanced.

**Why it's there:** Advancing a user who lives on real time would invent purchases in their ledger.

## `test_reinject_is_idempotent`

**What it does:** Injecting Oct 12 twice adds its transactions only once.

**Why it's there:** A retried day must never double a purchase, or the agent would see an overspend that didn't happen.

## `test_job_summary_updates`

**What it does:** With a job row, its summary shows three days done, the current date and three run ids after three days.

**Why it's there:** The admin API and the Alerts panel report progress ("Day 3 of 7") from this row while the job runs.

## `test_live_simulated_week`

**What it does:** It is skipped unless Ollama is running with `OLLAMA_MODEL`. It simulates the one-time user from Oct 10 to Oct 16 with the real model:
- seven budget runs happen;
- no two general-merchandise alerts share a severity;
- there's an over-limit (`high`) alert from a run on or after Oct 12.

**Why it's there:** It's the simulation with the real agent: data arriving day by day, the memory stopping repeats, and the escalation to over the limit still reported. Live it alerts twice: pace on Oct 10, and over the limit on Oct 12.
