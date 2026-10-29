# TPC-21: Scheduler and simulated days

Jira: TPC-21 (epic TPC-4, M3). Branch: `TPC-21`, cut from `TPC-20`.

## Goal

Make the budget agent run without being asked, and make time testable:
- **Scheduler (for real users).** An in-process APScheduler runs every real user's budget check each morning, and a month-end slot on the 1st. That slot is a recorded placeholder until TPC-22's close.
- **Simulation (for development and demos).** Synthetic users live on a simulated date. Each advanced day injects that day's generated transactions and runs the real agent, so a planted month can be replayed and watched on `/alerts`.

**Done when:** simulating 30 days gives 30 budget runs in `agent_runs`, one per simulated date.

**Out of scope:**
- period close itself (TPC-22; TPC-21 only calls a placeholder);
- Plaid Sandbox `/sandbox/transactions/create` (simulation is synthetic users only);
- catching up missed days;
- several uvicorn workers;
- the TPC-19 and TPC-20 deferred minors.

## Decisions

| Decision | Why |
|---|---|
| Advancing injects each day's generated transactions; synthetic users are seeded only up to a start date and carry `prefs_json["sim"] = {"profile", "date"}` | Data arrives over time as from a bank, and the dashboard, which reads the simulated date, follows the simulation |
| Only simulated users can be advanced (`409` otherwise); the admin routes answer only with `ENABLE_ADMIN=1` | Advancing a real user would mix invented purchases into a real ledger |
| Simulation runs the real agent (real model calls) in a background job; the done-when is checked with a schema-aware fake model | A month takes minutes on `qwen2.5:14b` (two calls per flagged day), but every run is the real agent. The user's choice over a model-free fast mode or code dedupe |
| In-process APScheduler 3.x (`BackgroundScheduler`), started on app startup only with `ENABLE_SCHEDULER=1` | Named by the ticket and architecture 13; quiet by default in dev and tests; fits the single-worker assumption |
| One shared `run_daily` behind one per-user lock, used by the scheduler, the run button and each simulated day | A scheduled run, a click and a simulation can never overlap for one user |
| Simulation progress lives in an `agent_runs` row (`graph="simulate"`) | Survives a restart, is visible next to the runs it made, and the startup sweep closes an interrupted one |
| The month-end slot calls `close_month`, which in TPC-21 only records an `agent_runs` row (`graph="close"`, `status="skipped"`) | The slot, ordering and wiring are tested now; TPC-22 replaces the body |
| No catch-up of missed days (misfire grace 1 h, coalesced) | Pace is judged on "today"; a skipped day loses nothing, and replaying days would alert on stale data |

## Files

```
backend/requirements.txt                       + apscheduler>=3.10,<4
backend/app/config.py                          ENABLE_SCHEDULER, ENABLE_ADMIN, DAILY_RUN_TIME, CLOSE_RUN_TIME, SCHEDULER_TZ
backend/app/budget_service/daily.py            user_lock, run_daily, run_daily_locked, close_month, sim_today
backend/app/budget_service/simulate.py         advance_days, NotSimulated
backend/app/scheduler.py                       daily_budget_job, month_end_job, build_scheduler
backend/app/api/admin.py                       GET /admin/sandbox, POST /admin/sandbox/advance, GET /admin/sandbox/advance/{id}
backend/app/api/budget.py                      uses daily.user_lock / run_daily; simulated "today"
backend/app/agents/budget_graph.py             close_interrupted_runs also closes graph="simulate"
backend/app/main.py                            admin router; scheduler start/stop in the lifespan
eval/budget_users.py                           seed_profile(..., until=), stable plaid ids
backend/scripts/seed_budget_user.py            --start
backend/scripts/simulate.py                    the CLI
frontend/lib/api.ts, frontend/app/alerts/page.tsx   the simulation panel
.env.example                                   the new settings
```

## Simulation model

**Seeding**
- `seed_profile(session, profile, until: Optional[date] = None) -> str`.
- With `until`, it writes only transactions dated on or before `until`, and sets `user.prefs_json["sim"] = {"profile": profile.id, "date": until.isoformat()}`.
- Every generated transaction gets `plaid_txn_id = f"sim-{profile.id}-{n}"`, where `n` is its index in `transactions(profile)`. That applies with or without `until`, and replaces the factory's per-session counter for seeded users.
- **`seed_budget_user.py --start`:** `--start YYYY-MM-DD`, or `--start default`, which is the day before the first of the run date's month (`overspend-*` → 2026-09-30; accuracy profiles → the day before `profile.today`).
  - With `--start`, it prints `python scripts/simulate.py --user budget-<id> --days 30`.
  - Without `--start`, nothing changes.

