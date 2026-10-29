# TPC-19 Budget Agent Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily budget LangGraph run for one user and date. It updates pace, flags overspending, has the local LLM diagnose each flag's cause from facts computed by code, picks an action, writes alerts and advice, and records the run. It can be run from a CLI, with a seed script for planted demo users.

**Architecture:**
- **`budget_engine/diagnose.py`** (pure) computes each flag's facts, the projection and the proposed limit.
- **`privacy/gateway.py`** turns the facts into the only prompt text, through the new `diagnose` purpose.
- **`agents/budget_llm.py`** holds the Pydantic outputs, the prompts and `invoke_structured`, which uses Ollama's `format` and one retry.
- **`agents/budget_graph.py`** wires the nodes: `run_budget` and the CLI.
- **`scripts/seed_budget_user.py`** puts the planted users from `eval/budget_users.py` into `dev.db`.

**Tech Stack:** Python 3.12, LangGraph, langchain-ollama (`ChatOllama`, `format=` JSON schema), Pydantic v2, SQLAlchemy 2.1, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-10-28-tpc-19-budget-graph-design.md`

## Global Constraints

- **Running things:** tests run from `backend/` with `../venv/bin/python -m pytest`; Alembic runs from `backend/` with `../venv/bin/alembic`. No new dependencies.
- **Money:** integer cents everywhere. Cents from division go through `round_cents` (half up). The LLM never produces a stored money number.
- **Privacy:** user data reaches a prompt only through `PrivacyGateway.build_context`. Prompts contain the pseudonym, never `user_id`.
- **Values from the spec:**
  - `TXN_LIMIT_DIAGNOSE = 20`; top merchants = 5;
  - the proposed limit rounds up to a multiple of 500;
  - severity: `over_limit` → `"high"`, `pace` → `"medium"`;
  - alert `kind="overspend"`, `status="pending_user"`;
  - `agent_runs.graph="budget"`;
  - statuses: `running | ok | no_budget | period_ended | error`;
  - `invoke_structured` retries once.
- **Fallback messages (exact):**
  - pace: `"{Category} is at {pace:.0%} of its pace: ${spent} of ${limit} by day {d}."`
  - over limit: `"{Category} is over its ${limit} limit: ${spent} so far."`

  `{Category}` is the category in title case with underscores as spaces ("General merchandise"), and amounts have 2 decimals.
- **Test docs:** every test gets an entry in `docs/tests/<module>.md` in the same commit (architecture section 12), in the existing format.
- **Comments:** short, lowercase, one-line "why" notes.
- **Commits:** prefixes `feat(TPC-19):`, `docs(TPC-19):`, `fix(TPC-19):`. **No Co-Authored-By or "Generated with Claude Code" lines.**

## Plan-time ruling (deviates from the spec)

- **Planted users get a minimal base:** rent, paycheck and savings transfer only. The spec said `RENT, GROCER, BASE`. A check at plan time showed a steady user already raises two flags on Aug 20:
  - ENTERTAINMENT at pace 1.48, because the $15.99 streaming charge lands on day 5;
  - TRANSPORTATION at pace 1.12, because of how many Mondays have passed.

  Those false alarms come from TPC-17's pace rule on lumpy spending. They'd add unrelated flags to the demo. They're recorded as a follow-up (M5 threshold tuning, and TPC-33 moving subscriptions into the fixed set). Task 2 pins "exactly one flag per planted user".

## Review Focus

1. **A refund in the flagged category makes the largest purchase bigger than net spending.** The share should be capped at 1.0, not 1.3. Pinned by `test_largest_share_capped` (Task 3).
2. **The model wraps its JSON in a ```json fence** (common even with `format`). It should parse, not burn the retry. Pinned by `test_fenced_json_parses` (Task 5).
3. **The model returns an empty `message` or `evidence`.** A blank alert should never be written; it counts as invalid and is retried. Pinned by `test_empty_text_is_invalid` (Task 5).
4. **A run on the period's last day** (Oct 31). It's still in the period, so it runs normally and doesn't count as `period_ended`. Pinned by `test_last_day_of_period_runs` (Task 6).
5. **A flagged merchant with no name.** The diagnose prompt carries `"merchant": null`, without crashing in the scrubber. Pinned by `test_diagnose_unnamed_merchant` (Task 4).

