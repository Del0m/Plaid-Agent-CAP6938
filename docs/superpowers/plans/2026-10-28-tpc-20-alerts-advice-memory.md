# TPC-20 Alerts UI and Advice Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the budget agent memory of its own alerts and advice, pause budget-changing proposals with LangGraph `interrupt()` until the user decides, and expose budget, runs and alerts through the API and a new Alerts page plus dashboard pace bars.

**Architecture:**
- **The graph** (`agents/budget_graph.py`) reads this month's alerts and recent advice into the diagnose facts. Through the gateway, it gains an `await_approval` node that checkpoints into SQLite.
- **`budget_service/alerts.py`** records decisions and resumes paused runs.
- **The API:** `api/budget.py` and `api/alerts.py` are thin routes.
- **The frontend:** Next.js gets an `/alerts` page and a Budget card on the dashboard.

**Tech Stack:** Python 3.12, FastAPI (BackgroundTasks), LangGraph 1.2 (`interrupt`, `Command`, `SqliteSaver`, `InMemorySaver`), SQLAlchemy 2.1, Alembic, pytest; Next.js 16.4, React 19, Tailwind 4, Vitest and React Testing Library.

**Spec:** `docs/superpowers/specs/2026-10-28-tpc-20-alerts-advice-memory-design.md`

## Global Constraints

- **Commands:**
  - backend tests run from `backend/` with `../venv/bin/python -m pytest`;
  - Alembic runs from `backend/`;
  - frontend tests run from `frontend/` with `npm test`, and lint with `npm run lint`.

  No new dependencies.
- **Next.js:** before writing frontend code, read the relevant guides in `frontend/node_modules/next/dist/docs/01-app/` (as `frontend/AGENTS.md` asks).
- **Privacy:** the memory facts carry no message text. User data reaches a prompt only through `PrivacyGateway.build_context`.
- **Values:**
  - thread id `f"budget-{run_id}"`;
  - `BUDGET_CHECKPOINT_PATH` defaults to `backend/data/budget_checkpoints.db`;
  - `DEV_USER_EMAIL` defaults to `"dev@localhost"`;
  - `SEED_TOKEN = "x"`;
  - `past_advice`: 90 days, at most 5;
  - the page polls every 2000 ms.
- **Statuses and decisions:** run statuses add `awaiting_user`; decisions are `"accepted" | "dismissed"`.
- **Error messages (exact):**
  - `"A budget run is already in progress."` (409);
  - `"Alert not found."` (404);
  - `"This alert was already decided."` (409);
  - `"Unknown DEV_USER_EMAIL user."` (404);
  - `"Run not found."` (404).
- **Test docs:** every test (backend and frontend) gets an entry in `docs/tests/<module>.md` (backend) or `docs/tests/<Name>.test.md` (frontend) in the same commit.
- **Comments:** short, lowercase, one-line "why" notes.
- **Commits:** prefixes `feat(TPC-20):`, `docs(TPC-20):`, `fix(TPC-20):`. **No Co-Authored-By or "Generated with Claude Code" lines.**

## Review Focus

1. **A paused run's checkpoint is gone** (the file was deleted, or the run was paused by an older build). Accepting its alert should still record the decision, and `awaiting_approval` shouldn't crash. Pinned by `test_missing_checkpoint_still_decides` (Task 4).
2. **Dismissing**, not just accepting, the alert a paused run waits on should also resume and finish it. Pinned by `test_dismiss_resumes_too` (Task 4).
3. **`GET /budget` after the period ended** (Nov 3 with only an October period) should give `period: null`, not October's numbers judged against November. Pinned by `test_budget_period_over_is_null` (Task 6).
4. **A run that ends `no_budget` or `period_ended`.** The page should say so ("No budget to check for that date."), not "No new alerts". Pinned by `test_run_no_budget_message` (Task 8).
5. **Another user's alerts** never appear in `GET /alerts`. Pinned by `test_alerts_only_current_user` (Task 5).

---

### Task 1: `advice_log.alert_id` and config

**Files:**
- Modify: `backend/app/db/models.py`, `backend/app/config.py`, `backend/app/agents/budget_graph.py` (`log_advice`).
- Create: `backend/alembic/versions/<rev>_advice_alert_link.py`.
- Modify: `backend/tests/test_budget_graph.py`, `docs/tests/test_budget_graph.md`.