**`budget_service/daily.py`**

```python
def user_lock(user_id: str) -> threading.Lock          # moved here from api/budget.py
def sim_today(session, user_id) -> Optional[date]      # prefs_json["sim"]["date"] + 0 days, or None
def run_daily_locked(session_factory, user_id, today, *, model, checkpointer) -> dict
def run_daily(session_factory, user_id, today, *, model, checkpointer) -> Optional[dict]   # None if the lock is held
def close_month(session_factory, user_id, month_start: date) -> int   # placeholder: an agent_runs row, returns its id
```

- `run_daily_locked` picks the sync function (`plaid_sync` if `has_real_plaid_item`, else `None`) and calls `run_budget(..., checkpointer=checkpointer)`. It returns the final state.
- `run_daily` acquires `user_lock(user_id)` without blocking, returning `None` if the lock is held, and releases it in `finally`.
- `close_month` writes `AgentRun(graph="close", status="skipped", finished_at=now, summary_json={"month": "YYYY-MM-01", "note": "period close arrives with TPC-22"})`.

**`budget_service/simulate.py`**

```python
class NotSimulated(Exception): ...
def advance_days(session_factory, user_id, days: int, *, model, checkpointer, job_id: Optional[int] = None,
                 on_day: Optional[Callable[[dict], None]] = None) -> dict
```

The caller holds the user's lock. For each of `days`:
1. `day = sim.date + 1`.
2. If `day.day == 1`, call `close_month(..., month_start=day replaced to the previous month's 1st)` first.
3. Insert that day's generated transactions whose `sim-<profile>-<n>` id isn't already present (idempotent), and commit.
4. `state = run_daily_locked(..., today=day)`.
5. Set `sim.date = day`, and commit.
6. If `job_id` is set, update that `agent_runs` row's `summary_json`:

   ```
   {days_requested, days_done, start_date, date, run_ids, alerts_written}
   ```

7. Call `on_day` with `{date, injected, status, flags, alerts}`.

- An exception on a day stops the loop and is re-raised. `sim.date` stays at the last completed day.
- It raises `NotSimulated` if the user has no `sim`.
- It returns the final progress dict.
- Days after the profile's last generated transaction inject nothing, and still run.

**Simulated "today"**
- `GET /budget` uses `sim_today(user)` when it's set, otherwise the real date.
- `POST /budget/run` with no `date` does the same.

## Scheduler (`app/scheduler.py`)

```python
def eligible_users(session) -> list[str]   # users with a budget_period or a real plaid item, without prefs_json["sim"]; sorted
def daily_budget_job(session_factory, *, today=None, model=None, checkpointer=None) -> dict   # {"ran": [...], "skipped": [...], "failed": [...]}
def month_end_job(session_factory, *, today=None) -> dict                                     # {"closed": [...]}
def build_scheduler() -> BackgroundScheduler   # not started
```

- **`daily_budget_job`:** `today` defaults to the date in `SCHEDULER_TZ`. For each eligible user, in order, it calls `run_daily`.
  - A `None` result (the lock is held) goes in `skipped`.
  - An exception goes in `failed`. It's logged without user data, and the run row is already marked `error` by `run_budget`. The job continues with the next user.
  - The model and checkpointer default to the `budget_hooks` ones.
- **`month_end_job`:** for each eligible user, `close_month(user, first day of the previous month)`.
- **`build_scheduler()`:**
  - `BackgroundScheduler(timezone=SCHEDULER_TZ)`.
  - `daily_budget_job` on a `CronTrigger(hour, minute)` from `DAILY_RUN_TIME`.
  - `month_end_job` on a `CronTrigger(day=1, hour, minute)` from `CLOSE_RUN_TIME`.
  - Both have `id`s `"daily_budget"` and `"month_end"`, `max_instances=1`, `coalesce=True` and `misfire_grace_time=3600`.
  - The jobs are bound with `budget_hooks.session_factory`.
- **Lifespan:** after `close_interrupted_runs`, if `config.ENABLE_SCHEDULER`, build and start the scheduler and log each job's next run time. On shutdown, `scheduler.shutdown(wait=False)`.

**Config** (all from the environment):
- `ENABLE_SCHEDULER` and `ENABLE_ADMIN` are booleans: `"1"`, `"true"` or `"yes"` turn them on, and the default is off.
- `DAILY_RUN_TIME="06:00"`, `CLOSE_RUN_TIME="05:00"`, `SCHEDULER_TZ="America/New_York"`.

