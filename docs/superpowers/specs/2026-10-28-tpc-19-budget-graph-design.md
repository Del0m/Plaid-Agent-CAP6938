# TPC-19: Budget agent graph with structured outputs

Jira: TPC-19 (epic TPC-4, M3). Branch: `TPC-19`, cut from `TPC-18`.

## Goal

The daily budget agent (architecture 6.1-6.2) as a LangGraph graph. It runs on one user for one date:
1. loads or initializes their budget;
2. updates each line's spending and pace;
3. flags overspending;
4. has the LLM diagnose each flag's cause and pick an action;
5. writes alerts and advice;
6. records the run.

The maths stays in `budget_engine` (TPC-17). The LLM classifies and writes text from facts the code computed.

**Done when:** a CLI run on a synthetic user with a planted overspend produces the right cause and an action. Three planted users, one per cause, are checked live against `qwen2.5:14b`.

**Out of scope:**
- reading `advice_log` back into `decide_action`, alert deduplication, `interrupt()`, and accept/dismiss (TPC-20). Until TPC-20 lands, a second run on the same overspend alerts again;
- the scheduler and sandbox time-advance (TPC-21);
- period close and opening the next period (TPC-22);
- the `detect_recurring` and `forecast_cash` nodes (TPC-33, TPC-31);
- LLM categorization of uncategorized transactions (follow-up).

## Decisions

