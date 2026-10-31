# TPC-20: Alerts UI and advice memory

Jira: TPC-20 (epic TPC-4, M3). Branch: `TPC-20`, cut from `TPC-19`.

## Goal

Close the loop between the budget agent (TPC-19) and the user:
- **Memory.** The agent remembers what it already said and what the user decided. It no longer repeats an alert or re-proposes advice the user dismissed.
- **Approval.** A proposal that would change the budget pauses the run with LangGraph `interrupt()` until the user decides (architecture 6.6).
- **API.** `/budget`, `/budget/run`, `/alerts` and accept/dismiss.
- **Frontend.** An Alerts page, and budget pace bars on the dashboard.

**Done when:** a second run on the same overspend doesn't create a duplicate alert. It is checked live against `qwen2.5:14b`, and the demo shows it in the UI.

**Out of scope:**
- applying an accepted `adjust_next_budget` to next month's limit (TPC-22's close; TPC-20 only records the decision);
- the scheduler (TPC-21);
- authentication (the API stays the single dev user, now configurable);
- the TPC-19 deferred minors.

## Decisions

| Decision | Why |
|---|---|
| `interrupt()` pauses a run only when it wrote an `adjust_next_budget` alert. `alert` and `suggest_cuts` alerts don't pause | Architecture 6.6: what changes the budget waits for the user. Informational alerts have nothing to approve, and a paused run per day would pile up |
| Deduplication is the model's decision. Code gives it the category's alerts this month and its recent advice with outcomes as facts, and the prompt says to `no_op` when nothing has got worse | The user's choice. It keeps one mechanism (the LLM decides) while making the duplicate obvious from the facts. The live test checks the second run |
| The memory facts carry action, severity, status, outcome and dates, never the old message text | Minimization: the model needs to know it already alerted, not what it wrote |
| `advice_log.alert_id` (nullable FK) | A decision on an alert has to reach the advice row it produced |
| Decisions live in `budget_service/alerts.py` (`decide_alert`), not in the routes | The scheduler (TPC-21) and close (TPC-22) reuse it. The routes stay thin |
| The decision is committed before the run is resumed. A failed resume leaves the decision saved and the run `awaiting_user` | The user's click must never be lost to a resume error |
| `GET /budget` computes pace live with `evaluate` and writes nothing | Fresh right after a sync. Without a scheduler, stored values would be "whenever the CLI last ran" |
| `DEV_USER_EMAIL` comes from the environment (default `dev@localhost`), and `POST /budget/run` runs the agent from the UI | The demo: run the API as a seeded planted user and click Run. TPC-21's scheduler calls the same `run_budget` |
| One budget run in flight per user (in-process lock, `409`) | Same reason as chat: a run holds the local model for tens of seconds |
| `POST /budget/run` starts the run in the background and returns `202` with the run id; the page polls `GET /budget/runs/{id}` | The Next.js `/api` proxy drops a response after 30 s without a byte (`api/chat.py`), and a run's two LLM calls per flag can stay silent longer than that |
| Budget checkpoints go to their own SQLite file, `data/budget_checkpoints.db`, with thread `budget-<run_id>` | A paused run survives a backend restart. Kept apart from chat's threads |

## Files

```
backend/alembic/versions/<rev>_advice_alert_link.py   advice_log.alert_id
backend/app/config.py                                 DEV_USER_EMAIL, BUDGET_CHECKPOINT_PATH
backend/app/api/deps.py                               get_current_user uses config.DEV_USER_EMAIL
backend/app/db/models.py                              AdviceLog.alert_id
backend/app/privacy/gateway.py                        diagnose: this_month_alerts, past_advice
backend/app/agents/budget_llm.py                      decide rules for memory
backend/app/agents/budget_graph.py                    memory facts, await_approval, checkpointer, resume_budget_run
backend/app/budget_service/alerts.py                  decide_alert
backend/app/api/budget.py                             GET /budget, POST /budget/run
backend/app/api/alerts.py                             GET /alerts, POST /alerts/{id}/accept|dismiss
backend/app/main.py                                   include the two routers
frontend/lib/api.ts                                   types and calls
frontend/components/BudgetPaceBar.tsx, AlertCard.tsx
frontend/app/alerts/page.tsx                          the Alerts page
frontend/app/dashboard/page.tsx                       Budget card, Alerts link
```