---

### Task 1: `alerts.proposed_limit_cents` and severity values

**Files:** Modify `backend/app/db/models.py`; create `backend/alembic/versions/<rev>_alert_proposed_limit.py`; modify `backend/tests/test_budget_service.py` and `docs/tests/test_budget_service.md`.

**Interfaces:**
- Produces:
  - `Alert.proposed_limit_cents: Mapped[Optional[int]]`;
  - `AlertSeverity = Literal["medium", "high"]` in `models.py`, next to the other aliases.

- [ ] **Step 1: Write the failing test** in `test_budget_service.py`:
```python
def test_alert_proposed_limit(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        s.add_all([Alert(user_id="u1", kind="overspend", severity="high", message="m"),
                   Alert(user_id="u1", kind="overspend", severity="medium", message="m", proposed_limit_cents=16000)])
        s.commit()
        assert sorted(a.proposed_limit_cents or 0 for a in s.query(Alert)) == [0, 16000]
```
- [ ] **Step 2: Run it and confirm it fails**

Run: `../venv/bin/python -m pytest tests/test_budget_service.py::test_alert_proposed_limit -v`
Expected: FAIL (`TypeError: 'proposed_limit_cents' is an invalid keyword argument`).

- [ ] **Step 3: Implement.** Add the column and the alias, then run `../venv/bin/alembic revision --autogenerate -m "alert proposed limit"`. Check that the generated file holds only `add_column('alerts', ...)` and its drop.
- [ ] **Step 4: Run the test and the migration check**

Run the test and confirm it passes. Then, on a scratch SQLite file (`DATABASE_URL=sqlite:///$SCRATCH/mig.db`), run `upgrade head`, then `check`, then `downgrade -1`, then `upgrade head`.
Expected: `No new upgrade operations detected.` Then run `../venv/bin/alembic upgrade head` on dev.db.

- [ ] **Step 5: Doc entry and commit.** Commit message: `feat(TPC-19): alerts.proposed_limit_cents for adjust_next_budget`.

---

### Task 2: Planted overspend profiles

**Files:** Modify `eval/budget_users.py`, `backend/tests/test_budget_users.py` and `docs/tests/test_budget_users.md`.

**Interfaces:**
- Produces:
  - `Monthly.first: Optional[date] = None`: no charge before this date;
  - `Profile.run_date: Optional[date] = None` and `Profile.expected_cause: Optional[str] = None`;
  - `PLANTED_BASE = (RENT, Monthly("INCOME", "Employer", -420000, day=15), Monthly("TRANSFER_OUT", "Savings", 50000, day=16))`;
  - `PLANTED: tuple[Profile, ...]`, with the three profiles from the spec's table, each built on `PLANTED_BASE`. Seeds 11, 12 and 13.
- The seasonal December purchases are 8 `Once`s at "Toy Barn", "Gift Gallery", "Book Nook", "Game Stop Shop", "Candle Co", "Sweater Hut", "Craft Corner" and "Kitchen Plus". They're dated Dec 1, 3, 5, 7, 9, 11, 13 and 14, at $60, $75, $90, $120, $65, $110, $80 and $100.