| Decision | Why |
|---|---|
| Hand-built `StateGraph`, with `diagnose` and `decide_action` looping over the flags inside the node | Same pattern as the chat graph. With 0-3 flags a day, `Send` fan-out adds merge logic for no gain, since the local model serves one request at a time |
| **`diagnose` gets facts computed by code** (largest charge's share, merchants new vs repeating, history, month) plus the category's transactions; the LLM picks the cause and writes the evidence | A 14B local model classifies unreliably from raw transactions. Computed facts are unit-testable and keep the prompt small. The LLM still makes the call, as architecture 6.2 says |
| **Code computes the proposed limit** for `adjust_next_budget` (projection rounded up to the next $5) and stores it in a new `alerts.proposed_limit_cents`; the LLM only picks the action | The core rule: the LLM never does arithmetic that matters. TPC-22's close applies exactly the number the user accepted |
| An LLM call that fails to parse after one retry falls back: `diagnose` → `cause = null`; `decide_action` → `alert` with a message the code writes. Failures are counted in the run summary | An overspend must never go unreported because the model produced bad JSON |
| `invoke_structured` binds Ollama's `format` to the Pydantic model's JSON schema and validates the reply text itself, instead of using `with_structured_output` | Constrained decoding on the real model; works the same with the scripted fake model; the retry controls parsing directly |
| `categorize` only counts uncategorized transactions in TPC-19 | Plaid Sandbox and synthetic users are always categorized; nothing in the done-when exercises it, and M3's date is today. LLM categorization is a listed follow-up |
| Unbudgeted spending is listed in the run summary, not alerted on | TPC-17 left this to TPC-19. A category without a line isn't overspent against anything; suggesting new lines belongs with close (TPC-22) |
| The sync step is a function passed in; without one (`--no-sync`, synthetic users) the node does nothing. A sync failure is recorded and the run continues on stored data | Synthetic users have no real Plaid item. A Plaid outage shouldn't stop the budget check |
| The demo runs on `dev.db`: `scripts/seed_budget_user.py` seeds a planted profile, then the CLI runs on it | The user's choice: closest to real use. `--replace` re-seeds cleanly |
| Severity: `over_limit` → `high`, pace flag → `medium` | TPC-18 left `severity` to TPC-19; two levels match the two flag reasons |

## Files

```
backend/alembic/versions/<rev>_alert_proposed_limit.py   alerts.proposed_limit_cents
backend/app/db/models.py                                 Alert.proposed_limit_cents, AlertSeverity
backend/app/budget_engine/diagnose.py                    OverspendFacts, overspend_facts, projected_cents, proposed_limit_cents
backend/app/privacy/gateway.py                           purpose "diagnose"
backend/app/agents/budget_llm.py                         Diagnosis, Action, prompts, invoke_structured
backend/app/agents/budget_graph.py                       BudgetState, nodes, build_budget_graph, run_budget, CLI
backend/scripts/seed_budget_user.py                      seed a profile into dev.db
eval/budget_users.py                                     PLANTED profiles
```

## Flow

```
load_state ─┬─(no_budget / period_ended)──────────────────────────────────► persist_state
            └► sync → categorize → update_totals → detect_overspend ─┬─(no flags)─► persist_state
                                                                     └► diagnose → decide_action → log_advice → persist_state
```

**State.** Plain, serializable values, so TPC-20 can add a checkpointer:

```python
class BudgetState(TypedDict, total=False):
    user_id: str
    today: str                 # ISO date
    run_id: int
    period_id: Optional[int]
    status: str                # running | ok | no_budget | period_ended | error
    sync: dict                 # {"items": n, "errors": [...]} or {"skipped": True}
    uncategorized: int
    results: list[dict]        # every PaceResult, as dicts
    flags: list[dict]          # the flagged ones
    facts: dict[str, dict]     # category -> OverspendFacts as dict
    diagnoses: dict[str, Optional[dict]]   # category -> Diagnosis, None after a failed call
    actions: dict[str, dict]   # category -> {action, message, proposed_limit_cents, fallback}
    alert_ids: list[int]
    llm_failures: int
```

**Nodes**

| Node | Does |
|---|---|
| `load_state` | Calls `initialize_budget(session, user_id, today)`; the `agent_runs` row (`graph="budget"`, `status="running"`) already exists, created by `run_budget`. **Routing:** `None` → status `no_budget`. A latest period with `end_date < today` → status `period_ended`; closing it is TPC-22's job. Otherwise it records the period id |
| `sync` | `sync_fn(session, user_id)` if one was passed: the default syncs each of the user's `active` Plaid items with `sync_item`. It records the item count and errors. With no `sync_fn`, it records `{"skipped": True}` |
| `categorize` | Counts this period's transactions (up to today) whose category maps to `UNCATEGORIZED` |
| `update_totals` | `evaluate(lines, load_txns(session, user_id, period.start_date, today), today)`. It writes `spent_cents` and `last_pace` to each line and commits |
| `detect_overspend` | `flags` = the results with `flagged=True`. Unbudgeted results stay only in `results`. **Routing:** no flags → `persist_state` |
| `diagnose` | For each flag: `overspend_facts(...)`, then `gateway.build_context("diagnose", facts=..., transactions=...)`, then `invoke_structured(model, Diagnosis, ...)` |
| `decide_action` | For each flag: `invoke_structured(model, Action, ...)` with the facts, the diagnosis, the projection and the proposed limit. The code adds `proposed_limit_cents` only when the action is `adjust_next_budget`. **On failure:** action `alert`, with the message `"{Category} is at {pace:.0%} of its pace: ${spent} of ${limit} by day {d}."`, or `"…over its ${limit} limit: ${spent} so far."` for an over-limit flag |
| `log_advice` | For each action that isn't `no_op`: one `Alert` (`kind="overspend"`, `period_id`, `category`, `severity`, `cause`, `action`, `message`, `proposed_limit_cents`, `status="pending_user"`) and one `AdviceLog` (`run_id`, `category`, `advice_text=message`). Commits |
| `persist_state` | Sets `finished_at`, `status` (`ok` unless already `no_budget`/`period_ended`) and `summary_json`: `{period_id, flags: [{category, reason, pace, spent_cents, limit_cents}], causes, actions, alert_ids, unbudgeted, uncategorized, sync, llm_failures}`. Commits |

```python
def build_budget_graph(session_factory, *, model=None, sync_fn=None, audit_path=None)
def run_budget(session_factory, user_id: str, today: date, *, model=None, sync_fn=None) -> dict   # final state
```

- `run_budget` inserts the `agent_runs` row itself, before invoking the graph, so a crash in any node, `load_state` included, still has a row to mark. `load_state` receives the `run_id` in the initial state. On any exception, `run_budget` rolls back, marks that row `error` with `{"error": type name}` in its summary, and re-raises.
- Each node opens its own session from `session_factory`.
- **The gateway** is one `PrivacyGateway` per run, so a run has one pseudonym.

## `budget_engine/diagnose.py`

```python
@dataclass(frozen=True)
class MerchantFact:
    merchant: Optional[str]
    period_cents: int            # net spend this period, up to today
    charges: int                 # purchases this period
    prior_months: int            # how many of the 3 months before the period had a purchase here
    typical_cents: Optional[int] # median monthly spend here over those prior months that had one

@dataclass(frozen=True)
class OverspendFacts:
    category: str
    reason: Literal["pace", "over_limit"]
    kind: Literal["fixed", "flexible"]
    month: date                  # period start
    limit_cents: int
    spent_cents: int
    pace: Optional[float]
    days_elapsed: int
    days_in_period: int
    projected_cents: int
    largest: Optional[Txn]       # biggest purchase this period
    largest_share: float         # largest.amount / spent, 0 when spent is 0
    merchants: tuple[MerchantFact, ...]   # top 5 by period_cents, then name
    monthly_totals: tuple[int, ...]       # from the line's fit_json, oldest first ((), if none)
    outlier_months: tuple[date, ...]      # from the line's fit_json

def overspend_facts(flag: PaceResult, line_fit: Optional[dict], period_txns, history_txns, period_start: date) -> OverspendFacts
def projected_cents(spent_cents: int, days_elapsed: int, days_in_period: int) -> int
def proposed_limit_cents(facts: OverspendFacts) -> int
```

- `period_txns` and `history_txns` are the user's `Txn`s. The function applies `spending()` itself and filters to the flag's category.
  - **Period:** `period_start..today` (`today` = period start + days elapsed − 1).
  - **History:** the 3 complete months before `period_start`.
- `projected_cents` = `round_cents(spent × days_in_period / days_elapsed)`.
- **`proposed_limit_cents`:**
  - flexible: the projection rounded **up** to a multiple of 500 (an exact multiple stays);
  - fixed: `spent_cents`.
- **Merchant grouping:** by the exact `merchant` string, with `None` grouped as one unnamed merchant.

## Gateway purpose `diagnose`

`build_context("diagnose", facts=OverspendFacts, transactions=list[Txn])` returns, inside the usual `{user, purpose, data}` envelope:

```json
{"category": "GENERAL_MERCHANDISE", "reason": "over_limit", "kind": "flexible", "month": "October 2026",
 "limit": 157.5, "spent": 704.12, "pace": 6.92, "day": 20, "days_in_month": 31, "projected": 1091.39,
 "largest": {"date": "2026-10-12", "amount": 600.0, "merchant": "ElectroMart", "share_of_spent": 0.85},
 "merchants": [{"merchant": "ElectroMart", "this_month": 600.0, "charges": 1, "months_seen_before": 0, "typical": null}],
 "history": [{"month": "July 2026", "total": 148.2}, ...], "outlier_months": [],
 "transactions": [{"date": "2026-10-12", "amount": 600.0, "merchant": "ElectroMart"}]}
```

- Only these keys are sent. Amounts are dollars, and merchants go through `scrub_merchant`. `transactions` is the flag's category only, newest first, capped at `TXN_LIMIT_DIAGNOSE = 20`.
- `Purpose` gains `"diagnose"`. The audit line's `params` are `{"category": ...}`.

## `agents/budget_llm.py`

```python
class Diagnosis(BaseModel):
    cause: Literal["one_time", "seasonal", "recurring"]
    evidence: str              # one sentence, citing the facts

class Action(BaseModel):
    action: Literal["alert", "suggest_cuts", "adjust_next_budget", "no_op"]
    message: str               # at most 2 sentences, addressed to the user

def invoke_structured(model, schema: type[BaseModel], messages, retries: int = 1) -> Optional[BaseModel]
```

- **`invoke_structured`:**
  - calls `model.bind(format=schema.model_json_schema()).invoke(messages)`, then `schema.model_validate_json(reply.content)`;
  - on a `ValidationError` or an invalid JSON reply, it tries again with the same messages, up to `retries` times, then returns `None`;
  - other exceptions (Ollama down) propagate.
- **`DIAGNOSE_PROMPT`:**
  - defines the causes:
    - `one_time`: one purchase or a short burst at merchants not seen in earlier months;
    - `recurring`: a charge that repeats monthly and is new or larger since the budget was set;
    - `seasonal`: spending tied to the time of year, such as holidays, back-to-school or summer travel, spread across purchases;
  - and says to use only the facts given and to cite numbers exactly as given.
- **`DECIDE_PROMPT`:**
  - **Inputs:** the facts, the diagnosis, the projected month-end spend and the proposed next-month limit.
  - **Guidance on which action to pick:**
    - `recurring` usually means `adjust_next_budget`;
    - `one_time` usually means `alert`;
    - `suggest_cuts` is for flexible spending that's running hot without one cause;
    - `no_op` only when the overspend is trivial.
  - **The message** is plain language, mentions the proposed limit when the action is `adjust_next_budget`, and gives no financial advice beyond the budget.
- **Prompts are templates over the gateway's context string.** No user data reaches a prompt any other way.

## Seed script and CLI

**`backend/scripts/seed_budget_user.py --profile <id> [--replace]`** (`dev.db`; the same `sys.path` setup as the other scripts):
- It looks the id up in `PROFILES + PLANTED`. An unknown id exits 2, listing the valid ids.
- If `budget-<id>` exists, it exits 1 with a hint, unless `--replace` is passed. With `--replace` it deletes, in foreign-key order, the user's `advice_log`, `alerts`, `agent_runs`, `budget_lines` (via their periods), `budget_periods`, `transactions`, `liabilities`, `accounts`, `plaid_items` and `users` rows, then re-seeds.
- It prints the user id and `python -m app.agents.budget_graph --user budget-<id> --date <run_date> --no-sync`.

**`python -m app.agents.budget_graph --user <id> [--date YYYY-MM-DD] [--no-sync] [--model NAME]`** (`main(argv=None, session_factory=None, model=None) -> int`):
- **Defaults:** `--date` is today, `--model` is `OLLAMA_MODEL`, and the session factory is the app's `SessionLocal`.
- **It prints:**
  - the run id, status and period;
  - one row per line: category, kind, spent, limit and pace;
  - for each flag: reason, cause, evidence, action, message, and the proposed limit if there is one;
  - the alerts written, the unbudgeted categories, and any LLM failures.
- **Exit code:** 0 when the run finishes (any status but `error`); 1 when `run_budget` raised, with the exception type printed.

## Planted profiles (`eval/budget_users.py`)

`Profile` gains `run_date: Optional[date] = None` and `expected_cause: Optional[str] = None`. `PLANTED` is a separate tuple; `PROFILES` (the accuracy eval) is unchanged.

| id | start..end | Patterns (plus `RENT`, `GROCER`, `BASE`) | run_date | expected |
|---|---|---|---|---|
| `overspend-one-time` | Jul 1 - Oct 31 2026 | `Weekly("GENERAL_MERCHANDISE", "Corner Shop", 3500)`; `Once("GENERAL_MERCHANDISE", "ElectroMart", 60000, Oct 12)` | 2026-10-20 | `one_time` |
| `overspend-recurring` | Jul 1 - Oct 31 2026 | `Monthly("PERSONAL_CARE", "Barber", 4000, day=20)`; `Monthly("PERSONAL_CARE", "FitClub Gym", 9000, day=3)`, first charge Sep 3 | 2026-10-20 | `recurring` |
| `overspend-seasonal` | Sep 1 - Dec 31 2026 | `Weekly("GENERAL_MERCHANDISE", "Corner Shop", 3500)`; 8 `Once` purchases of $60-$120 at 8 different shops, Dec 1-14 | 2026-12-15 | `seasonal` |

- `Monthly` gains `first: Optional[date] = None`, so the gym starts on Sep 3.
- The gym starts in the last history month on purpose: earlier, and the median would absorb it and there'd be no overspend.

## Tests (`backend/tests/`, each with a `docs/tests/<module>.md`)

**`test_budget_diagnose.py`**
- **One-time:** `largest` is the $600 purchase, its share of spending is right, and ElectroMart has `prior_months == 0`.
- **Recurring:** the gym has `prior_months == 1` and `typical_cents == 9000`.
- **History:** `month` is the period start; `monthly_totals` and `outlier_months` come from `fit_json`, or are empty when it's `None`.
- **Merchants:** capped at 5 and ordered; a missing merchant name is grouped; refunds net out within a merchant.
- **`projected_cents`:** day 20 of 31 is exact and half-up.
- **`proposed_limit_cents`:** a flexible line rounds up to $5, an exact multiple is unchanged, and a fixed line is `spent`.

**`test_gateway.py`** (added)
- The `diagnose` keys are exactly the allowlist; amounts are dollars.
- A merchant containing an email is scrubbed.
- Only the category's transactions appear, capped at 20, newest first.
- The audit line has the pseudonym and `{"category": ...}`, and no user id.

**`test_budget_llm.py`**
- A valid reply is parsed, and `format` was bound to the schema.
- An invalid first reply is retried and the second is used.
- Two invalid replies give `None`.
- A model exception propagates.

**`test_budget_graph.py`** (the `db` fixture, `seed_profile` on planted profiles, `ScriptedChatModel`, a `tmp_path` audit file)
- **A full run on `overspend-one-time` at 2026-10-20:**
  - one `high` `overspend` alert (`pending_user`) carrying the scripted cause, action and message;
  - an `advice_log` row with the run id;
  - `agent_runs` finished as `ok`, with the summary keys;
  - `spent_cents` and `last_pace` updated.
- **Budget creation:** the first run creates the budget; a second run reuses it, and alerts again (TPC-20 dedupes).
- **Runs that stop early:**
  - no history: `no_budget`, no LLM calls;
  - a date after the period's end: `period_ended`;
  - no flags: `ok`, zero LLM calls.
- **Actions:**
  - `adjust_next_budget` stores the proposed limit, and `alert` leaves it `None`;
  - `no_op` writes no alert and no advice.
- **LLM failures:**
  - two bad diagnose replies: alert with `cause=None` and `llm_failures == 1`;
  - two bad decide replies: an `alert` action with the fallback message.
- **Sync:**
  - `sync_fn` is called once;
  - a raising `sync_fn` is recorded and the run still finishes `ok`;
  - no `sync_fn`: `{"skipped": True}`.
- **Crash:** an exception in a node marks the run `error` and is re-raised.
- **Privacy:** the diagnose prompt sent to the model contains the gateway context (with the pseudonym) and never the user id. The audit file has one `diagnose` line per flag.
- **Unbudgeted:** spending in a category with no line is listed in the summary, with no alert.

**`test_budget_users.py`** (added)
- `overspend-one-time` has $600 at ElectroMart on Oct 12.
- The gym's first charge is Sep 3.
- December has 8 shops in `overspend-seasonal`.
- `PROFILES` is unchanged.

**`test_seed_budget_user.py`**
- Seeding writes the rows.
- A second seed exits 1 and leaves the rows unchanged.
- `--replace` deletes everything, including a run's alerts and budget, and re-seeds with the same counts.
- An unknown profile exits 2.

**`test_budget_graph_cli.py`**
- Arguments parse.
- The printed report has the flag, its cause and its action.
- Exit 0 on a normal run; exit 1 when the run raised.

**Live check** (`test_budget_graph.py::test_live_planted_causes`): skipped unless `ollama_has(config.OLLAMA_MODEL)`. For each `PLANTED` profile, the real model gives `expected_cause` and an action other than `no_op`.

**Migration check:** upgrade, `alembic check` (no drift), downgrade, upgrade, on a scratch SQLite file.

## Docs

- **Architecture:**
  - 5: `alerts.proposed_limit_cents`, and the severity values;
  - 6.1-6.2: the state and nodes as built, the fallbacks, and that `categorize` only counts;
  - 6.4: unbudgeted spending goes in the summary;
  - 7: the `diagnose` purpose;
  - 12: the CLI and the seed script.
- **Decision log (2026-10-28):**
  - computed facts plus an LLM decision for the cause;
  - the proposed limit is computed by code and stored on the alert;
  - an LLM failure falls back to a plain alert;
  - `invoke_structured` uses Ollama's `format`;
  - LLM categorization is deferred;
  - the live result for the three planted causes.
- **Follow-ups (architecture 15 or the decision log):** LLM categorization; checking that a message's numbers appear in the facts; alert deduplication and `advice_log` read-back (TPC-20).

## Risks

| Risk | Mitigation |
|---|---|
| The live model picks the wrong cause, most likely `seasonal` vs `one_time` | Facts name the month and whether merchants are new; the live test reports which profile failed. If it flips, tune the prompt wording, not the facts' maths |
| A message states a number not in the facts | Prompt says cite only given numbers; follow-up check measured in M5 |
| Duplicate alerts on daily runs before TPC-20 | Documented; TPC-20's done-when is exactly this |
| Ollama down mid-run | The exception propagates; the run is marked `error`; the next daily run retries |
