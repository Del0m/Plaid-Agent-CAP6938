# TPC-21 Scheduler and Simulated Days Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every real user's budget check each morning with APScheduler, plus a month-end placeholder slot. Let synthetic users live on a simulated date that advances day by day, injecting that day's transactions and running the real agent, from a CLI, an admin API and an Alerts-page panel.

**Architecture:**
- **`budget_service/daily.py`:** one per-user lock and `run_daily` shared by the scheduler, the run button and the simulation; the `close_month` placeholder; `sim_today`.
- **`budget_service/simulate.py`:** `advance_days`, which injects, closes, runs and saves the date.
- **`app/scheduler.py`:** the two cron jobs and `build_scheduler`.
- **`api/admin.py`:** the simulation behind `ENABLE_ADMIN`.
- **Frontend:** a panel on `/alerts`.

**Tech Stack:** Python 3.12, APScheduler 3.x (new), FastAPI, SQLAlchemy 2.1, LangGraph 1.2, pytest; Next.js 16.4, React 19, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-29-tpc-21-scheduler-simulation-design.md`

## Global Constraints

- **Commands:**
  - backend tests run from `backend/` with `../venv/bin/python -m pytest`;
  - frontend tests run from `frontend/` with `npm test`, plus `npm run lint` and `npm run build`.
- **New dependency (the only one):** `apscheduler>=3.10,<4`. Add it to `backend/requirements.txt` and install it with `../venv/bin/pip install "apscheduler>=3.10,<4"`.
- **Settings:**
  - `ENABLE_SCHEDULER` and `ENABLE_ADMIN` are true only for `"1"`, `"true"` or `"yes"` (case-insensitive); the default is off;
  - `DAILY_RUN_TIME="06:00"`, `CLOSE_RUN_TIME="05:00"`, `SCHEDULER_TZ="America/New_York"`.
- **Simulation:**
  - **Prefs:** `prefs_json["sim"] = {"profile": <id>, "date": "YYYY-MM-DD"}`.
  - **Transaction ids:** `f"sim-{profile.id}-{n}"`.
  - **Run graphs:** `"simulate"` for the job row and `"close"` for the placeholder (status `"skipped"`, summary `{"month": "YYYY-MM-01", "note": "period close arrives with TPC-22"}`).
  - **Days:** 1-31 per advance.
- **Error messages (exact):**
  - `"Only simulated users can be advanced."` (409);
  - `"A budget run is already in progress."` (409);
  - `"The simulation couldn't start."` (500);
  - `"Not found."` (404, admin off);
  - the CLI's `"not a simulated user; seed it with --start"`.
- **The working tree holds someone else's uncommitted edits:**
  - `docs/architecture.md` §2;
  - `docs/diagrams/`, `docs/system-walkthrough.md`, `graphify-out/`.

  Never stage, modify or remove them. Commit `architecture.md` changes only as a patch built against `HEAD` and applied with `git apply --cached` plus `git apply`, as done in TPC-20.
- **Test docs:** every test gets an entry in `docs/tests/<module>.md` (or `<Name>.test.md` for frontend tests) in the same commit.
- **Commits:** prefixes `feat(TPC-21):`, `docs(TPC-21):`, `fix(TPC-21):`. **No Co-Authored-By or "Generated with Claude Code" lines.**

## Review Focus

1. **Advancing across New Year** (Dec 31 → Jan 1): the close placeholder must record December 2026, not December 2027 or January. Pinned by `test_close_across_new_year` (Task 3).
2. **Seeding with a start date before the profile's first transaction:** zero rows, `sim` set, and the first advance injects from the beginning. Pinned by `test_until_before_profile_start` (Task 1).
3. **"To month end" on the last day of the month** should advance exactly 1 day, never 0 (which the API rejects with 422). Pinned by `test month end on the last day advances one` (Task 7).
4. **`GET /budget` for a simulated user whose date is past the open period** (Nov 2 with only October) should report `ended_period`, consistent with TPC-20. Pinned by `test_budget_sim_date_past_period` (Task 2).
5. **An advance whose model is down** should end the job `error` with the exception type, keep the date at the last good day, and free the lock. Pinned by `test_advance_job_error_reports_type` (Task 5).

---

### Task 1: Seeding up to a start date

**Files:**
- Modify: `eval/budget_users.py`, `backend/tests/factories.py`, `backend/scripts/seed_budget_user.py`.
- Tests: `backend/tests/test_budget_users.py`, `backend/tests/test_seed_budget_user.py`, with their docs.

**Interfaces:**
- Produces:
  - `seed_profile(session, profile, until: Optional[date] = None) -> str`;
  - `default_start(profile) -> date`: the day before the first of `profile.run_date`'s month, or `profile.today - 1 day` when there's no `run_date`;
  - `add_txn(..., plaid_id: Optional[str] = None)`;
  - `seed_budget_user.py --start {YYYY-MM-DD|default}`.

- [ ] **Step 1: Failing tests.**
  - **`test_budget_users.py`:**
    - `test_seed_until_sets_sim`: seed `overspend-one-time` until Sep 30. All rows are dated on or before Sep 30, the count equals the generated ones up to then, and `prefs_json["sim"] == {"profile": "overspend-one-time", "date": "2026-09-30"}`.
    - `test_seed_without_until_unchanged`: no `sim` key, and the full count.
    - `test_seed_ids_stable`: the ids are `sim-overspend-one-time-0…`, the same across two separate databases.
    - `test_until_before_profile_start`: until Jun 1 → 0 transactions, and `sim.date == "2026-06-01"`.
    - `test_default_start`: `overspend-one-time` gives Sep 30, `overspend-seasonal` gives Nov 30, and `steady` gives Jul 31.
  - **`test_seed_budget_user.py`:** `test_seed_start_default` returns 0, sets `sim` at Sep 30, and prints `python scripts/simulate.py --user budget-overspend-one-time --days 30`.
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run `test_budget_users.py`, `test_seed_budget_user.py`, `test_budget_accuracy.py` and `test_budget_graph.py -k "not live"`, and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): seed synthetic users up to a start date`.