**Interfaces:**
- Produces:
  - `AdviceLog.alert_id: Mapped[Optional[int]]`, with `ForeignKey("alerts.id")` and an index;
  - `config.DEV_USER_EMAIL: str` (from env `DEV_USER_EMAIL`, default `"dev@localhost"`);
  - `config.BUDGET_CHECKPOINT_PATH: Path` (env `BUDGET_CHECKPOINT_PATH`, or `Path(__file__).parent.parent / "data" / "budget_checkpoints.db"`).

- [ ] **Step 1: Failing test.** `test_advice_links_alert`: the full one-time run (as in `test_full_run_one_time`) gives `advice.alert_id == alert.id`.
- [ ] **Step 2: Run it and confirm it fails** (`AttributeError: alert_id`).
- [ ] **Step 3: Implement.**
  - Add the model column and the config values.
  - In `log_advice`, flush the alert before adding the advice, so `alert.id` is available.
  - Autogenerate the migration. Check that it only does `add_column` plus index and FK, created inside `batch_alter_table`, with a named FK `fk_advice_log_alert_id_alerts`.
- [ ] **Step 4: Verify.**
  - The test passes.
  - Migration round trip on a scratch SQLite file (upgrade, `check` gives "No new upgrade operations detected.", downgrade -1, upgrade).
  - `alembic upgrade head` on dev.db.
  - The full suite is green.
- [ ] **Step 5: Doc entry and commit.** Commit message: `feat(TPC-20): link advice to its alert`.

---

### Task 2: Memory facts and decide rules

**Files:**
- Modify: `backend/app/privacy/gateway.py`, `backend/app/agents/budget_llm.py`, `backend/app/agents/budget_graph.py` (`diagnose`, `decide_action`).
- Tests: `test_gateway.py`, `test_budget_llm.py`, `test_budget_graph.py`, with their docs.

**Interfaces:**
- Consumes: `AdviceLog.alert_id` (Task 1).
- Produces:
  - `build_context("diagnose", *, facts, transactions, this_month_alerts: list[dict] = (), past_advice: list[dict] = ())`, adding `this_month_alerts` and `past_advice` to `DIAGNOSE_FIELDS`;
  - `this_month_alerts` items are `{"day": int, "severity", "action", "status"}`;
  - `past_advice` items are `{"month": "October 2026", "action", "outcome"}` (outcome `None` if undecided);
  - `decide_messages(context, diagnosis, projected_cents, proposed_cents, severity: str)`, which adds the line `Severity of this flag: {severity}`.
- Graph helpers: `_this_month_alerts(s, user_id, period, category) -> list[dict]` and `_past_advice(s, user_id, category, today) -> list[dict]`.
  - The advice is read through `AdviceLog.alert_id` → `Alert.action`, keeping only rows that have an `alert_id`.
  - It's newest first, at most 5, within `created_at >= today - 90 days`.
  - `created_at` is compared on its date part.
- **`DECIDE_PROMPT` gains these lines (exact):**
  - `"- If this_month_alerts already has an alert and this flag's severity is not higher than the highest severity there (high is higher than medium), choose no_op.\n"`
  - `"- Never choose adjust_next_budget if past_advice has a dismissed adjust_next_budget.\n"`