- [ ] **Step 1: Write the failing tests**
```python
def planted(pid):
    return next(p for p in PLANTED if p.id == pid)

def test_planted_one_time_purchase():
    assert (date(2026, 10, 12), 60000, "GENERAL_MERCHANDISE", "ElectroMart") in transactions(planted("overspend-one-time"))

def test_planted_gym_starts_september():
    gym = [d for d, _, _, m in transactions(planted("overspend-recurring")) if m == "FitClub Gym"]
    assert gym == [date(2026, 9, 3), date(2026, 10, 3)]

def test_planted_december_shops():
    dec = {m for d, _, cat, m in transactions(planted("overspend-seasonal"))
           if d.month == 12 and cat == "GENERAL_MERCHANDISE" and m != "Corner Shop"}
    assert len(dec) == 8

def test_planted_flags_only_the_planted_category():
    # fit on the three months before run_date, pace on run_date: exactly one flag, the planted one
    for p in PLANTED:
        txns = [Txn(d, c, cat, merchant=m) for d, c, cat, m in transactions(p)]
        start = month_start(p.run_date)
        fit = fit_budget([t for t in txns if t.date < start], p.run_date)
        lines = [BudgetLine(l.category, l.kind, l.limit_cents) for l in fit.lines]
        flagged = [r.category for r in evaluate(lines, [t for t in txns if t.date <= p.run_date], p.run_date) if r.flagged]
        assert flagged == [{"overspend-recurring": "PERSONAL_CARE"}.get(p.id, "GENERAL_MERCHANDISE")], p.id

def test_profiles_unchanged():
    assert [p.id for p in PROFILES] == ["steady", "vacation", "early-rent", "rising", "occasional"]
    assert all(p.run_date is None for p in PROFILES)
```
- [ ] **Step 2: Run them and confirm they fail** (`ImportError: PLANTED`).
- [ ] **Step 3: Implement** the new fields and `PLANTED`.
- [ ] **Step 4: Run `tests/test_budget_users.py tests/test_budget_accuracy.py` and confirm they pass.** If `test_planted_flags_only_the_planted_category` shows an extra flag, change that profile's seed or amounts, not the assertion, and record a ruling.
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): three planted overspend users, one per cause`.

---

### Task 3: `budget_engine/diagnose.py`

**Files:** Create `backend/app/budget_engine/diagnose.py`, `backend/tests/test_budget_diagnose.py` and `docs/tests/test_budget_diagnose.md`.

**Interfaces:**
- Consumes: `Txn`, `spending`, `round_cents`, `month_start` and `add_months` (`txns.py`); `PaceResult` (`pace.py`).
- Produces: `MerchantFact`, `OverspendFacts`, `overspend_facts(flag, line_fit, period_txns, history_txns, period_start) -> OverspendFacts`, `projected_cents(spent_cents, days_elapsed, days_in_period) -> int` and `proposed_limit_cents(facts) -> int`, all exactly as in the spec's `diagnose.py` section. All are frozen dataclasses.

- [ ] **Step 1: Write the failing tests.** Shared setup:
  - `OCT = date(2026, 10, 1)`;
  - `flag(cat, spent, limit, reason="over_limit", kind="flexible", pace=None)` builds a `PaceResult` with `days_elapsed=20`, `days_in_period=31` and `flagged=True`;
  - `t(m, d, cents, cat="GENERAL_MERCHANDISE", merchant=None)` builds a `Txn` in 2026.

| Test | Input | Assert |
|---|---|---|
| `test_one_time_largest_and_new_merchant` | Period: `t(10,12,60000,merchant="ElectroMart")` and `t(10,5,3500,merchant="Corner Shop")`. History: Corner Shop $35 on Jul 4, Aug 4 and Sep 4 | `largest.amount_cents == 60000`; `largest_share == 60000/63500`; ElectroMart has `prior_months == 0` and `typical_cents is None`; Corner Shop has `prior_months == 3` and `typical_cents == 3500` |
| `test_recurring_gym_seen_once_before` | PERSONAL_CARE. Period: gym 9000 on Oct 3, barber 4000 on Oct 20. History: barber in Jul, Aug and Sep; gym on Sep 3 | gym has `prior_months == 1` and `typical_cents == 9000`; merchants are ordered by `period_cents` descending |
| `test_history_from_fit_json` | `line_fit={"monthly_totals":[15000,14800,15200],"outliers":[{"month":"2026-08-01",...}]}` | `monthly_totals == (15000,14800,15200)`; `outlier_months == (date(2026,8,1),)`; `month == OCT`. With `line_fit=None`, both are `()` |
| `test_other_categories_and_income_ignored` | FOOD and INCOME rows in the period | they are absent from `merchants`, and `largest` is still the GENERAL_MERCHANDISE purchase |
| `test_merchants_capped_and_unnamed_grouped` | 7 merchants, plus 2 purchases with no merchant | `len(merchants) == 5`; the unnamed purchases are one entry with `charges == 2` if it's in the top 5 |
| `test_largest_share_capped` | a $100 purchase and a $40 refund (net $60) | `largest_share == 1.0` |
| `test_projected_cents` | | `projected_cents(10000, 20, 31) == 15500`; `projected_cents(1001, 2, 31) == 15516` (half up) |
| `test_proposed_limit` | | flexible with projection 15501 → 16000; projection 15500 → 15500; fixed with spent 13000 → 13000 |

- [ ] **Step 2: Run and confirm they fail** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement.** `today` is `period_start + timedelta(days=flag.days_elapsed - 1)`. History is `add_months(period_start, -3) .. period_start - 1 day`. `typical_cents` is `round_cents(Decimal(str(statistics.median(...))))` over the merchant's non-zero monthly sums.
- [ ] **Step 4: Run and confirm they pass**, then run the four `test_budget_*` engine suites.
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): overspend facts, projection and proposed limit`.