## Admin API (`api/admin.py`)

`require_admin()` is a dependency that raises `404 {"detail": "Not found."}` unless `config.ENABLE_ADMIN` (read per request). The routes act as `get_current_user`.

| Route | Response | Errors |
|---|---|---|
| `GET /admin/sandbox` | `{"simulated": bool, "profile": str or null, "date": "YYYY-MM-DD" or null}` | `404` when admin is off |
| `POST /admin/sandbox/advance` | Body `{"days": int}` (1-31). It takes `user_lock` without blocking, checks the user is simulated, and inserts `AgentRun(graph="simulate", status="running", summary_json={days_requested, days_done: 0, start_date, date, run_ids: [], alerts_written: 0})`. It then starts the job thread through `api/budget._spawn` and returns `202 {"job_id"}`. The thread runs `advance_days(job_id=...)`, then sets the row `ok`, or `error` with `summary_json["error"] = type name`, and releases the lock | `404` admin off; `409 {"detail": "Only simulated users can be advanced."}`; `409 {"detail": "A budget run is already in progress."}`; `422` days out of range |
| `GET /admin/sandbox/advance/{job_id}` | `{"job_id", "status", "days_requested", "days_done", "start_date", "date", "run_ids", "alerts_written", "error"}` | `404` admin off, or not this user's `simulate` row |

- **Lock handoff:** the lock is acquired in the route and released by the thread. A failure before the thread starts releases it in the route and returns `500 {"detail": "The simulation couldn't start."}` (the TPC-20 lesson).
- **The startup sweep** (`close_interrupted_runs`) also marks `graph="simulate"` rows left `running` as `error` ("Interrupted").

## CLI (`backend/scripts/simulate.py`)

`main(argv=None, session_factory=None, model=None) -> int`, with `--user ID --days N [--model NAME]`.
- It takes the user lock, and returns 1 with "A budget run is already in progress." if it's held.
- It runs `advance_days` with `on_day` printing `2026-10-12  +2 txns  ok  flags: GENERAL_MERCHANDISE  alerts: 1`.
- It ends with `simulated N day(s): <start> → <end>, R runs, A alerts`.
- It returns 1 for `NotSimulated` ("not a simulated user; seed it with --start") or a failed day, printing the exception type.
- The checkpointer is a `SqliteSaver` on `BUDGET_CHECKPOINT_PATH`, as in the budget CLI.

## Frontend

- **`lib/api.ts`:**
  - `type SimState = {simulated: boolean; profile: string | null; date: string | null}`;
  - `type SimJob = {job_id; status: "running" | "ok" | "error"; days_requested; days_done; start_date; date; run_ids: number[]; alerts_written; error}`;
  - `getSim()`, `advanceSim(days)` (resolves to `{job_id}`) and `getSimJob(id)`.
- **`/alerts`:** on load, `getSim()`. A `404` (admin off) or `simulated: false` shows nothing. Otherwise a panel shows:
  - "Simulated date: Oct 3, 2026";
  - the buttons **Advance 1 day**, **Advance 7 days** and **To month end** (days to the last day of the simulated month, at least 1);
  - while a job runs: "Day 4 of 7…" (polling every 2 s, with the same 10-minute cap and unmount stop as the run button);
  - then "Advanced 7 days to Oct 10: 7 runs, 1 alert written.", or "The simulation stopped on Oct 6 (ConnectionError).". The alert list and the simulated date reload.

  The panel's buttons and the run button are disabled while either is busy.

## Tests

**Test support:** `tests/fakes.py` gains `SchemaReplyModel`. It is a `BaseChatModel` that reads the bound `format` schema:
- for `Diagnosis`, it replies `{"cause": "one_time", "evidence": "One purchase explains it."}`;
- for `Action`, it replies `{"action": "alert", "message": "…"}` when the prompt's "Earlier alerts for this category this month:" line says `none`, and `{"action": "no_op", "message": "Already alerted."}` otherwise.

It records every call.

**Backend** (each with `docs/tests/<module>.md`)
- **`test_budget_users.py`** (added):
  - `until` seeds only up to and including the date, and sets `sim`;
  - no `until` means no `sim` and the full history;
  - the ids are `sim-<profile>-<n>` and stable across two seeds.
