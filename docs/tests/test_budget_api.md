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