---

### Task 4: Gateway purpose `diagnose`

**Files:** Modify `backend/app/privacy/gateway.py`, `backend/tests/test_gateway.py` and `docs/tests/test_gateway.md`.

**Interfaces:**
- Consumes: `OverspendFacts` (Task 3).
- Produces:
  - `build_context("diagnose", *, facts: OverspendFacts, transactions: list[Txn]) -> str`. The data is exactly the spec's JSON keys: `category, reason, kind, month, limit, spent, pace, day, days_in_month, projected, largest, merchants, history, outlier_months, transactions`;
  - `TXN_LIMIT_DIAGNOSE = 20`;
  - `DIAGNOSE_FIELDS`, a tuple of those top-level keys.
- Formats:
  - `month` is `"October 2026"`;
  - `history` months are named the same way, oldest first;
  - `pace` is rounded to 2 decimals, or `None`;
  - `largest` is `{date, amount, merchant, share_of_spent}` (share rounded to 2 decimals), or `None`;
  - each item in `merchants` is `{merchant, this_month, charges, months_seen_before, typical}`.

- [ ] **Step 1: Write the failing tests**
  - `test_diagnose_allowlist`: `set(data(prompt)) == set(DIAGNOSE_FIELDS)`; `data["limit"] == 157.5` for 15750 cents.
  - `test_diagnose_scrubs_merchants`: a merchant `"Paid jane.doe@example.com"` doesn't appear anywhere in the prompt.
  - `test_diagnose_category_only_capped_newest_first`: 25 transactions in the category plus 3 FOOD → 20 transactions, all in the category, with dates descending.
  - `test_diagnose_audited`: the audit line has `purpose == "diagnose"`, `params == {"category": "GENERAL_MERCHANDISE"}`, the pseudonym, and no `"u-jane"` in the line.
  - `test_diagnose_unnamed_merchant`: a purchase with `merchant=None` gives `"merchant": None` in both `merchants` and `transactions`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Add a `"diagnose"` branch: `_diagnose(facts, transactions)`. `scrub_merchant` is only called on non-`None` names. Filter to `facts.category` and sort by date descending before applying the cap.
