# Tests: `backend/tests/test_budget_graph.py`

These tests cover `app/agents/budget_graph.py`, the daily budget agent (TPC-19, architecture 6.1-6.2). For one user and date, `run_budget`:
1. loads or creates the budget;
2. optionally syncs Plaid, and updates every line's spending and pace;
3. flags overspending;
4. asks the LLM for each flag's cause and an action (from facts the code computed, through the privacy gateway);
5. writes alerts and advice;
6. records the run in `agent_runs`.

An LLM that can't answer never hides an overspend: the graph falls back to a plain alert.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_graph.py -v
```

## Shared setup

- `seed(db, pid)` writes a planted or accuracy profile from `eval/budget_users.py` into the test database and returns its user id. `overspend-one-time` overspends general merchandise with a $600 purchase on Oct 12. `overspend-recurring` overspends personal care with a new $90 gym.
- `fake(*replies)` is a `ScriptedChatModel`. `reply(**kw)` builds a JSON reply. `DIAG` is a `one_time` diagnosis and `ALERT` an `alert` action. Each flag takes a diagnose reply, then a decide reply.
- `run(db, uid, today, model, tmp_path, ...)` calls `run_budget` with the audit log in `tmp_path`. `ONE` is Oct 20 2026.
- `rows(db, cls, **filters)` reads rows in a fresh session.

---

## `test_full_run_one_time`

**What it does:** On the one-time user, a run writes one `overspend` alert (`high`, `pending_user`, cause `one_time`, action `alert`, no proposed limit) and one advice row tied to the run. The `agent_runs` row finishes `ok` with every summary key (including the user's `decisions`), and the general-merchandise line's spent and pace are updated.

**Why it's there:** It's the whole graph working end to end on the done-when's user, with a scripted model.

## `test_advice_links_alert`

**What it does:** The advice row a run writes carries the id of the alert it became.

**Why it's there:** When the user accepts or dismisses that alert (TPC-20), the decision is written back to this advice row as its outcome. The agent's memory then shows what the user did with it.

## `test_memory_facts_in_prompt`

**What it does:** After a first run alerts on general merchandise, the second run's diagnose prompt lists that alert (`high`, `alert`, `pending_user`) under `this_month_alerts`. It lists the advice under `past_advice` (October 2026, `alert`, no outcome yet), and doesn't contain the first alert's message.

**Why it's there:** It's the memory the model needs to avoid repeating itself (TPC-20), read by code and passed through the gateway. Past advice is keyed by its budget month, not the wall clock, because runs can be simulated on other dates.

## `test_second_run_no_op_no_alert`

**What it does:** When the model chooses `no_op` on the second run, there is still exactly one alert.

**Why it's there:** It's the done-when's mechanism with a scripted model: deduplication happens because the model chose `no_op`. `test_live_second_run_no_duplicate` checks that the real model does.

## `test_memory_other_category_ignored`

**What it does:** A dismissed travel alert in the same month doesn't appear in the general-merchandise prompt.

**Why it's there:** Memory is per category. Another category's alerts would mislead the model into `no_op` for a new overspend.

## `test_second_run_reuses_budget_and_alerts_again`

**What it does:** Two runs on the same day share one budget period, write two alerts and record two runs.

**Why it's there:** The budget is created once. Alerting again is the known gap that TPC-20's deduplication closes; this test pins today's behaviour so that change is deliberate.

## `test_no_history_no_budget`

**What it does:** A user whose only spending is this month ends the run as `no_budget`, with no LLM calls and no alerts.

**Why it's there:** With nothing to fit there's no budget to check, and the run says so instead of failing.

## `test_period_ended`

**What it does:** After an October run, a run on Nov 3 ends as `period_ended` and writes no alerts.

**Why it's there:** Closing October and opening November is TPC-22's job. The daily run mustn't judge November's spending against October's budget.

## `test_last_day_of_period_runs`

**What it does:** A run on Oct 31 runs normally.

**Why it's there:** The last day is still in the period. An off-by-one would skip the month's final check.

## `test_no_flags_no_llm`

**What it does:** A user with only rent, paid on the 1st, finishes `ok` without calling the model, and the rent line shows $1,500 spent.

**Why it's there:** The LLM only runs when something is flagged, which keeps daily runs fast and keeps the model out of the normal case.

## `test_adjust_stores_proposed_limit`

**What it does:** When the model picks `adjust_next_budget` for the gym user, the alert stores a proposed limit of $135: the old $42 limit plus the new gym's $90 typical month, rounded up to $5.

**Why it's there:** The number the user accepts is computed by code, never written by the model, and TPC-22's close applies exactly that number.

## `test_no_op_writes_nothing`

**What it does:** A `no_op` action writes no alert and no advice, but the run still records it.

**Why it's there:** The model can judge a flag not worth bothering the user about, and the summary still shows that it did.

## `test_diagnose_failure_still_alerts`

**What it does:** Two unparseable diagnose replies still produce an alert, with no cause, and count one LLM failure.

**Why it's there:** An overspend must reach the user even when the model can't explain it.

## `test_decide_failure_fallback`

**What it does:** Two unparseable decide replies produce an `alert` with the code's own message ("General merchandise is over its $… limit: $… so far."), marked as a fallback.

**Why it's there:** It's the same rule for the second call: the user is still told, in plain numbers.

## `test_adjust_downgraded_unless_recurring`

**What it does:** When the model diagnoses `seasonal` but picks `adjust_next_budget` (with a "$1600" message), the code stores a plain `alert` with no proposed limit and its own message, and marks the action as downgraded.

**Why it's there:** It's the code guard behind the prompt change. Only a recurring cause may change next month's budget, and the model's message can't be kept because it quotes a limit it shouldn't have proposed.

## `test_adjust_pauses_awaiting_user`

**What it does:** With a checkpointer, the gym user's `adjust_next_budget` alert pauses the run. The state and the `agent_runs` row say `awaiting_user`, and the run is waiting on that alert's id.

**Why it's there:** Architecture 6.6: a proposal that would change the budget waits for the user, through LangGraph `interrupt()` (TPC-20).

## `test_resume_accept_finishes`

**What it does:** Resuming that run with "accepted" finishes it as `ok`. The decision is in the run summary, and nothing is waiting any more.

**Why it's there:** The user's click on Accept is what continues the paused run. Its decision has to be recorded where TPC-22's close and the eval can read it.

## `test_two_adjusts_any_order`

**What it does:** A run with two budget proposals (personal care and travel) waits for both. Dismissing the second one first leaves it waiting on the first, and accepting the first finishes it with both decisions recorded.

**Why it's there:** The Alerts page lets the user decide in any order, so the run can't assume the order it asked in. Each resume carries an `[alert_id, decision]` pair, not a dict, because LangGraph reads a dict resume as interrupt id → value.

## `test_reused_run_id_starts_clean`

**What it does:**
1. A paused run is accepted.
2. The user is deleted and seeded again, which is what `seed_budget_user.py --replace` does.
3. A new run is started.

SQLite gives the new run the same id, and it still pauses, with its alert pending.

**Why it's there:** It was found in the TPC-20 review. Checkpoints are stored under `budget-<run_id>`, and the old thread's `decisions` used to carry into the reused id, so the run skipped the pause and recorded an approval the user never gave. `run_budget` now clears any thread for a new run id before it starts.

## `test_alert_only_never_pauses`

**What it does:** A run whose only action is a plain `alert` finishes `ok`, even with a checkpointer.

**Why it's there:** Informational alerts change nothing, so nothing should wait on them, and a paused run per day would pile up.

## `test_no_checkpointer_never_pauses`

**What it does:** Without a checkpointer, an `adjust_next_budget` run finishes `ok`, as in TPC-19.

**Why it's there:** There's nowhere to keep a paused run without a checkpointer, so callers that don't pass one keep the old behaviour.

## `test_on_start_gets_run_id`

**What it does:** `on_start` is called once with the new run's id.

**Why it's there:** `POST /budget/run` answers with the run id straight away and lets the run continue in the background, because the Next.js proxy drops a request after 30 s of silence.

## `test_sync_called_and_failure_recorded`

**What it does:** The sync function is called once with the user id. When it raises, the run still finishes `ok`, and the summary records the error by type.

**Why it's there:** A Plaid outage shouldn't stop the daily budget check, which then runs on the data already stored.

## `test_no_sync_fn_skipped`

**What it does:** Without a sync function, the run records the sync as skipped.

**Why it's there:** Synthetic users and the CLI's `--no-sync` have no Plaid item to sync.

## `test_crash_marks_error`

**What it does:** When a node raises, `run_budget` re-raises, and the run's row is `error` with the exception type and a finish time.

**Why it's there:** A failed run must be visible in `agent_runs`, not left as `running` forever, and the scheduler's next run can retry.

## `test_prompt_privacy`

**What it does:** The audit log has exactly one `diagnose` prompt. Every prompt the model received carries that pseudonym and the merchant, and never the user id.

**Why it's there:** It's the privacy rule (architecture section 7): user data reaches the model only through the gateway, and each prompt is audited once, even though the decide call reuses it.

## `test_unbudgeted_listed`

**What it does:** A travel purchase in a category with no budget line is listed as unbudgeted in the summary, and gets no alert.

**Why it's there:** A category with no line isn't overspent against anything. TPC-19 reports it, and suggesting a new line is left to period close.

## `test_live_planted_causes`

**What it does:** It is skipped unless Ollama is running with `OLLAMA_MODEL`. For each of the four planted users, it runs the real model on its run date and checks the cause. A recurring cause must lead to `adjust_next_budget`; every other cause must lead to something other than `adjust_next_budget` or `no_op`.

**Why it's there:** It's the done-when: the local model gets the right cause from the computed facts for all three causes, plus the big one-off at a regular shop found in review. Checking the action as well catches a proposal the user shouldn't get, such as a higher budget after holiday shopping.

## `test_live_second_run_no_duplicate`

**What it does:** It is skipped unless Ollama is running with `OLLAMA_MODEL`. It runs the one-time planted user twice on Oct 20 with the real model. The first run writes one alert. The second chooses `no_op`, and there is still one alert.

**Why it's there:** It's TPC-20's done-when, checked against the real local model, because deduplication is the model's decision from its memory facts.