## Graph changes (`agents/budget_graph.py`)

```
… → diagnose → decide_action → log_advice ─┬─(any adjust_next_budget alert)─► await_approval → persist_state
                                           └────────────────────────────────► persist_state
```

**Memory facts.** In `diagnose`, for each flag, code reads:
- `this_month_alerts`: the user's `overspend` alerts for the category in this period, oldest first, as `{day, severity, action, status}`;
- `past_advice`: the category's `advice_log` rows created in the 90 days before today, newest first, at most 5, as `{month, action, outcome}`. The action comes from the linked alert; rows with no `alert_id` are skipped.

Both go to the gateway as new keyword arguments, `build_context("diagnose", ..., this_month_alerts=..., past_advice=...)`, and appear in the data under those keys. They are added to `DIAGNOSE_FIELDS`. Dates become day numbers and month names, and no message text is included.

**Decide prompt** gains two rules:
- If `this_month_alerts` already has an alert for this category and this flag's severity isn't higher than the highest one there, choose `no_op`. (`high` is above `medium`; the prompt states the flag's severity.)
- Don't choose `adjust_next_budget` when `past_advice` has a dismissed `adjust_next_budget` for this category.

`decide_messages` adds the line `Severity of this flag: high|medium`.

**`log_advice`** sets `AdviceLog.alert_id` to the alert it just wrote.

**`await_approval`**:

```python
waiting = [ids of this run's alerts whose action is adjust_next_budget]
decisions = dict(state.get("decisions", {}))
while set(waiting) - set(decisions):
    got = interrupt({"waiting_for": sorted(set(waiting) - set(decisions))})   # resume value: {alert_id: "accepted"|"dismissed"}
    decisions.update({int(k): v for k, v in got.items()})
return {"decisions": decisions}
```

- `BudgetState` gains `decisions: dict[int, str]`.
- `persist_state` adds `decisions` to the summary (string keys).

**Checkpointer and threads**
- `build_budget_graph(..., checkpointer=None)` compiles with it.
- `run_budget(..., checkpointer=None)`:
  - with a checkpointer, it invokes with `config={"configurable": {"thread_id": f"budget-{run_id}"}}`;
  - when the result contains `__interrupt__`, it sets the run's `status = "awaiting_user"` and returns the state, with `status` set to `"awaiting_user"`.
  - Without a checkpointer, `await_approval` is skipped (routed straight to `persist_state`), so the CLI and tests without one behave as in TPC-19.
- **`resume_budget_run(session_factory, run_id, alert_id, decision, *, checkpointer, model=None) -> dict`**:
  - rebuilds the graph;
  - invokes `Command(resume={alert_id: decision})` on thread `budget-<run_id>`;
  - if it pauses again (another alert still waiting), leaves the run `awaiting_user`; otherwise `persist_state` finishes it `ok`;
  - is wrapped in the same error handling as `run_budget` (marks `error`, re-raises).
- **The gateway** isn't used after `log_advice`, so a resume builds a fresh one with a new pseudonym; no prompt is built on resume.
- **The CLI** (`main`) uses a `SqliteSaver` on `config.BUDGET_CHECKPOINT_PATH` and prints `status: awaiting_user` with the alert ids it's waiting for.

## `budget_service/alerts.py`

```python
class AlertNotFound(Exception): ...
class AlertAlreadyDecided(Exception): ...

def decide_alert(session_factory, user_id: str, alert_id: int, decision: Literal["accepted", "dismissed"],
                 *, checkpointer=None, model=None) -> Alert
```

1. Load the alert. If it's missing or belongs to another user, raise `AlertNotFound`. If its status isn't `pending_user`, raise `AlertAlreadyDecided`.
2. Set `alert.status = decision` and set `outcome = decision` on every `AdviceLog` with `alert_id == alert.id`. Commit.
3. **Is a paused run waiting on it?** Find the `AgentRun` (`graph="budget"`, `status="awaiting_user"`, same user) whose checkpoint is waiting for this alert id (read with `graph.get_state(config).tasks[*].interrupts[*].value["waiting_for"]`). If there's one and a `checkpointer` was given, call `resume_budget_run`.
4. If the resume raises, log it locally (no user data) and return the saved alert anyway. The run stays `awaiting_user`.

`awaiting_approval(session_factory, user_id, *, checkpointer) -> set[int]` gives the alert ids that paused runs are waiting on, for `GET /alerts`.

## API

Both routers use `get_current_user`. Test hooks are module-level, like `api/chat.py`:
- `session_factory = SessionLocal`;
- `make_model = get_chat_model`;
- `get_checkpointer()`, an lru-cached `SqliteSaver` on `BUDGET_CHECKPOINT_PATH`.

| Route | Response | Errors |
|---|---|---|
| `GET /budget` | `{"period": {"start", "end"} or null, "days_elapsed", "days_in_period", "lines": [{"category", "kind", "limit_cents", "spent_cents", "pace", "flagged", "reason"}], "unbudgeted": [{"category", "spent_cents"}]}`. It uses the user's latest period if it covers today, and `evaluate(lines, load_txns(period.start..today), today)`. Lines come in `evaluate`'s order. With no period covering today: `period: null` and empty lists | none |
| `POST /budget/run` | Body `{"date": "YYYY-MM-DD"}`, optional (default today). It takes the user's lock (non-blocking), inserts nothing itself, and schedules a FastAPI background task. The task runs `run_budget(session_factory, user.id, date, model=make_model(), sync_fn=plaid_sync if the user has a PlaidItem whose access token isn't the seed placeholder else None, checkpointer=get_checkpointer())` and releases the lock in `finally`. `run_budget` gains `on_start: Callable[[int], None]`, called with the new run id right after the `agent_runs` row is committed; the route waits for that id (a `threading.Event`, at most 5 s) and returns `202 {"run_id"}` | `409 {"detail": "A budget run is already in progress."}` while the lock is held; `422` for a bad date |
| `GET /budget/runs/{run_id}` | `{"run_id", "status", "alert_ids", "waiting_for", "error"}`. `status` is the `agent_runs` status (`running`, `ok`, `no_budget`, `period_ended`, `awaiting_user`, `error`). `alert_ids` are the alerts whose advice rows carry this `run_id`. `waiting_for` is those still waited on by the paused run. `error` is the exception type for an `error` run, else null | `404` if it isn't this user's run |
| `GET /alerts` | `?status=pending_user` or `all` (default). Newest first by `created_at`, then `id`. Each alert: `{"id", "kind", "category", "severity", "cause", "action", "message", "proposed_limit_cents", "status", "created_at", "period_start", "awaiting_approval"}` | `422` for another status value |
| `POST /alerts/{id}/accept`, `POST /alerts/{id}/dismiss` | `decide_alert(...)`, then the alert in the same shape | `404 {"detail": "Alert not found."}`; `409 {"detail": "This alert was already decided."}` |

- **The seed placeholder token:** a user is synced only if one of their items has `access_token_enc != "x"`. `"x"` is what `tests/factories.add_user` stores; it lives in a named constant `SEED_TOKEN` in `budget_service/alerts.py`, next to the comment explaining it.
- **`get_current_user`:** finds the user by `config.DEV_USER_EMAIL`. It still creates the user if missing, but only when the email is the default `dev@localhost`; any other missing email gives `404 {"detail": "Unknown DEV_USER_EMAIL user."}`.

## Frontend

Before writing code, read the relevant guides in `frontend/node_modules/next/dist/docs/` (as `frontend/AGENTS.md` asks). Follow the existing patterns: `"use client"` pages, Tailwind, and typed calls in `lib/api.ts` through `request()` and `errorMessage()`.

- **`lib/api.ts`** gains:
  - the types `Budget`, `BudgetLine`, `Alert` and `RunResult`;
  - the calls `getBudget()`, `runBudget(date?: string)` (resolves to `{run_id}`), `getRun(id)`, `getAlerts(status?: "pending_user" | "all")`, `acceptAlert(id)` and `dismissAlert(id)`.
- **`components/BudgetPaceBar.tsx`** (`{line: BudgetLine, daysElapsed, daysInPeriod}`):
  - label, `$spent of $limit`, a pace figure, and "(fixed)" for fixed lines;
  - a fill of `min(spent/limit, 1)` (an empty bar for a limit of 0), and a "today" marker at `daysElapsed/daysInPeriod`;
  - colour: amber when `reason === "pace"`, red when `reason === "over_limit"`, neutral otherwise;
  - the bar has `role="meter"` with `aria-valuenow` set to the percentage spent.
- **`components/AlertCard.tsx`** (`{alert, onAccept, onDismiss, busy}`):
  - category label (`GENERAL_MERCHANDISE` shows as "General merchandise"), a severity badge, the cause and the message;
  - "Proposed limit next month: $X" only for `adjust_next_budget`, and "The agent is waiting for your decision" when `awaiting_approval`;
  - Accept and Dismiss buttons only when `status === "pending_user"`, disabled while `busy`;
  - decided alerts show their outcome.
- **`app/alerts/page.tsx`:**
  - a "Run budget check" button with a date input (default today) and a "Running…" state. After `runBudget` returns the run id, it polls `getRun(id)` every 2 s until the status isn't `running`;
  - after a run: "M alert(s) written", "No new alerts", "Waiting for your approval on M proposal(s)", or "The run failed (<error>)". Errors come through `errorMessage` (409 → "A budget run is already in progress.");
  - **Pending** cards, then **History**. It reloads after a run or a decision.
- **`app/dashboard/page.tsx`:**
  - a **Budget** card above Accounts, with one bar per line and unbudgeted categories in one line beneath;
  - with `period: null`: "No budget yet. Run a budget check from Alerts.";
  - the header gets an **Alerts** link, showing the pending count when above 0 ("Alerts (2)").

## Tests

**Backend** (each with its `docs/tests/<module>.md`)

**`test_budget_graph.py`** (added)
- **Memory facts:** after an earlier run's alert, the diagnose prompt carries `this_month_alerts` (day, severity, action, status) and `past_advice` (month, action, outcome) for that category only, with no message text.
- **Duplicate:** a second run scripted to `no_op` writes no new alert.
- **Advice link:** the advice row carries the alert's id.
- **Paused run:**
  - an `adjust_next_budget` run with an in-memory checkpointer ends `awaiting_user`, waiting for that alert id;
  - `resume_budget_run(..., "accepted")` finishes it `ok`, with `decisions` in the summary.
- **Two adjust alerts** (two planted overspend categories, built in the test): resuming in reverse order pauses after the first and finishes after the second.
- **Never pauses:**
  - a run with only `alert` actions never pauses, even with a checkpointer;
  - a run without a checkpointer never pauses (TPC-19 behaviour).
- **Live, the done-when** (skipped without Ollama): `overspend-one-time` on Oct 20 gives 1 alert. Running again on Oct 20 with the same model gives still 1 alert.

**`test_budget_alerts.py`**
- **Recording:** accept and dismiss set the status and the outcome on the linked advice rows.
- **Errors:** an already-decided alert raises `AlertAlreadyDecided`; another user's alert or an unknown id raises `AlertNotFound`.
- **Resuming:** accepting the alert a paused run waits on resumes it to `ok`.
- **A failed resume:** with `resume_budget_run` patched to raise, the decision is saved, the run stays `awaiting_user`, and the alert is returned.
- **`awaiting_approval`:** gives the waited-on ids.

**`test_budget_api.py`** (`client` fixture; hooks patched to the test database, a scripted model and an in-memory saver)
- **`GET /budget`:**
  - the lines match `evaluate` for a seeded planted user on a fixed date (the date is patched);
  - `period: null` with no budget;
  - nothing is written.
- **`POST /budget/run` and `GET /budget/runs/{id}`** (TestClient runs background tasks before returning):
  - `202` with a run id, and the run then reads `ok` with its alert ids;
  - `409` while the lock is held;
  - a raising model gives a run with status `error` and the exception type;
  - a seeded user isn't synced; a user with a real-looking item is, through a patched `plaid_sync`;
  - an adjust gives `awaiting_user` and `waiting_for`;
  - another user's run gives `404`.
- **`GET /alerts`:** newest first, the `status` filter, `422` for a bad status, and `awaiting_approval`.
- **Accept and dismiss:** `200` with the updated alert; `404` for another user's; `409` when already decided.
- **`DEV_USER_EMAIL`:** set to a seeded user's email, the routes act as that user; an unknown non-default email gives `404`.

**Migration check:** upgrade, `alembic check`, downgrade, upgrade.

**Frontend** (Vitest + React Testing Library, documented in `docs/tests/<name>.test.md`)
- **`api.test.ts`:** the URLs and bodies of the six calls; a 409 maps to its detail message.
- **`BudgetPaceBar.test.tsx`:**
  - fill width and the today marker position;
  - amber for pace, red for over-limit;
  - capped at 100% with the true amount shown;
  - a limit of 0.
- **`AlertCard.test.tsx`:**
  - Accept and Dismiss call back with the id;
  - no buttons on a decided alert;
  - the proposed limit only for adjust;
  - the waiting note only when `awaiting_approval`.
- **`alerts.test.tsx`** (the page, with `fetch` mocked and fake timers): loads pending and history, a run polls until it's done then shows its result line and reloads, accepting reloads, and a 409 run shows the message.

## Docs

- **Architecture:**
  - 5: `advice_log.alert_id`, and the `awaiting_user` run status;
  - 6.2 and 6.6: memory facts, `await_approval`, the checkpoint file, resuming;
  - 9: the new routes;
  - 4.1: the Alerts page and pace bars;
  - 12: the UI demo.
- **Decision-log rows (2026-10-28):**
  - only budget-changing proposals pause;
  - deduplication is the model's choice from memory facts (with the live result);
  - live pace on the dashboard;
  - `DEV_USER_EMAIL` and the run button for the demo until TPC-21.
- **README:** the UI demo:

  ```bash
  cd backend && ../venv/bin/python scripts/seed_budget_user.py --profile overspend-one-time --replace
  DEV_USER_EMAIL=overspend-one-time@eval.local ../venv/bin/uvicorn app.main:app
  # frontend: npm run dev, open /alerts, run on 2026-10-20, accept or dismiss, run again
  ```

## Risks

| Risk | Mitigation |
|---|---|
| The model alerts again despite the memory facts | The facts state the earlier alert and its severity outright, and the prompt rule is explicit. The live test checks it. If it fails, tune the wording (a ruling), and keep the decision with the model, as chosen |
| A paused run is never resumed (the user never decides) | It stays `awaiting_user`. The alert is still visible as pending. TPC-22's close treats undecided proposals as dismissed |
| A run takes tens of seconds | It runs in the background and the page polls, so the 30 s proxy limit never applies. The per-user lock stops a second run. A run without flags makes no LLM call |
| The checkpoint file grows with paused runs | One small thread per run that pauses (adjust proposals only). Cleanup can come with TPC-21 |