- [ ] **Step 4: Run `tests/test_gateway.py` and confirm it passes.**
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): gateway diagnose purpose`.

---

### Task 5: `agents/budget_llm.py`

**Files:**
- Create: `backend/app/agents/budget_llm.py`, `backend/tests/test_budget_llm.py`, `docs/tests/test_budget_llm.md`.
- Modify: `backend/tests/fakes.py` (`ScriptedChatModel.kwargs_seen`).

**Interfaces:**
- Produces:
  - `Diagnosis(cause: Literal["one_time","seasonal","recurring"], evidence: str = Field(min_length=1))`;
  - `Action(action: Literal["alert","suggest_cuts","adjust_next_budget","no_op"], message: str = Field(min_length=1))`;
  - `invoke_structured(model, schema, messages, retries=1) -> Optional[BaseModel]`;
  - `DIAGNOSE_PROMPT`, `DECIDE_PROMPT` (the system texts from the spec);
  - `diagnose_messages(context: str) -> list` and `decide_messages(context: str, diagnosis: Optional[Diagnosis], projected_cents: int, proposed_cents: int) -> list`. The diagnosis, projection and proposal are added to the human message as dollars.
- Test support: `ScriptedChatModel` gains `kwargs_seen: list`, which records each call's kwargs.

- [ ] **Step 1: Write the failing tests** (replies are `AIMessage(content=...)`):
  - `test_valid_reply_parsed`: returns `Diagnosis(cause="one_time", ...)`, and `kwargs_seen[0]["format"] == Diagnosis.model_json_schema()`.
  - `test_invalid_then_valid_retries`: a first reply of `"not json"` and then a valid one → parsed; 2 calls.
  - `test_two_invalid_gives_none`: `"{}"` twice → `None`; 2 calls.
  - `test_fenced_json_parses`: a ```json-fenced valid object → parsed on the first call.
  - `test_empty_text_is_invalid`: `{"cause":"one_time","evidence":""}` and then a valid reply → the second is used.
  - `test_model_error_propagates`: a reply of `ConnectionError("down")` → `pytest.raises(ConnectionError)`.
  - `test_decide_messages_carry_numbers`: the human text contains `"$155.00"` and `"$160.00"` for 15500 and 16000 cents, plus the diagnosis cause.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** Strip one surrounding ```` ``` ```` / ```` ```json ```` fence before `model_validate_json`. `ValidationError` (which also covers invalid JSON) counts as invalid.