- [ ] **Step 1: Failing tests.**
  - `test_gateway.py::test_diagnose_memory_keys`: data has both keys, items have exactly the listed keys, there's no `"message"` anywhere in those lists, and the defaults are `[]`.
  - `test_budget_llm.py::test_decide_messages_severity_and_rules`: the text contains `"Severity of this flag: high"`, and `DECIDE_PROMPT` contains `"choose no_op"` and `"Never choose adjust_next_budget if past_advice"`.
  - `test_budget_graph.py::test_memory_facts_in_prompt`: run on Oct 20, then a second run. The second run's diagnose prompt (from `m.seen`) contains `"this_month_alerts":[{"day":20,"severity":"high","action":"alert","status":"pending_user"}]`, and its `past_advice` has `"action":"alert"` and `"outcome":null`. It contains none of the first alert's message text.
  - `test_budget_graph.py::test_second_run_no_op_no_alert`: the second run, scripted to `no_op`, leaves 1 alert in total.
  - `test_budget_graph.py::test_memory_other_category_ignored`: an earlier alert for TRAVEL is absent from the GENERAL_MERCHANDISE prompt.
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement.** **Step 4: Run the touched modules' tests, then the full suite, and confirm they pass.**
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-20): the agent sees its own alerts and advice`.

---

### Task 3: `await_approval`, checkpointing, `resume_budget_run`

**Files:** Modify `backend/app/agents/budget_graph.py`, `backend/tests/test_budget_graph.py`, `backend/tests/test_budget_graph_cli.py` and their docs.

**Interfaces:**
- Produces:
  - `build_budget_graph(..., checkpointer=None)`;
  - `run_budget(..., checkpointer=None, on_start: Optional[Callable[[int], None]] = None) -> dict`. Its state `status` is `"awaiting_user"` when paused, and it gains `waiting_for: list[int]`;
  - `resume_budget_run(session_factory, run_id: int, alert_id: int, decision: str, *, checkpointer, model=None) -> dict`;
  - `waiting_for(session_factory, checkpointer, run_id: int) -> list[int]`. It compiles the graph with `model=None, gateway=None` (neither is touched by `get_state`), reads `get_state({"configurable": {"thread_id": f"budget-{run_id}"}}).tasks[*].interrupts[*].value["waiting_for"]`, and gives `[]` when there's nothing;
  - `BudgetState.decisions: dict`.
- **Routing:** after `log_advice`, go to `await_approval` only when a checkpointer exists and `actions` has any `adjust_next_budget` with an alert written.
- **The `await_approval` body** is as in the spec. Resume values arrive with string keys from JSON and are cast with `int`.
- **The summary's `decisions`** has string keys.
- **The CLI's `main`** builds `SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))` on `config.BUDGET_CHECKPOINT_PATH` (creating its parent directory). `_print_report` prints `status: awaiting_user (waiting for alert(s) 3, 4)`.

- [ ] **Step 1: Failing tests** (`ck = InMemorySaver()`):
  - `test_adjust_pauses_awaiting_user`: the recurring user, an adjust reply, and `checkpointer=ck`. State `status == "awaiting_user"`, `waiting_for == [alert.id]`, and the `agent_runs` status is `"awaiting_user"`.
  - `test_resume_accept_finishes`: then `resume_budget_run(db, run_id, alert.id, "accepted", checkpointer=ck)` gives status `"ok"`, with `summary_json["decisions"] == {str(alert.id): "accepted"}`.
  - `test_two_adjusts_any_order`: build a user with two overspent flexible categories. Seed the recurring profile, then with `s.info["n"] = 100000` add a second recurring-like pattern in TRAVEL: $40 in Jul, Aug and Sep, then $200 on Oct 5 at "AirCo" plus $40. Script two diagnoses and two adjust actions. Resume the later alert id first → still `awaiting_user`, `waiting_for == [first id]`. Resume the first → `ok`.
  - `test_alert_only_never_pauses`: the one-time user with `checkpointer=ck` and an `alert` action gives `ok`.
  - `test_no_checkpointer_never_pauses`: the recurring user, an adjust action, no checkpointer → `ok` (TPC-19 behaviour).
  - `test_on_start_gets_run_id`: `on_start` is called once with the run's id before the graph runs.
  - `test_cli_awaiting_user`: CLI test, with `config.BUDGET_CHECKPOINT_PATH` patched to `tmp_path`, the recurring user and an adjust reply → output contains `"status: awaiting_user (waiting for alert(s)"`.
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass**, then run the full suite.
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-20): budget proposals wait for the user with interrupt()`.

---

### Task 4: `budget_service/alerts.py`

**Files:** Create `backend/app/budget_service/alerts.py`, `backend/tests/test_budget_alerts.py` and `docs/tests/test_budget_alerts.md`.

**Interfaces:**
- Consumes: `resume_budget_run` and `waiting_for` (Task 3).
- Produces:
  - `SEED_TOKEN = "x"`, `AlertNotFound`, `AlertAlreadyDecided`;
  - `decide_alert(session_factory, user_id, alert_id, decision, *, checkpointer=None, model=None) -> Alert`;
  - `awaiting_approval(session_factory, user_id, *, checkpointer) -> set[int]`;
  - `has_real_plaid_item(session, user_id) -> bool` (any `PlaidItem` with `access_token_enc != SEED_TOKEN`).