---

### Task 2: `budget_service/daily.py`, settings and simulated "today"

**Files:**
- Create: `backend/app/budget_service/daily.py`, `backend/tests/test_daily.py`, `docs/tests/test_daily.md`.
- Modify: `backend/app/config.py`, `backend/app/api/budget.py`, `backend/tests/test_budget_api.py` (and its doc).

**Interfaces:**
- Produces:
  - **`daily.py`:** `user_lock(user_id) -> threading.Lock` (moved from `api/budget.py`: `_user_lock`, `_locks` and `_locks_guard` go), `sim_today(session, user_id) -> Optional[date]`, `run_daily_locked(session_factory, user_id, today, *, model, checkpointer) -> dict`, `run_daily(...) -> Optional[dict]` and `close_month(session_factory, user_id, month_start) -> int`;
  - **`config.py`:** `ENABLE_SCHEDULER`, `ENABLE_ADMIN`, `DAILY_RUN_TIME`, `CLOSE_RUN_TIME` and `SCHEDULER_TZ`, through a helper `_flag(name) -> bool`.
- **`api/budget.py`:**
  - `start_run` uses `daily.user_lock` and `run_daily_locked` inside its thread (the lock is still taken in the route);
  - `GET /budget` and the run's default date use `sim_today(...) or _today()`.
- **The existing tests** that patch `budget_api._user_lock` switch to `daily.user_lock`.