- [ ] **Step 4: Run them and confirm they pass**, then run `tests/test_chat_graph.py` to confirm the change to `fakes.py` broke nothing.
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): structured diagnosis and action calls with one retry`.

---

### Task 6: `agents/budget_graph.py`: the graph and `run_budget`

**Files:** Create `backend/app/agents/budget_graph.py`, `backend/tests/test_budget_graph.py` and `docs/tests/test_budget_graph.md`.

**Interfaces:**
- Consumes: Tasks 1-5; `initialize_budget` and `load_txns` (TPC-18); `evaluate` and `BudgetLine as EngineLine` (pace); `sync_item`; `PrivacyGateway`; `get_chat_model`.
- Produces:
  - `BudgetState` (the spec's TypedDict);
  - `build_budget_graph(session_factory, *, model, gateway_factory, sync_fn=None)`;
  - `run_budget(session_factory, user_id: str, today: date, *, model=None, sync_fn=None, audit_path=None) -> dict` (the final state);
  - `plaid_sync(session, user_id) -> dict`, the default sync function. The CLI passes it unless `--no-sync` is given.
- Behaviour follows the spec's node table, routing, fallbacks and summary keys exactly.

- [ ] **Step 1: Write the failing tests.** Shared setup:
  - `seed(db, pid)` seeds a `PLANTED` or `PROFILES` profile and returns its user id;
  - `reply(**kw)` builds `AIMessage(content=json.dumps(kw))`;
  - `audit` is `tmp_path / "audit.jsonl"`;
  - `ONE` is `date(2026, 10, 20)`.

| Test | Arrange | Assert |
|---|---|---|
| `test_full_run_one_time` | `overspend-one-time` at `ONE`; replies: a diagnosis (`one_time`), then `action="alert"` with a message | 1 alert: `overspend`, `high`, `pending_user`, `cause="one_time"`, `action="alert"`, `proposed_limit_cents is None`; 1 `advice_log` row with `run_id` = the run; `agent_runs.status == "ok"` and the summary has keys `period_id, flags, causes, actions, alert_ids, unbudgeted, uncategorized, sync, llm_failures`; the GENERAL_MERCHANDISE line has `spent_cents > 60000` and `last_pace` is set |
| `test_second_run_reuses_budget_and_alerts_again` | run twice | 1 period; 2 alerts; 2 `agent_runs` |
| `test_no_history_no_budget` | a user with only October transactions | status `no_budget`; model `seen == []`; 0 alerts |
| `test_period_ended` | run once at `ONE`, then at `date(2026, 11, 3)` | second status is `period_ended`; no new alerts |
| `test_last_day_of_period_runs` | a run at `date(2026, 10, 31)` | status `ok` |
| `test_no_flags_no_llm` | a user (factories) with only rent of $1,500 on the 1st of Jul-Oct, run at `ONE` | status `ok`; `seen == []`; the rent line `spent_cents == 150000` |
| `test_adjust_stores_proposed_limit` | `overspend-recurring` at `ONE`; `action="adjust_next_budget"` | `alert.proposed_limit_cents == proposed_limit_cents(facts)` and is a multiple of 500 |
| `test_no_op_writes_nothing` | `overspend-recurring` at `ONE`; `action="no_op"` | 0 alerts, 0 advice; `actions["PERSONAL_CARE"]["action"] == "no_op"` |
| `test_diagnose_failure_still_alerts` | `overspend-one-time`; two `"bad"` diagnose replies, then a valid action | alert has `cause is None`; `llm_failures == 1` |
| `test_decide_failure_fallback` | `overspend-one-time`; a valid diagnosis, then two `"bad"` replies | `action == "alert"`; the message starts with `"General merchandise is over its $"`; `actions[...]["fallback"] is True` |
| `test_sync_called_and_failure_recorded` | `sync_fn` records calls; a second run's `sync_fn` raises `RuntimeError` | called once with the user id; second run `ok`, with `summary["sync"]["errors"] == ["RuntimeError"]` |
| `test_no_sync_fn_skipped` | | `summary["sync"] == {"skipped": True}` |
| `test_crash_marks_error` | monkeypatch `budget_graph.evaluate` to raise `RuntimeError` | `pytest.raises(RuntimeError)`; the run row has `status == "error"` and `summary_json == {"error": "RuntimeError"}` |
| `test_prompt_privacy` | full run | the first model call's text contains the audit pseudonym and `"ElectroMart"` and not the user id; the audit file has exactly 1 `diagnose` line |
| `test_unbudgeted_listed` | add a TRAVEL purchase on Oct 15 to `overspend-one-time` | `"TRAVEL" in summary["unbudgeted"]`; no TRAVEL alert |
| `test_live_planted_causes` | `@pytest.mark.skipif(not ollama_has(config.OLLAMA_MODEL), ...)`; each `PLANTED` profile at its `run_date` with the real model | the planted category's diagnosis cause is `expected_cause`, and its action is not `no_op` |

- [ ] **Step 2: Run and confirm they fail** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement.**
  - Nodes are closures in `build_budget_graph`, and each opens `with session_factory() as s:`.
  - `run_budget` inserts the `AgentRun`, builds one gateway (`PrivacyGateway(s, user_id, audit_path=audit_path)`; its session is used only by `build_context`), invokes the graph with `{"user_id", "today", "run_id"}`, and handles errors as the spec says.
  - Flags are processed in `results` order.
  - The fallback message follows Global Constraints.
- [ ] **Step 4: Run `tests/test_budget_graph.py` and confirm it passes** (the live test is skipped without Ollama). Then run the full suite.
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): budget agent graph from pace to alerts and advice`.

---

### Task 7: CLI

**Files:** Modify `backend/app/agents/budget_graph.py` (`main`, `if __name__ == "__main__"`); create `backend/tests/test_budget_graph_cli.py` and `docs/tests/test_budget_graph_cli.md`.