- **Error handling:** checkpoint read errors in `awaiting_approval` count as "not waiting". A missing thread gives `get_state` an empty `tasks`.
- **Resume failures** are logged with `logger.warning("budget run %s resume failed", run_id, exc_info=True)`.
- **The model for a resume** comes from the `model` argument, or `get_chat_model()` when `None`. A resume doesn't call it, but the graph builder needs one.

- [ ] **Step 1: Failing tests:**
  - `test_accept_sets_status_and_outcome`;
  - `test_dismiss_sets_status_and_outcome`;
  - `test_already_decided`;
  - `test_other_user_or_unknown_not_found`;
  - `test_accept_resumes_paused_run` (the run becomes `ok`);
  - `test_dismiss_resumes_too`;
  - `test_resume_failure_keeps_decision` (monkeypatch `resume_budget_run` to raise: the alert is returned as `accepted` and the run stays `awaiting_user`);
  - `test_missing_checkpoint_still_decides` (pause with one `InMemorySaver`, decide with a fresh empty one: the decision is recorded, the run stays `awaiting_user`, and `awaiting_approval` with the empty saver is `set()`);
  - `test_awaiting_approval_ids`;
  - `test_has_real_plaid_item` (a seeded user is `False`; set the item's token to `"enc"` and it's `True`).
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Doc and commit.** Commit message: `feat(TPC-20): decide_alert records the choice and resumes the run`.

---

### Task 5: `DEV_USER_EMAIL` and `api/alerts.py`

**Files:**
- Modify: `backend/app/api/deps.py`, `backend/app/main.py`.
- Create: `backend/app/api/budget_hooks.py`, `backend/app/api/alerts.py`, `backend/tests/test_budget_api.py`, `docs/tests/test_budget_api.md`.

