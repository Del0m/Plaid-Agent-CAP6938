# Tests: `backend/tests/test_budget_alerts.py`

These tests cover `app/budget_service/alerts.py`, the user's side of the budget agent (TPC-20). `decide_alert`:
- accepts or dismisses an alert;
- writes that decision to the advice rows it came from, as their outcome, which is the agent's memory;
- resumes the budget run if it was paused on that alert, waiting for approval of a budget change.

The decision is committed before the resume, so a failed resume never loses the user's click. `awaiting_approval` lists the alerts paused runs are waiting on, and `has_real_plaid_item` says whether a user has anything to sync.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_alerts.py -v
```

## Shared setup

- `plain_alert(db, tmp_path)` runs the one-time planted user with a scripted `alert` action, which leaves a pending alert and no paused run.
- `paused(db, tmp_path, ck)` runs the gym user with a scripted `adjust_next_budget` and an in-memory checkpointer. The run pauses, waiting on that alert. It returns the user, the alert id and the run id.
- `get(db, cls, id)` and `outcomes(db, alert_id)` read rows back in a fresh session.

---

## `test_accept_sets_status_and_outcome`

**What it does:** Accepting a pending alert makes it `accepted`, and its advice row's outcome `accepted`.

**Why it's there:** The outcome is what the next run's memory reads, so the agent knows the user took that advice.

## `test_dismiss_sets_status_and_outcome`

**What it does:** Dismissing does the same with `dismissed`.

**Why it's there:** A dismissed proposal is what stops the agent proposing it again (the decide prompt's memory rule).

## `test_already_decided`

**What it does:** Deciding an alert a second time raises `AlertAlreadyDecided`, and the first decision stands.

**Why it's there:** A double click, or a second tab, must not flip a decision a paused run may already have acted on. The API turns this into `409`.

## `test_other_user_or_unknown_not_found`

**What it does:** Another user's alert, or an unknown id, raises `AlertNotFound` and changes nothing.

**Why it's there:** One user can't decide another's alerts. The API turns this into `404`, without saying whether the alert exists.

## `test_accept_resumes_paused_run`

**What it does:** Accepting the alert a paused run waits on finishes the run `ok`, with the decision in its summary.

**Why it's there:** It's the human-in-the-loop path end to end: the click continues the `interrupt()`.

## `test_dismiss_resumes_too`

**What it does:** Dismissing that alert also resumes and finishes the run, recording `dismissed`.

**Why it's there:** A "no" is a decision too. The run must not wait forever for an accept that won't come.

## `test_resume_failure_keeps_decision`

**What it does:** When the resume raises, the alert is still returned `accepted` and saved, and the run stays `awaiting_user`.

**Why it's there:** The user's decision is the thing that must not be lost. A broken checkpoint is logged, and the decision still counts.

## `test_missing_checkpoint_still_decides`

**What it does:** With a checkpointer that doesn't have the paused run (as if its file were deleted), nothing counts as waiting, deciding still works, and the run stays `awaiting_user`.

**Why it's there:** Checkpoints live in a separate file that can go missing. Alerts must stay decidable regardless.

## `test_awaiting_approval_ids`

**What it does:** It lists the alert a paused run waits on, and nothing once it's decided.

**Why it's there:** `GET /alerts` uses it to mark which alerts are holding a run, so the page can say "the agent is waiting for your decision".

## `test_has_real_plaid_item`

**What it does:** A seeded user (placeholder token `"x"`) has no real Plaid item. After the token changes, it does.

**Why it's there:** `POST /budget/run` only syncs users with a real item. A seeded user's fake token would just record a sync error on every run.

## `test_failed_resume_keeps_run_awaiting`

**What it does:** When the resume fails before anything runs (the thread is still paused), the decision is saved and the run stays `awaiting_user`.

**Why it's there:** It was found in the TPC-20 review. The spec said both "a failed resume leaves the run waiting" and "a failed resume marks it error". The rule now: a run still paused keeps waiting, along with its other proposals, and can be resumed again. Only a run that got past the pause and then failed is `error`.

## `test_failed_resume_retried_on_next_load`

**What it does:** Makes the resume fail on accept, so the alert is accepted but the run stays `awaiting_user`. With the resume working again, `awaiting_approval` (what `GET /alerts` calls) returns nothing, and the run is `ok` with the decision recorded.

**Why it's there:** The decision is committed before the resume, and an accepted alert can't be decided again (409). So a resume that failed once, on "database is locked" or a model error, left the run waiting forever on an alert nobody could click. The next load now finishes it. The review on #13 found it.

## `test_decision_just_before_the_pause_is_picked_up`

**What it does:** Wraps `interrupt` so the alert is dismissed just before the run pauses, while its row still says `running`, so `decide_alert` resumes nothing. The run still ends `ok`.

**Why it's there:** `log_advice` commits the alerts a few steps before the run is marked `awaiting_user`. A click in that window found no paused run, and the run then paused on an alert that was already decided. `run_budget` now checks for decided proposals right after marking the run. The review on #13 found it.

## `test_two_decisions_before_a_resume_both_count`

**What it does:** Accepts a paused run's alert directly in the database, as a request whose resume never ran would, then loads the alerts. The run finishes with the decision recorded.

**Why it's there:** `await_approval` used to take decisions only from resume payloads. Two decisions for one thread could each resume the same checkpoint, and one would be lost. The run now reads every decision from the alert rows, and resumes for one run take a lock, so a decision counts however it arrives. The review on #13 found it.

---

## Not covered

- **Two requests deciding one alert at the same instant.** `decide_alert` sets the status with one conditional update (`WHERE status = 'pending_user'`), so only one can win. A test can't make two requests overlap between the read and the write without real concurrency, so the race itself isn't tested.