**Interfaces:**
- Produces: `main(argv=None, session_factory=None, model=None) -> int`.
- Flags: `--user` (required), `--date` (ISO, default today), `--no-sync`, `--model`.
- Without `--no-sync`, `sync_fn=plaid_sync`.

- [ ] **Step 1: Write the failing tests**
  - `test_cli_prints_report`: the `overspend-one-time` seed, a scripted model, `["--user", uid, "--date", "2026-10-20", "--no-sync"]` → returns 0. Output contains `"GENERAL_MERCHANDISE"`, `"one_time"`, `"alert"`, `"status: ok"` and `"alerts written: 1"`.
  - `test_cli_error_exit`: the model's first reply is `ConnectionError("down")` → returns 1, and the output contains `"ConnectionError"`.
  - `test_cli_bad_date`: `--date 2026-13-01` → `SystemExit` with code 2 (argparse).
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement**, with the printed layout as in the spec. **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): budget graph cli`.

---

### Task 8: Seed script

**Files:** Create `backend/scripts/seed_budget_user.py`, `backend/tests/test_seed_budget_user.py` and `docs/tests/test_seed_budget_user.md`.

**Interfaces:**
- Produces: `main(argv=None, session_factory=None) -> int`, and `delete_user(session, user_id) -> None` (FK order as in the spec).
- Path setup: put `REPO/backend` and `REPO` on `sys.path`, then `FERNET_KEY` setdefault, as in `eval/run_eval.py`.
- Tests import it as `from scripts.seed_budget_user import main`.

- [ ] **Step 1: Write the failing tests**
  - `test_seed_writes_rows`: returns 0; transactions in the database == `len(transactions(profile))`; the output contains `"--date 2026-10-20"`.
  - `test_seed_refuses_existing`: a second call returns 1, with the same transaction count.
  - `test_replace_deletes_and_reseeds`: seed, `run_budget` with a scripted model (which creates a period, an alert, advice and a run), then `--replace` → returns 0; the same transaction count; 0 periods, alerts, advice and runs for that user.
  - `test_unknown_profile`: returns 2, and the output lists `"overspend-one-time"`.
- [ ] **Step 2: Run and confirm they fail.** **Step 3: Implement.** **Step 4: Run and confirm they pass.**
- [ ] **Step 5: Doc entries and commit.** Commit message: `feat(TPC-19): seed_budget_user script for planted demo users`.

---

### Task 9: Live demo and docs

**Files:** Modify `docs/architecture.md`.

- [ ] **Step 1: Live check.** Run `../venv/bin/python -m pytest tests/test_budget_graph.py::test_live_planted_causes -v -s` with Ollama up.
  - Expected: PASS for all 3 profiles.
  - If a cause is wrong, adjust the prompt wording only (spec risk table), and record a ruling with the before and after.
- [ ] **Step 2: Demo on dev.db:**
```bash
../venv/bin/python scripts/seed_budget_user.py --profile overspend-one-time --replace
../venv/bin/python -m app.agents.budget_graph --user budget-overspend-one-time --date 2026-10-20 --no-sync
```
  Expected: `status: ok`, one GENERAL_MERCHANDISE flag, cause `one_time`, an action, and `alerts written: 1`. Save the output for the decision log.
- [ ] **Step 3: Update `docs/architecture.md`.**
  - Sections 5, 6.1-6.2, 6.4, 7 and 12 as the spec's Docs section lists.
  - Decision-log rows (2026-10-28) for each spec decision, plus the live result.
  - Section 15 follow-ups:
    - LLM categorization;
    - checking that a message's numbers appear in the facts;
    - TPC-20 deduplication;
    - **pace false alarms on lumpy flexible spending** (a monthly subscription early in the month, weekday counts). The plan-time check: a steady user flags ENTERTAINMENT 1.48 and TRANSPORTATION 1.12 on Aug 20.
- [ ] **Step 4: Commit.** Commit message: `docs(TPC-19): architecture, decision log and live budget agent result`.