**Interfaces:**
- Produces:
  - `get_current_user` reads `config.DEV_USER_EMAIL` at call time;
  - `api/budget_hooks.py` holds the three test hooks: `session_factory = SessionLocal`, `make_model = get_chat_model` and `get_checkpointer()` (lru-cached `SqliteSaver` on `BUDGET_CHECKPOINT_PATH`, like chat's). Both routers read them as `budget_hooks.<name>` at call time, so tests patch one place;
  - `api/alerts.py` has `router`, `AlertOut`, `GET /alerts`, and `POST /alerts/{alert_id}/accept|dismiss`.
- The `status` query is `Literal["pending_user", "all"] = "all"`.

- [ ] **Step 1: Failing tests** in `test_budget_api.py`.
  - **Fixture `api(db, monkeypatch)`:** patch `budget_hooks.session_factory = db`, `budget_hooks.get_checkpointer = lambda: ck` (one `InMemorySaver` per test) and `budget_hooks.make_model` to a scripted-model factory. Set `config.DEV_USER_EMAIL` to a seeded planted user's email `"<pid>@eval.local"`, and the audit path to `tmp_path`. The `client` fixture's `get_session` override covers `get_current_user`.
  - **Tests:**
    - `test_alerts_newest_first_and_filter`;
    - `test_alerts_bad_status_422`;
    - `test_alerts_only_current_user`;
    - `test_alerts_awaiting_approval_flag`;
    - `test_accept_200_returns_alert`;
    - `test_dismiss_200`;
    - `test_accept_other_users_404`;
    - `test_accept_twice_409`;
    - `test_dev_user_email_override_acts_as_user`;
    - `test_unknown_dev_user_email_404`;
    - `test_default_dev_user_still_created`.
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement**, and include the router in `main.py`. **Step 4: Run `test_budget_api.py` and the existing `test_dashboard.py`, `test_chat_api.py` and `test_plaid.py`, and confirm they pass.**
- [ ] **Step 5: Doc and commit.** Commit message: `feat(TPC-20): alerts api and a configurable dev user`.

---

### Task 6: `api/budget.py`

**Files:** Create `backend/app/api/budget.py`; modify `backend/app/main.py` and `backend/tests/test_budget_api.py` (and its doc).

**Interfaces:**
- Consumes: the `budget_hooks`, `has_real_plaid_item`, `awaiting_approval`, `run_budget(on_start=...)` and `plaid_sync`.
- Produces:
  - `GET /budget` → `BudgetOut`;
  - `POST /budget/run` (`RunRequest {date: Optional[date]}`) → `202 {"run_id"}`;
  - `GET /budget/runs/{run_id}` → `RunOut {run_id, status, alert_ids, waiting_for, error}`;
  - a per-user lock dict, like `api/chat.py`'s;
  - `_today()` (patchable) for `GET /budget`.
- **The background task** acquires nothing itself: the route acquires the lock with `blocking=False`, and the task releases it in `finally`. The route waits on a `threading.Event` that `on_start` sets (at most 5 s), then returns the id. If the event times out, the route returns `202 {"run_id": null}`.

- [ ] **Step 1: Failing tests:**
  - `test_budget_lines_match_evaluate` (planted one-time user, `_today` patched to Oct 20, after one run creates the budget);
  - `test_budget_no_period_null`;
  - `test_budget_period_over_is_null`;
  - `test_budget_writes_nothing` (row counts and `updated` values are unchanged);
  - `test_run_202_then_ok` (`POST` with `{"date": "2026-10-20"}`, then `GET /budget/runs/{id}` gives `ok` and `alert_ids` of length 1);
  - `test_run_409_while_locked` (hold the user's lock in the test);
  - `test_run_error_status` (model raises `ConnectionError` → status `error`, `error == "ConnectionError"`);
  - `test_run_syncs_only_real_items` (patch `budget.plaid_sync` to record calls: a seeded user isn't called; after setting the token to `"enc"`, it is);
  - `test_run_awaiting_user` (recurring user, adjust reply → `awaiting_user`, `waiting_for == alert_ids`);
  - `test_run_other_users_404`;
  - `test_run_bad_date_422`.
- [ ] **Step 2: Run them and confirm they fail.** **Step 3: Implement.** **Step 4: Run them and confirm they pass**, then run the full suite.
- [ ] **Step 5: Doc and commit.** Commit message: `feat(TPC-20): budget pace and background runs api`.

---

### Task 7: Frontend API client, `BudgetPaceBar` and `AlertCard`

**Files:**
- Modify: `frontend/lib/api.ts`, `frontend/lib/api.test.ts`.
- Create: `frontend/components/BudgetPaceBar.tsx` and `.test.tsx`, `frontend/components/AlertCard.tsx` and `.test.tsx`, plus `docs/tests/BudgetPaceBar.test.md` and `docs/tests/AlertCard.test.md`.
- Update: `docs/tests/api.test.md`.

**Interfaces:**
- Produces, in `lib/api.ts`:
  - `type BudgetLine`, `type Budget`, `type Alert`, `type RunStatus = { run_id; status; alert_ids; waiting_for; error }`;
  - `getBudget()`, `runBudget(date?)` → `{run_id: number | null}`, `getRun(id)`, `getAlerts(status?)`, `acceptAlert(id)`, `dismissAlert(id)`;
  - `categoryLabel(c: string): string` in `lib/format.ts` (`"GENERAL_MERCHANDISE"` → `"General merchandise"`).
- Components: `BudgetPaceBar({line, daysElapsed, daysInPeriod})` and `AlertCard({alert, onAccept, onDismiss, busy})`, as in the spec.

- [ ] **Step 1: Failing tests.**
  - **`api.test.ts`:** each call's method, URL and body. `runBudget("2026-10-20")` POSTs `{"date":"2026-10-20"}`, and `runBudget()` POSTs `{}`. A 409 from `runBudget` → `errorMessage` gives `"A budget run is already in progress."`.
  - **`BudgetPaceBar.test.tsx`:**
    - `spent 236, limit 472` → the meter's `aria-valuenow` is 50, and the fill style width is `50%`;
    - the today marker's left is `${20/31*100}%`;
    - `reason "pace"` → the fill has an amber class; `"over_limit"` → a red class;
    - spent above the limit → width `100%`, and the text shows the real spent;
    - a limit of 0 → width `0%`, no crash;
    - a fixed line shows "(fixed)".
  - **`AlertCard.test.tsx`:**
    - Accept and Dismiss call back with the id;
    - a decided alert has no buttons and shows "Accepted" or "Dismissed";
    - `busy` disables the buttons;
    - the proposed limit reads `"Proposed limit next month: $135.00"`, and only for `adjust_next_budget`;
    - the waiting note appears only when `awaiting_approval`;
    - the label is `"General merchandise"`.
- [ ] **Step 2: `npm test` and confirm they fail.** **Step 3: Implement.** **Step 4: `npm test` and `npm run lint`, and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-20): budget pace bar, alert card and api calls`.

---

### Task 8: The Alerts page and the dashboard Budget card

**Files:**
- Create: `frontend/app/alerts/page.tsx`, `frontend/app/alerts/page.test.tsx`, `docs/tests/alerts-page.test.md`.
- Modify: `frontend/app/dashboard/page.tsx`.

**Interfaces:** Consumes Task 7's calls and components. The page test mocks `@/lib/api` with `vi.mock`, as `ChatPanel.test.tsx` does (not `fetch`), and uses fake timers for polling.

- [ ] **Step 1: Failing tests** (`page.test.tsx`):
  - `test loads pending and history` (two lists, headings "Pending" and "History");
  - `test run polls until done then shows result and reloads`:
    - `runBudget` → `{run_id: 7}`;
    - `getRun` → `running`, then `ok` with 1 alert id;
    - after advancing 2000 ms twice, the text shows "1 alert written", and `getAlerts` was called again;
  - `test run awaiting shows approval line` → "Waiting for your approval on 1 proposal";
  - `test_run_no_budget_message` → "No budget to check for that date.";
  - `test run error shows failure` → "The run failed (ConnectionError)";
  - `test 409 shows message`;
  - `test accept reloads` (`acceptAlert` called with the id, then `getAlerts` again).
- [ ] **Step 2: `npm test` and confirm they fail.**
- [ ] **Step 3: Implement the page.**
  - Then the dashboard: a `getBudget()` fetch next to `getDashboard()`, a **Budget** card above Accounts (bars, plus an "Unbudgeted: …" line, or "No budget yet. Run a budget check from Alerts."), and a header **Alerts** link with the pending count from `getAlerts("pending_user")`.
  - A dashboard budget-fetch error shows inside the card and doesn't break the page.
- [ ] **Step 4: `npm test`, `npm run lint` and `npm run build`, and confirm they pass.**
- [ ] **Step 5: Docs and commit.** Commit message: `feat(TPC-20): alerts page and dashboard budget pace bars`.

---

### Task 9: Live check, demo and docs

**Files:** Modify `backend/tests/test_budget_graph.py` (live test), `docs/architecture.md` and `README.md`.

- [ ] **Step 1: Live test** `test_live_second_run_no_duplicate` (skipped without Ollama). `overspend-one-time` on 2026-10-20 with the real model: 1 alert. A second `run_budget` on the same date: still 1 alert, and the second run's action for GENERAL_MERCHANDISE is `no_op`.
  - Run it twice. If it fails, reword the decide rule only, record a ruling, and rerun.
- [ ] **Step 2: API demo** (no browser available):
  1. seed `overspend-recurring --replace`;
  2. start uvicorn with `DEV_USER_EMAIL=overspend-recurring@eval.local` in the background;
  3. `curl -X POST /budget/run -d '{"date":"2026-10-20"}'`, then poll `/budget/runs/{id}` until it's `awaiting_user`;
  4. `GET /alerts` shows `awaiting_approval: true`;
  5. `POST /alerts/{id}/accept`, and the run then reads `ok`;
  6. run again → no new alert.

  Stop uvicorn. Save the output for the decision log.
- [ ] **Step 3: Docs.** Architecture sections 4.1, 5, 6.2, 6.6, 9 and 12, and the decision-log rows from the spec's Docs list, including the live dedupe result and the demo. `README.md` gets the UI demo commands. Remove the "a daily run alerts again until TPC-20" caveats, since that's now fixed.
- [ ] **Step 4: Full backend suite, `npm test` and `npm run build`.** Commit message: `docs(TPC-20): architecture, decision log and live dedupe result`.