- **`test_simulate.py`:**
  - **The done-when:** `overspend-one-time` seeded until Sep 30 and advanced 30 days gives 30 `graph="budget"` runs, one each Oct 1 to Oct 30 (from `agent_runs` and the returned `run_ids`), and `sim.date == 2026-10-30`;
  - each day injects exactly that day's transactions (Oct 12 includes ElectroMart);
  - Sep 30 → Nov 2 records a `close` row for October before the Nov 1 budget run;
  - a day that raises (patch `run_daily_locked` on the 3rd call) stops at the 2nd day, and a second advance resumes from there;
  - advancing past Oct 31 for a profile ending Oct 31 still runs;
  - a user who isn't simulated raises `NotSimulated`;
  - re-inserting an already-injected day adds nothing;
  - the job row's summary updates per day.
- **`test_scheduler.py`:**
  - `eligible_users` includes users with a budget and users with a real item, and excludes simulated and empty users;
  - `daily_budget_job` runs the eligible users for the given `today`;
  - a held lock goes in `skipped`, and a raising run goes in `failed`, with the others still run;
  - `month_end_job` writes one `close` row per eligible user for the previous month;
  - `build_scheduler()` has two jobs with the right cron fields, timezone and options (inspected, not started);
  - the lifespan starts the scheduler only with `ENABLE_SCHEDULER`. Patch `scheduler.build_scheduler` to a recorder, and use `with TestClient(app)`.
- **`test_daily.py`:**
  - `run_daily` returns `None` while the lock is held;
  - `run_daily_locked` syncs only a real item;
  - `close_month` writes the placeholder row;
  - `sim_today` reads `sim`, or gives `None`.
- **`test_budget_api.py`** (added):
  - `GET /budget` uses the simulated date;
  - a run with no date uses the simulated date;
  - the run button gives `409` while the user's lock is held through `daily.user_lock`.
- **`test_admin_api.py`:**
  - with admin off, all three routes give `404`;
  - `GET /admin/sandbox` returns the state;
  - advance gives `202`, then the job reads `ok`, with `days_done == days_requested` and run ids;
  - `409` for a user who isn't simulated, and `409` while locked;
  - `422` for 0 and for 32;
  - another user's job gives `404`;
  - a failure before the thread starts gives `500` and frees the lock;
  - the startup sweep turns a `running` simulate row into `error` ("Interrupted").
- **`test_seed_budget_user.py`** (added): `--start default` seeds until Sep 30 for `overspend-one-time`, sets `sim`, and prints the simulate command.
- **`test_simulate_cli.py`:**
  - the printed per-day lines and summary;
  - exit 1 for a user who isn't simulated;
  - exit 1 when the lock is held.
- **Live** (skipped without Ollama): `overspend-one-time` seeded until Oct 9 and advanced 7 days with the real model gives 7 runs. No two overspend alerts for GENERAL_MERCHANDISE in October share a severity, and there's at least one `high` alert, dated Oct 12 or later.

**Frontend**
- **`api.test.ts`:** the three admin calls' URLs and bodies.
- **`app/alerts/page.test.tsx`:**
  - no panel when `getSim` rejects with 404, or says not simulated;
  - the panel shows the date, and **Advance 7 days** polls to "Day 7 of 7", shows the result line, and reloads alerts and the simulated state;
  - a 409 shows its message;
  - the run button is disabled while a simulation runs.

## Docs

- **Architecture:**
  - 4.1: the simulation panel;
  - 5: the `simulate` and `close` run graphs, and `prefs_json["sim"]`;
  - 9: the admin routes;
  - 12: "Sandbox time travel", replaced with how it's built;
  - 13: `scheduler.py`, `budget_service/daily.py` and `simulate.py`;
  - 6.2 and 6.6: who calls `run_daily`.
- **Decision-log rows (2026-10-29)** for each decision above.
- **README:** "Simulate a month" (seed with `--start default`, then the CLI, or `ENABLE_ADMIN=1` and the panel), and "Run the agent every morning" (`ENABLE_SCHEDULER=1`).
- **`.env.example`:** the five settings with their defaults.

## Risks

| Risk | Mitigation |
|---|---|
| A 30-day simulation takes minutes with the real model | Runs in the background with progress. The done-when test uses the fake model. The CLI prints each day as it goes |
| The scheduler fires while the user simulates or clicks Run | The one per-user lock. A busy user is skipped for that day, and the next day runs normally |
| A server down at 06:00 skips the day | Corrected after review: APScheduler's misfire grace only covers a running server. At startup, if today's slot has passed, it runs once for users not yet checked today (and the month-end slot on the 1st for users not yet closed). Earlier days are never replayed |
| APScheduler in-process with more than one worker would double-run | One worker is already required (chat and budget locks). Documented |