- [ ] **Step 1: Failing tests.**
  - **`test_daily.py`:**
    - `test_run_daily_none_when_locked`;
    - `test_run_daily_locked_syncs_only_real_items` (patch `daily.plaid_sync`);
    - `test_close_month_placeholder_row` (graph `close`, status `skipped`, the summary exact, `finished_at` set);
    - `test_sim_today` (with `sim` → that date; without → `None`);
    - `test_config_flags` (monkeypatch the environment and reload `app.config` in a fixture that reloads it back: `"Yes"` → True, `""` → False).
  - **`test_budget_api.py`:**
    - `test_budget_uses_sim_date` (seed until Oct 19, then a direct `run_budget` on Oct 19 to create the budget; with `_today` patched to Nov 30, `GET /budget` still reports day 19);
    - `test_run_default_date_is_sim_date` (`POST /budget/run` with `{}` → the run's `agent_runs` and budget are for Oct 19);
    - `test_budget_sim_date_past_period` (sim Nov 2, only an October period → `period` is null, and `ended_period` is October).
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run `test_daily.py` and `test_budget_api.py`, then the full `-k "not live"` suite, and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): one daily run per user behind one lock, and a simulated today`.

---

### Task 3: `advance_days`

**Files:**
- Create: `backend/app/budget_service/simulate.py`, `backend/tests/test_simulate.py`, `docs/tests/test_simulate.md`.
- Modify: `backend/tests/fakes.py` (`SchemaReplyModel`), `backend/app/agents/budget_graph.py` (`close_interrupted_runs` covers `graph in ("budget", "simulate")`), `backend/tests/test_budget_api.py` (sweep test, and its doc).

**Interfaces:**
- Consumes: Task 1's `seed_profile(until=)`; Task 2's `run_daily_locked`, `close_month` and `sim_today`.
- Produces:
  - `NotSimulated`;
  - `advance_days(session_factory, user_id, days, *, model, checkpointer, job_id=None, on_day=None) -> dict`, returning `{days_requested, days_done, start_date, date, run_ids, alerts_written}`, where dates are ISO strings;
  - each `on_day` payload is `{"date", "injected", "status", "flags": [categories], "alerts": n}`;
  - **`SchemaReplyModel()`** in `tests/fakes.py`, as the spec describes (it looks at `kwargs["format"]["title"]`: `"Diagnosis"` or `"Action"`).

- [ ] **Step 1: Failing tests:**
  - `test_thirty_days_thirty_runs` (the done-when);
  - `test_each_day_injects_its_transactions`;
  - `test_month_boundary_closes_first`;
  - `test_close_across_new_year` (seasonal, until Dec 30, 3 days → the close row's summary month is `"2026-12-01"`, written before the Jan 1 budget run, by id order);
  - `test_failed_day_stops_and_resumes`;
  - `test_runs_past_profile_end`;
  - `test_not_simulated_raises`;
  - `test_reinject_is_idempotent` (call the private injector twice for one day: the count is unchanged);
  - `test_job_summary_updates` (`job_id` row: after 3 days, `days_done == 3` and `date` is the third day);
  - in `test_budget_api.py`: `test_startup_closes_interrupted_simulation`.
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass**, then run the full `-k "not live"` suite.
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): advance a simulated user day by day`.

---

### Task 4: `app/scheduler.py` and startup

**Files:**
- Create: `backend/app/scheduler.py`, `backend/tests/test_scheduler.py`, `docs/tests/test_scheduler.md`.
- Modify: `backend/app/main.py`, `backend/requirements.txt`.

**Interfaces:**
- Consumes: `run_daily`, `close_month` and `budget_hooks`.
- Produces:
  - `eligible_users(session) -> list[str]`;
  - `daily_budget_job(session_factory, *, today=None, model=None, checkpointer=None) -> dict`;
  - `month_end_job(session_factory, *, today=None) -> dict`;
  - `build_scheduler() -> BackgroundScheduler`;
  - the lifespan starts it when `config.ENABLE_SCHEDULER`, and shuts it down on exit.

- [ ] **Step 1: Install APScheduler and add it to `requirements.txt`.** Then the failing tests:
  - `test_eligible_users`;
  - `test_daily_job_runs_eligible`;
  - `test_daily_job_skips_locked_and_survives_failure` (one user's lock held; another's `run_daily` patched to raise for that user; a third ok → `ran`, `skipped` and `failed` are each that one user);
  - `test_month_end_job_closes_previous_month` (`today=2026-11-01` → `close` rows with month `"2026-10-01"`);
  - `test_build_scheduler_jobs`: ids `{"daily_budget", "month_end"}`. Each trigger's fields stringify to `hour='6', minute='0'` for the daily job and `day='1', hour='5', minute='0'` for month end, in timezone `America/New_York`. Both have `max_instances == 1`, `coalesce is True` and `misfire_grace_time == 3600`. Read them via `scheduler.get_jobs()` without starting;
  - `test_lifespan_starts_only_when_enabled` (patch `app.main.build_scheduler` to return a stub that records `start`/`shutdown`: with the flag, started and shut down; without it, never built).
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass**, then run the full `-k "not live"` suite.
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): daily budget and month-end jobs on APScheduler`.

---

### Task 5: `api/admin.py`

**Files:**
- Create: `backend/app/api/admin.py`, `backend/tests/test_admin_api.py`, `docs/tests/test_admin_api.md`.
- Modify: `backend/app/main.py`.

**Interfaces:**
- Consumes: `advance_days`, `NotSimulated`, `daily.user_lock`, `budget_hooks`, and `api.budget._spawn`, which tests patch to run inline.
- Produces: `GET /admin/sandbox`, `POST /admin/sandbox/advance` (`AdvanceRequest {days: int = Field(ge=1, le=31)}`) and `GET /admin/sandbox/advance/{job_id}`, as in the spec.

- [ ] **Step 1: Failing tests** (fixture like TPC-20's `api`: hooks patched, `SchemaReplyModel` as the model, `_spawn` inline, `config.ENABLE_ADMIN = True`):
  - `test_admin_off_404` (all three routes);
  - `test_sandbox_state`;
  - `test_advance_202_then_ok`;
  - `test_advance_not_simulated_409`;
  - `test_advance_locked_409`;
  - `test_advance_days_422` (0 and 32);
  - `test_other_users_job_404`;
  - `test_start_failure_500_frees_lock`;
  - `test_advance_job_error_reports_type` (the model raises `ConnectionError` → the job is `error` with `"ConnectionError"`, `sim.date` is unchanged, and the lock is free).
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass**, then run the full suite.
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): admin endpoints to advance a simulated user`.

