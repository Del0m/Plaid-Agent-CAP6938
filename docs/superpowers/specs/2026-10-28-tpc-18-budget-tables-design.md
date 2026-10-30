# TPC-18: Budget tables and `initialize_budget`

Jira: TPC-18 (epic TPC-4, M3). Branch: `TPC-18`, cut from `TPC-17`.

## Goal

Give the budget agent somewhere to keep its state, and build the user's first budget from history (architecture 5 and 6.3):
- **Tables:** one Alembic revision for `budget_periods`, `budget_lines`, `alerts`, `advice_log` and `agent_runs`. TPC-18 only writes the first two. The rest exist so TPC-19, TPC-20 and TPC-22 don't each need a migration.
- **`initialize_budget`:** loads the user's history, calls TPC-17's `fit_budget`, and stores an open period with its lines and the evidence behind each limit (monthly totals and outliers).
- **Accuracy:** a reusable eval script that fits synthetic users and scores the initial budget against the next month's actual spending, median vs mean.

**Done when:**
- a synthetic user gets a sensible first budget (the eval script prints it);
- initial budget accuracy (MAPE) for median vs mean is recorded in the decision log.

**Out of scope:**
- filling `spent_cents` / `last_pace` (TPC-19's `update_totals`);
- graph nodes (TPC-19): `initialize_budget` is a plain function that TPC-19 wraps;
- writing alerts, advice or agent runs (TPC-19, TPC-20);
- `alerts.stream_id` and `recurring_streams` (TPC-33);
- a dev.db CLI.

## Decisions

| Decision | Why |
|---|---|
| All five tables in one revision, with the architecture 5 columns except `alerts.stream_id` | The ticket asks for them; later tickets write rows instead of migrating. TPC-33 adds `stream_id` together with its foreign key to `recurring_streams` |
| Small fixed value sets are SQLAlchemy `Enum`s (period `status`, line `kind`, line `source`); alert `kind`/`cause`/`action`/`status`/`severity` and `advice_log.outcome` are plain strings, with `Literal` aliases in `models.py` wherever architecture 5 lists the values (not `severity`, which TPC-19 defines) | Alert kinds and actions grow in TPC-31/32/33; a new value shouldn't need a migration (a Postgres native enum needs `ALTER TYPE`) |
| Fit evidence is stored as JSON: `budget_lines.fit_json` (monthly totals, outliers with their top transactions) and `budget_periods.fit_json` (window, skipped) | TPC-19's `diagnose` reads the explanation off the line it is diagnosing; re-fitting later could drift from what the budget was built from. Same pattern as `users.prefs_json` |
| Unique `(user_id, start_date)` on periods and `(period_id, category)` on lines | Idempotency is a database guarantee: a re-run or a scheduler/manual race can't create two October periods or two FOOD lines |
| `initialize_budget` runs only when the user has **no** period at all; otherwise it returns the most recent one | Architecture 6.2: "only if no budget exists". Opening the next month is TPC-22's close, not a re-fit |
| No lines fitted (no complete month, or everything skipped) → create nothing, return `None` | The next daily run tries again once history exists; an empty period would block that forever |
| New `app/budget_service/` package for DB-facing budget code | Keeps `budget_engine/` pure (no DB, no clock). TPC-19's `update_totals` and TPC-22's close reuse `load_txns` |
| **A fixed line is the median of the monthly totals, from the first non-zero month onward** (supersedes the 2026-10-27 "latest non-zero month" rule) | Rent paid early puts two rents in one month: `[1500, 3000, 0]` and `[1500, 1500, 3000]` both gave $3,000 under the old rule, 100% off next month. The median gives $1,500 for both, and $1,500 for a new lease `[0, 0, 1500]`. Cost: a rent increase `[1500, 1500, 1600]` fits at $1,500, and close (fixed follows actual) adopts $1,600 one period later |
| `fit_budget` takes `center="median"\|"mean"`, affecting **flexible** lines only | The architecture 6.3 learning exercise compares the two; fixed lines stay on the median rule so the runs differ in exactly one thing |
| Budget accuracy is an eval script over generated synthetic users, not a pytest | It's the "initial budget accuracy" metric from architecture 10.2, which M5 grows; profiles defined as patterns are cheap to add and can't have hand-summed mistakes |

## Files

```
backend/alembic/versions/<rev>_budget_tables.py   the five tables
backend/app/db/models.py                         BudgetPeriod, BudgetLine, Alert, AdviceLog, AgentRun + Literal aliases
backend/app/budget_engine/fit.py                 center parameter, FitResult.window, fixed = median
backend/app/budget_service/
├── __init__.py
└── initialize.py                                load_txns, initialize_budget
eval/metrics.py                                  + mape
eval/budget_users.py                             patterns, profiles, seed_profile
eval/budget_accuracy.py                          the median-vs-mean run
```

## Schema

```
budget_periods  id, user_id→users (index), start_date, end_date,
                status Enum(open|closed, name="budget_period_status") default open,
                fit_json JSON null, created_at
                UNIQUE (user_id, start_date)

budget_lines    id, period_id→budget_periods (index), category,
                kind Enum(fixed|flexible, name="budget_line_kind"), limit_cents,
                spent_cents default 0, last_pace float null,
                source Enum(init|adapted|user, name="budget_line_source"),
                fit_json JSON null
                UNIQUE (period_id, category)

alerts          id, user_id→users (index), period_id→budget_periods null, kind str, category str null,
                severity str, cause str null, action str null, message str,
                status str default "pending_user", created_at

advice_log      id, user_id→users (index), run_id→agent_runs null, category str null,
                advice_text str, created_at, outcome str null

agent_runs      id, user_id→users (index), graph str, started_at, finished_at null,
                status str, summary_json JSON default {}
```

- `alerts.period_id` is nullable: a `low_balance` or `new_subscription` alert isn't tied to a budget period.
- `advice_log.run_id` is nullable: advice can be written outside a graph run (accept/dismiss in TPC-20).
- `created_at` / `started_at` default to `utcnow`, as in the existing models.
- `Literal` aliases in `models.py`, with the architecture 5 values: `AlertKind`, `AlertCause`, `AlertAction`, `AlertStatus`, `AdviceOutcome`. Nothing in TPC-18 writes these columns; TPC-19/20 type against them.

`fit_json` shapes (dates are ISO strings, amounts integer cents):

```python
# budget_periods.fit_json
{"window": ["2026-07-01", "2026-08-01", "2026-09-01"], "skipped": ["SHOPPING"]}

# budget_lines.fit_json
{"monthly_totals": [45000, 190000, 25000],
 "outliers": [{"month": "2026-08-01", "total_cents": 190000, "median_cents": 45000,
               "top_txns": [{"date": "2026-08-14", "amount_cents": 120000, "merchant": "Delta"}]}]}
```

`budget_periods.fit_json` is `null` for a period TPC-22's close opens.

## `fit.py` changes

```python
@dataclass(frozen=True)
class FitResult:
    lines: tuple[FittedLine, ...]
    skipped: tuple[str, ...]
    window: tuple[date, ...]          # new: the fitted months (first of each), oldest first; () when empty

def fit_budget(txns, today, fixed=FIXED_DEFAULT, center: Literal["median", "mean"] = "median") -> FitResult
```

- **Fixed:** `limit` = the median of the window totals from the first month with a total above 0 to the end of the window, rounded with `round_cents`. No markup, no outliers.
- **Flexible:** `center` = `statistics.median` of the window totals (zeros included), or for `"mean"` exactly `Decimal(sum(totals)) / len(totals)`. If `center == 0` the category is skipped (only possible for the median). `limit = round_cents(center × 1.05)`. Outliers are months above `1.5 × center`.
- `OutlierMonth.median_cents` keeps its name; a comment says it holds the center, which is always the median in production.

## `initialize.py`

```python
def load_txns(session, user_id: str, start: date, end: date) -> list[Txn]
def initialize_budget(session, user_id: str, today: date,
                      fixed: frozenset[str] = FIXED_DEFAULT) -> Optional[BudgetPeriod]
```

**`load_txns`**: the user's transactions with `removed=False`, dated `start..end` inclusive, as `Txn`s:
- `category = our_category or pf_category_primary or "UNCATEGORIZED"`. Nothing writes `our_category` yet; once the `categorize` node does (architecture 6.2), its category wins.
- `detailed = pf_category_detailed`, `merchant = merchant_name`.
- Pending transactions are included (TPC-17 spec).

**`initialize_budget`**:
1. If the user has any `budget_period`, return the most recent (by `start_date`) without fitting.
2. `txns = load_txns(session, user_id, add_months(month_start(today), -3), month_start(today) - 1 day)`. `fit_budget`'s earliest-date clamp still shrinks the window for short history.
3. `fit = fit_budget(txns, today, fixed)`. If `fit.lines` is empty, return `None` and write nothing.
4. Add the period (`month_start(today)..month_end(today)`, `open`, `fit_json = {window, skipped}`) and one line per `FittedLine` (`source="init"`, `spent_cents=0`, `last_pace=None`, `fit_json` from the line).
5. Commit. On `IntegrityError` (a race on the unique period), roll back and return the existing period.

## Budget accuracy

**`eval/metrics.py`**: `mape(pairs: list[tuple[int, int]]) -> Optional[float]`, pairs of `(limit_cents, actual_cents)` with `actual > 0`. Returns the mean of `|limit − actual| / actual` as a percentage rounded to 1 decimal, or `None` for no pairs.

**`eval/budget_users.py`**:
- Patterns: `Weekly(category, merchant, cents, jitter)`, `Monthly(category, merchant, cents, day, overrides)` (`overrides` maps a month to a different amount or a different day), `Once(category, merchant, cents, day)`. A `Profile` has an `id`, a description, `today`, a seed and its patterns; amounts come from a `random.Random(seed)`.
- Every profile has a paycheck (`INCOME`) and a savings transfer (`TRANSFER_OUT`), so the exclusions are exercised.
- `today = 2026-08-01` for every profile: fit on May-July, score on August. Transactions run from May 1 to Aug 31.
- `seed_profile(session, profile) -> user_id` writes the user, one checking account and the transactions through `tests/factories.py`.

| Profile | Planted | Expected to favour |
|---|---|---|
| `steady` | groceries ~$110/week, dining ~$25 twice a week, rent $1,500 on the 1st | tie |
| `vacation` | steady + Travel $200 / **$1,900** / $250, then $220 in August | median |
| `early-rent` | steady, but July's rent is paid Jun 30 (June $3,000, July $0) | fixed rule check |
| `rising` | groceries up 10% each month | mean, slightly |
| `occasional` | steady + Shopping $300 once in June, $0 in August | median (skips it) |

**`eval/budget_accuracy.py`** (`venv/bin/python eval/budget_accuracy.py`):
1. For each profile: seed a `temp_db()`, run `initialize_budget` and print the stored budget (category, kind, limit, outliers, skipped). This is the "sensible first budget" check.
2. Fit with `center="median"` and `center="mean"` on `load_txns` over the window, and score each against `category_totals(spending(load_txns(August)))`:
   - `mape` over lines whose category has August spending above 0;
   - `zero_actual_lines`: lines whose category had $0 in August (they can't be scored);
   - `unbudgeted`: categories with August spending and no line.
3. Print a per-profile table and a pooled MAPE (over every scored line of every profile) per center.
4. Write `results.json` (per profile and pooled, both centers, plus the git commit) to `results_dir(RESULTS_DIR, date.today(), "budget_accuracy")`, which adds `-2` on a second run the same day.

No LLM calls; it runs in seconds.

## Tests (`backend/tests/`, each with a `docs/tests/<module>.md`)

**`test_budget_fit.py`** (changed)
- Early rent, realistic: `[1500, 3000, 0]` → $1,500 (replaces `test_fixed_paid_early_uses_previous_month`).
- Double payment in the last window month: `[1500, 1500, 3000]` → $1,500.
- New lease: `[0, 0, 1500]` → $1,500.
- Rent increase: `[1500, 1500, 1600]` → $1,500 (pins the trade-off; close adopts it).
- `center="mean"`: Travel $200/$1,900/$250 → $822.50; an occasional category gets a line instead of being skipped.
- `FitResult.window`: the 3 months oldest first, shrunk for short history, `()` when empty.

**`test_budget_service.py`** (new; `db` fixture + factories)
- 3 months of history → an open period for the current month; lines with `source="init"`, `spent_cents=0` and `fit_json`; outlier top transactions stored as ISO dates and integer cents.
- The period's `fit_json` has `window` and `skipped`.
- `load_txns`: removed transactions excluded; `our_category` wins over `pf_category_primary`; `None` becomes `UNCATEGORIZED`; another user's transactions ignored; the date range is inclusive.
- Idempotency: a second call returns the same period and adds no rows; an older existing period is returned and no new one is opened.
- No complete month, or every category skipped → `None` and no rows.
- Race: with the existence check patched to miss, the commit hits the unique constraint and the call returns the existing period with no stray lines.
- A duplicate `(period_id, category)` line raises `IntegrityError`.

**`test_eval_metrics.py`** (added): `mape` on a known case, `None` for no pairs, rounding to 1 decimal.

**`test_budget_users.py`** (new): the same seed gives the same transactions; planted facts hold (June Travel = $1,900 in `vacation`, June rent = $3,000 in `early-rent`); `seed_profile` writes the user, account and rows.

**`test_budget_accuracy.py`** (new): the scorer counts `zero_actual_lines` and `unbudgeted`; an end-to-end run on two profiles writes JSON with both centers, and median MAPE is below mean MAPE on `vacation`.

**Migration check** (verification, not pytest): on a scratch SQLite file, `alembic upgrade head`, then `alembic check` reports no drift from the models, then `alembic downgrade -1` runs cleanly.

## Docs

- Architecture:
  - 5: the `fit_json` columns, the unique constraints, `stream_id` arriving with TPC-33;
  - 6.3: the fixed rule (median from the first non-zero month) and `initialize_budget`'s behaviour;
  - 10: an "as built" note for budget accuracy;
  - 13: `budget_service/`.
- Decision log (2026-10-28): fixed line = median from the first non-zero month (supersedes 2026-10-27); fit evidence stored as JSON; string alert columns vs Enum for stable sets; the median vs mean MAPE results.
- `eval/README.md`: running `budget_accuracy.py` and adding a profile.

## Risks

| Risk | Mitigation |
|---|---|
| Five profiles are a thin basis for the median vs mean result | Reported as a first measurement; M5 adds profiles and the script needs no change |
| A rent increase isn't adopted at init | Close follows actual for fixed lines, so it's adopted one period later; pinned by a test and noted in the decision log |
| The schema for alerts, advice and runs is designed before anything writes it | Plain string columns and nullable foreign keys keep TPC-19/20 changes additive |
| SQLite batch migrations differ from Postgres | Plain `create_table` only; the migration check runs upgrade and downgrade |
