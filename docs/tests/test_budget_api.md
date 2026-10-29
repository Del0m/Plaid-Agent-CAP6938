# Tests: `backend/tests/test_budget_api.py`

These tests cover the budget agent's API (TPC-20):
- **`api/alerts.py`:** `GET /alerts`, `POST /alerts/{id}/accept` and `POST /alerts/{id}/dismiss`;
- **`api/budget.py`:** `GET /budget`, `POST /budget/run` and `GET /budget/runs/{id}`;
- **`get_current_user`:** the configurable `DEV_USER_EMAIL`.

The routes read their database, model and checkpointer from `api/budget_hooks.py` at call time, so the tests swap those in one place.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_api.py -v
```

## Shared setup

- The `client` fixture (`conftest.py`) points the API's sessions at the test database.
- **The `api` fixture:**
  - points `budget_hooks` at the test database, one `InMemorySaver`, and a queue of scripted models (`api.models`; empty means a model with no replies);
  - puts the prompt audit log in `tmp_path`.
- **`api.seed(pid)`** seeds a planted user and sets `DEV_USER_EMAIL` to its `<pid>@eval.local`, so the API acts as that user.
- **`api.run(uid, *replies, checkpointer=None)`** runs the budget agent directly on Oct 20 2026 with scripted replies.
- **The fixture makes `POST /budget/run` run inline** (it patches `budget._spawn`). The in-memory test database is a single SQLite connection, and a background thread and the polling requests can't use it at the same time. The threaded path is exercised against a real server in the TPC-20 demo.
- **`start(api, date)`** posts a run and returns its id; **`wait(api, run_id)`** polls `GET /budget/runs/{id}` until it isn't `running`.

---

## `test_alerts_newest_first_and_filter`

**What it does:** After two runs (the first alert accepted, the second pending), `GET /alerts` lists the newer one first. `?status=pending_user` gives only the pending one, with every field: overspend, general merchandise, `high`, `one_time`, `alert`, period start 2026-10-01, no proposed limit, not awaiting approval.

**Why it's there:** The Alerts page shows pending alerts first and history below, both from this list.

## `test_alerts_bad_status_422`

**What it does:** `?status=nope` gives `422`.

**Why it's there:** A typo in the filter should fail, not silently return everything.

## `test_alerts_only_current_user`

**What it does:** With alerts for two users, the current user sees only their own.

**Why it's there:** It's the privacy boundary for the list (plan Review Focus 5).

## `test_alerts_awaiting_approval_flag`

**What it does:** The gym user's `adjust_next_budget` alert, which holds a paused run, is listed with `awaiting_approval: true` and a proposed limit of $135.

**Why it's there:** The page tells the user the agent is waiting on them, and shows the number they'd be accepting.

## `test_accept_200_returns_alert`

**What it does:** Accepting returns `200` and the alert, now `accepted`.

**Why it's there:** The page updates the card from the response.

## `test_dismiss_200`

**What it does:** Dismissing returns `200` and the alert, now `dismissed`.

**Why it's there:** The other half of the decision, through the API.

## `test_accept_resumes_paused_run`

**What it does:** Accepting the alert a paused run waits on finishes the run (`ok`), and the alert is no longer marked as awaiting approval.

**Why it's there:** It's the human-in-the-loop path through HTTP: the click resumes the `interrupt()`.

## `test_accept_other_users_404`

**What it does:** Accepting another user's alert gives `404` "Alert not found.".

**Why it's there:** One user can't decide another's alerts, or learn whether an id exists.

## `test_accept_twice_409`

**What it does:** Dismissing an already-accepted alert gives `409` "This alert was already decided.".

**Why it's there:** A double click or a second tab can't flip a decision a paused run may already have acted on.

## `test_dev_user_email_override_acts_as_user`

**What it does:** With `DEV_USER_EMAIL` set to the planted user, `GET /alerts` shows its alert. Set back to the default, it shows none.

**Why it's there:** It's how the UI demo runs as a seeded user until there's real authentication.

## `test_unknown_dev_user_email_404`

**What it does:** A `DEV_USER_EMAIL` that matches no user gives `404` "Unknown DEV_USER_EMAIL user." and creates nothing.

**Why it's there:** A typo in the setting should be obvious, not quietly create an empty user and show an empty app.

## `test_default_dev_user_still_created`

**What it does:** With the default `dev@localhost`, the user is created on first request, as before.

**Why it's there:** Plaid linking and the dashboard rely on the dev user existing on a fresh database.

## `test_budget_lines_match_evaluate`

**What it does:** After a run creates October's budget, `GET /budget` on Oct 20 returns the period, day 20 of 31, and one line per budgeted category. Their spent, limit, flag and reason equal `evaluate` over the same transactions.

**Why it's there:** The dashboard's pace bars must show the same numbers the agent judges by, computed live.

## `test_budget_no_period_null`

**What it does:** With no budget yet, `period` is `null` and the lists are empty.

**Why it's there:** The dashboard shows "No budget yet" instead of an error.

## `test_budget_period_over_is_null`

**What it does:** On Nov 3, with only an October budget, `period` is `null`.

**Why it's there:** November's spending must not be drawn against October's limits (plan Review Focus 3). Opening November is period close's job.

## `test_budget_writes_nothing`

**What it does:** A `GET /budget` leaves every line's stored spent and pace, and the run and alert counts, unchanged.

**Why it's there:** Reloading the dashboard is safe, and only a real run updates the stored budget.

## `test_run_202_then_ok`

**What it does:** `POST /budget/run` answers `202` with a run id. The run then reads `ok`, with one alert and no error.

**Why it's there:** The page starts a run, then polls it, because the Next.js proxy drops a request after 30 s of silence.

## `test_run_409_while_locked`

**What it does:** While the user's run lock is held, a second run gets `409` "A budget run is already in progress.".

**Why it's there:** A run holds the local model for tens of seconds. A double click must not start two.

## `test_run_error_status`

**What it does:** When the model raises, the run reads `error` with `ConnectionError`, and the next run can start (the lock was released).

**Why it's there:** The page can say the run failed and why, and a failure doesn't lock the user out.

## `test_run_syncs_only_real_items`

**What it does:** A seeded user isn't synced. After its item gets a real-looking token, the next run syncs it.

**Why it's there:** A seeded user's placeholder token would only record a sync error on every run, and real users must still be synced before the check.

## `test_run_awaiting_user`

**What it does:** The gym user's run reads `awaiting_user`, waiting on its one alert.

**Why it's there:** The page shows "Waiting for your approval" from this.

## `test_run_other_users_404`

**What it does:** Another user's run id gives `404` "Run not found.".

**Why it's there:** One user can't watch another's runs.

## `test_run_bad_date_422`

**What it does:** `{"date": "2026-13-01"}` gives `422`.

**Why it's there:** A bad date fails before anything runs.

## `test_run_lock_released_when_start_fails`

**What it does:** When starting a run fails (here, looking up the simulated date raises, for a run with no date), the request gets `500` "The budget run couldn't start.", and the user's run lock is free again.

**Why it's there:** It was found in the TPC-20 review. The lock used to stay held, so every later run got `409` "already in progress" until the server restarted.

## `test_run_that_never_started_is_500`

**What it does:** When the run fails before writing its run row (the model can't be created), the request gets `500` instead of `202` with no id.

**Why it's there:** It was found in the TPC-20 review. A `202` with no id made the page say the run "hasn't reported back yet", for a run that never existed.

## `test_budget_ended_period_reported`

**What it does:** On Nov 3, with only an October budget, `period` is `null` and `ended_period` is October's dates. On Oct 20, `ended_period` is `null`.

**Why it's there:** It was found in the TPC-20 review. The dashboard used to say "No budget yet" for a budget that exists but has ended, which pointed the user at a run that can't help. It now says when the budget ended.

## `test_startup_closes_interrupted_runs`

**What it does:** On startup, a budget run still marked `running` becomes `error` ("Interrupted"), and an `awaiting_user` run is left alone.

**Why it's there:** It was found in the TPC-20 review. A run's thread dies with the server, and its row would otherwise say `running` forever, which keeps the page polling. A paused run doesn't need its thread; it lives in the checkpoint.

## The simulated "today" (TPC-21)

`seed_until(api, pid, until)` seeds a planted user as simulated from `until` and makes the API act as that user.

## `test_budget_uses_sim_date`

**What it does:** For a user simulated at Oct 19, `GET /budget` reports October's budget on day 19, even though the real date is patched to Nov 30.

**Why it's there:** The dashboard of a simulated user follows the simulation, not the wall clock.

## `test_run_default_date_is_sim_date`

**What it does:** Running a check with no date, for a user simulated at Oct 19, runs it for Oct 19 (October's budget is created).

**Why it's there:** The run button's blank date means "today", and a simulated user's today is the simulated date.

## `test_budget_sim_date_past_period`

**What it does:** For a simulated date of Nov 2 with only an October budget, `period` is `null` and `ended_period` is October.

**Why it's there:** Simulating past the month end must show the same "your budget ended" message as real time (TPC-20). It mustn't judge November against October.

## `test_startup_closes_interrupted_simulation`

**What it does:** On startup, a simulation job still marked `running` becomes `error` ("Interrupted").

**Why it's there:** A simulation's thread dies with the server, like a run's. Otherwise the Alerts panel would poll it forever.