---

### Task 6: `scripts/simulate.py`

**Files:** Create `backend/scripts/simulate.py`, `backend/tests/test_simulate_cli.py` and `docs/tests/test_simulate_cli.md`.

**Interfaces:**
- Produces: `main(argv=None, session_factory=None, model=None) -> int`.
- Path setup: as in `seed_budget_user.py`.
- Day line format: `f"{date}  +{injected} txns  {status}  flags: {', '.join(flags) or '-'}  alerts: {alerts}"`.
- Summary line: `f"simulated {n} day(s): {start} → {end}, {runs} runs, {alerts} alerts"`.

- [ ] **Step 1: Failing tests:**
  - `test_cli_prints_days_and_summary` (3 days from Oct 10 → 3 day lines, and the summary line);
  - `test_cli_not_simulated_exit_1`;
  - `test_cli_locked_exit_1`.
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): simulate cli`.

---

### Task 7: Frontend simulation panel

**Files:**
- Modify: `frontend/lib/api.ts`, `frontend/lib/api.test.ts`, `frontend/app/alerts/page.tsx`, `frontend/app/alerts/page.test.tsx`.
- Docs: `docs/tests/api.test.md`, `docs/tests/alerts-page.test.md`.

**Interfaces:**
- Produces: `SimState`, `SimJob`, `getSim()`, `advanceSim(days)` and `getSimJob(id)`, as in the spec.
- Helper: `daysToMonthEnd(iso: string): number` in `lib/format.ts`, at least 1, computed in UTC.

- [ ] **Step 1: Failing tests.**
  - **`api.test.ts`:** the URLs and bodies (`GET /api/admin/sandbox`, `POST /api/admin/sandbox/advance` with `{"days":7}`, `GET /api/admin/sandbox/advance/5`), and `daysToMonthEnd("2026-10-03") == 28`.
  - **`page.test.tsx`:**
    - `no panel when admin is off`;
    - `no panel for a real user`;
    - `advance 7 days polls and reloads` (it shows "Day 7 of 7", then "Advanced 7 days to Oct 10, 2026: 7 runs, 1 alert written.", and `getAlerts` and `getSim` are called again);
    - `simulation error shows where it stopped`;
    - `409 shows message`;
    - `run button disabled while simulating`;
    - `month end on the last day advances one` (sim date 2026-10-31 → `advanceSim(1)`).
- [ ] **Step 2: `npm test` and confirm they fail.** **Step 3: Implement.** No `new Date()` during render; dates are formatted from ISO strings with the existing `formatDate`. **Step 4: `npm test`, `npm run lint` and `npm run build`, and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-21): simulation panel on the alerts page`.

---

### Task 8: Live check, demo and docs

**Files:**
- Modify: `backend/tests/test_simulate.py` (live test), `docs/architecture.md` (via the patch method), `README.md` and `.env.example`.

- [ ] **Step 1: Live test** `test_live_simulated_week` (skipped without Ollama):
  - `overspend-one-time` until Oct 9, 7 days with the real model → 7 budget runs;
  - no two GENERAL_MERCHANDISE overspend alerts share a severity;
  - at least one `high` alert, created by a run dated Oct 12 or later.

  Run it twice. If it fails, rule (prompt wording only) and rerun.
- [ ] **Step 2: CLI demo on dev.db:**
  1. `seed_budget_user.py --profile overspend-one-time --replace --start default`;
  2. `scripts/simulate.py --user budget-overspend-one-time --days 31`.

  Expected: 31 day lines, an alert around Oct 12, a `close` entry on Nov 1, and exit 0. Save the output for the decision log.
- [ ] **Step 3: Docs.**
  - Architecture sections 4.1, 5, 6.2/6.6, 9, 12 and 13, and the decision-log rows from the spec.
  - The README sections "Simulate a month" and "Run the agent every morning".
  - The five settings in `.env.example`.
- [ ] **Step 4: Full backend suite (live included), `npm test` and `npm run build`.** Commit message: `docs(TPC-21): architecture, decision log, readme and live simulated week`.
