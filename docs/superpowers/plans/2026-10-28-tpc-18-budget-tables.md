# TPC-18 Budget Tables and `initialize_budget` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store the budget agent's state in five new tables, build a user's first budget from history with `initialize_budget`, and measure initial-budget MAPE for median vs mean on synthetic users.

**Architecture:**
- `budget_engine/fit.py` (pure, TPC-17) gains a fixed-line median rule, a `center` parameter and `FitResult.window`.
- A new `app/budget_service/initialize.py` is the DB side: it loads `Txn`s and stores the fit as a `BudgetPeriod` plus `BudgetLine`s.
- `eval/budget_users.py` generates synthetic users from patterns; `eval/budget_accuracy.py` fits them and scores both centers against the following month.

**Tech Stack:** Python 3.12, SQLAlchemy 2 (typed `Mapped`), Alembic (batch mode, SQLite), pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-28-tpc-18-budget-tables-design.md`

## Global Constraints

- **Running things:** tests run from `backend/` with `../venv/bin/python -m pytest`; Alembic from `backend/` with `../venv/bin/alembic`; the eval script from the repo root with `venv/bin/python eval/budget_accuracy.py`.
- **Money:** integer cents everywhere, Plaid sign (positive = money out). Cents from multiplication or division go through `round_cents` (half up, via `Decimal(str(x))`); never Python's `round()` on money.
- **Purity:** `budget_engine/` stays free of `app.db`, `app.config` and clocks.
- **Constants unchanged:** `FIXED_DEFAULT = frozenset({"RENT_AND_UTILITIES", "LOAN_PAYMENTS"})`, `FLEX_MARKUP = 1.05`, `OUTLIER_RATIO = 1.5`, `HISTORY_MONTHS = 3`.
- **Enum names:** `budget_period_status` (`open`, `closed`), `budget_line_kind` (`fixed`, `flexible`), `budget_line_source` (`init`, `adapted`, `user`).
- **Constraint names:** `uq_budget_periods_user_start` on `(user_id, start_date)`, `uq_budget_lines_period_category` on `(period_id, category)`.
- **`fit_json` shapes:** period `{"window": [iso dates], "skipped": [categories]}`; line `{"monthly_totals": [cents], "outliers": [{"month": iso, "total_cents", "median_cents", "top_txns": [{"date": iso, "amount_cents", "merchant"}]}]}`.
- **Test docs:** every test function gets an entry in `docs/tests/<test module>.md` in the same commit (architecture section 12), in the existing format: intro, run command, `## Shared setup`, then `## \`test_name\`` with **What it does** / **Why it's there**, and a "Not covered" section where relevant.
- **Comments:** short, lowercase, one-line "why" notes, as in the existing files.
- **Commits:** prefixes `feat(TPC-18):`, `docs(TPC-18):`, `test(TPC-18):`. **No Co-Authored-By or "Generated with Claude Code" lines** (user's standing rule for this repo).

## Review Focus

1. **A fixed median that lands on a half cent** (totals `[10001, 10002]` → 10001.5). Expect half-up 10002, not banker's rounding. Pinned by `test_fixed_median_rounds_half_up` (Task 1).
2. **Spending in the current month, including on the 1st** (`today` = Aug 1 and rent posted Aug 1). It must not leak into the fit. Pinned by `test_current_month_ignored` (Task 3).
3. **`fit_json` read back in a fresh session.** A `date` left inside the JSON only fails at flush, and a value that only looks right in the identity map hides it. Pinned by `test_initialize_creates_period_and_lines` reading in a new session (Task 3).
4. **An outlier transaction with no merchant name.** It should store `"merchant": null`, not crash. Pinned by `test_outlier_without_merchant` (Task 3).
5. **A user whose history is only income and transfers.** Expect no budget and no rows, just like no history. Pinned by `test_only_excluded_history_returns_none` (Task 3).

---

### Task 1: Fit changes: fixed median, `center`, `window`

**Files:**
- Modify: `backend/app/budget_engine/fit.py`
- Modify: `backend/tests/test_budget_fit.py`
- Modify: `docs/tests/test_budget_fit.md`

**Interfaces:**
- Produces:
  - `FitResult(lines, skipped, window: tuple[date, ...] = ())`, where `window` is the fitted months (first of each), oldest first;
  - `fit_budget(txns, today, fixed=FIXED_DEFAULT, center: Literal["median", "mean"] = "median") -> FitResult`.

- [ ] **Step 1: Write the failing tests** in `test_budget_fit.py`. Rename `test_fixed_uses_latest_month_no_markup` → `test_fixed_median_no_markup` (body unchanged). Delete `test_fixed_paid_early_uses_previous_month`. Add:

```python
RENT = "RENT_AND_UTILITIES"

def rent(result):
    return line(result, RENT)

def test_fixed_paid_early_double_month():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT), t(2026, 8, 31, 150000, RENT)]
    r = rent(fit_budget(txns, TODAY))
    assert r.monthly_totals == (150000, 300000, 0)
    assert r.limit_cents == 150000

def test_fixed_double_in_last_month():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT),
            t(2026, 9, 1, 150000, RENT), t(2026, 9, 30, 150000, RENT)]
    assert rent(fit_budget(txns, TODAY)).limit_cents == 150000

def test_fixed_new_lease_ignores_leading_zeros():
    # food in july keeps the window at jul-sep, so rent's totals start with two zeros
    txns = [t(2026, 7, 10, 5000), t(2026, 9, 1, 150000, RENT)]
    r = rent(fit_budget(txns, TODAY))
    assert r.monthly_totals == (0, 0, 150000)
    assert r.limit_cents == 150000

def test_fixed_increase_not_adopted_at_init():
    assert rent(fit_budget(monthly(RENT, 150000, 150000, 160000), TODAY)).limit_cents == 150000

def test_fixed_median_rounds_half_up():
    assert rent(fit_budget(monthly(RENT, 10001, 10002, start_month=8), TODAY)).limit_cents == 10002

def test_mean_center_flexible():
    txns = monthly("TRAVEL", 20000, 190000, 25000)
    travel = line(fit_budget(txns, TODAY, center="mean"), "TRAVEL")
    assert travel.limit_cents == 82250               # 235000 / 3 × 1.05
    assert [o.month for o in travel.outliers] == [date(2026, 8, 1)]

def test_mean_center_keeps_occasional():
    result = fit_budget(monthly("SHOPPING", 0, 30000, 0) + NORMAL, TODAY, center="mean")
    assert line(result, "SHOPPING").limit_cents == 10500
    assert result.skipped == ()

def test_mean_center_leaves_fixed_alone():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT), t(2026, 8, 31, 150000, RENT)]
    assert rent(fit_budget(txns, TODAY, center="mean")).limit_cents == 150000

def test_window_reported():
    assert fit_budget(NORMAL, TODAY).window == (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1))
    assert fit_budget(monthly("FOOD_AND_DRINK", 100, 200, start_month=8), TODAY).window == \
        (date(2026, 8, 1), date(2026, 9, 1))
    assert fit_budget([], TODAY).window == ()
    assert fit_budget([t(2026, 10, 2, 100)], TODAY).window == ()
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `../venv/bin/python -m pytest tests/test_budget_fit.py -v`
Expected: the new tests FAIL (`unexpected keyword argument 'center'`, `no attribute 'window'`, and `300000 != 150000` for the double month).

- [ ] **Step 3: Implement in `fit.py`**
  - Add `window: tuple[date, ...] = ()` to `FitResult`, and return `tuple(months)` from the non-empty path.
  - **Fixed:** `limit = round_cents(Decimal(str(statistics.median(totals[first:]))))`, where `first` is the index of the first total above 0. Update the comment: rent paid early doubles one month, and the median sees through it.
  - **Flexible:** the center is `Decimal(str(statistics.median(totals)))` for `"median"`, or `Decimal(sum(totals)) / len(totals)` for `"mean"`. The skip, markup and outlier lines use the center instead of `median`.
  - Comment on `OutlierMonth.median_cents`: holds the center, which is always the median in production.

- [ ] **Step 4: Run the budget suites and confirm they pass**

Run: `../venv/bin/python -m pytest tests/test_budget_fit.py tests/test_budget_pace.py tests/test_budget_close.py tests/test_budget_txns.py -v`
Expected: all PASS (`test_custom_fixed_set` still gives 1500: the median of 1000/2000/1500).

- [ ] **Step 5: Update `docs/tests/test_budget_fit.md`**
  - Intro: fixed lines get the median from the first non-zero month; `center` and `window` exist.
  - Rename the `test_fixed_uses_latest_month_no_markup` entry and replace the `..._paid_early_uses_previous_month` entry.
  - Add an entry for each new test. The "why" lines: rent paid early doubles a month (the bug the old rule had); a rent increase waits for close (the trade-off); mean is the comparison from architecture 6.3.

- [ ] **Step 6: Commit**

```bash
git add backend/app/budget_engine/fit.py backend/tests/test_budget_fit.py docs/tests/test_budget_fit.md
git commit -m "feat(TPC-18): fixed lines take the median, fit reports its window and can centre on the mean"
```

---

### Task 2: Models and migration for the five tables

**Files:**
- Modify: `backend/app/db/models.py`
- Create: `backend/alembic/versions/<rev>_budget_tables.py` (autogenerated, then reviewed)
- Modify: `backend/tests/factories.py`
- Create: `backend/tests/test_budget_service.py` (constraint tests; Task 3 adds the rest)
- Create: `docs/tests/test_budget_service.md`

**Interfaces:**
- Produces (in `app.db.models`):
  - **`BudgetPeriod`:** `id`, `user_id`, `start_date: Date`, `end_date: Date`, `status` (default `"open"`), `fit_json: Optional[dict]`, `created_at`, and `lines: list[BudgetLine]` (relationship, `back_populates="period"`).
  - **`BudgetLine`:** `id`, `period_id`, `category`, `kind`, `limit_cents`, `spent_cents` (default 0), `last_pace: Optional[float]`, `source`, `fit_json: Optional[dict]`, and `period`.
  - **`Alert`, `AdviceLog`, `AgentRun`:** the columns in the spec's Schema section.
  - **`Literal` aliases** with the architecture 5 values: `AlertKind`, `AlertCause`, `AlertAction`, `AlertStatus`, `AdviceOutcome` (`accepted|dismissed|ignored`).
- Produces (in `tests.factories`): `add_txn(s, acct, amount, category, merchant="Shop", day=..., removed=False, detailed=None, our_category=None)`.

- [ ] **Step 1: Write the failing tests** in `test_budget_service.py`

```python
from datetime import date
import pytest
from sqlalchemy.exc import IntegrityError
from app.db.models import BudgetLine, BudgetPeriod
from tests.factories import add_user

def period(user_id="u1", start=date(2026, 10, 1)):
    return BudgetPeriod(user_id=user_id, start_date=start, end_date=date(2026, 10, 31))

def test_one_period_per_user_month(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        s.add_all([period(), period()])
        with pytest.raises(IntegrityError):
            s.commit()

def test_one_line_per_category(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        p = period(); s.add(p); s.flush()
        s.add_all([BudgetLine(period_id=p.id, category="FOOD_AND_DRINK", kind="flexible", limit_cents=1, source="init")
                   for _ in range(2)])
        with pytest.raises(IntegrityError):
            s.commit()

def test_period_defaults(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        p = period(); s.add(p); s.flush()
        s.add(BudgetLine(period_id=p.id, category="TRAVEL", kind="flexible", limit_cents=100, source="init"))
        s.commit()
        assert p.status == "open" and p.fit_json is None
        assert p.lines[0].spent_cents == 0 and p.lines[0].last_pace is None
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `../venv/bin/python -m pytest tests/test_budget_service.py -v`
Expected: FAIL with `ImportError: cannot import name 'BudgetLine'`.

- [ ] **Step 3: Add the five models to `models.py`**
  - Follow the existing style: typed `Mapped`, `mapped_column(ForeignKey(...), index=True)` on every `user_id`/`period_id`, and `created_at`/`started_at` defaulting to `utcnow`.
  - `JSON` for `fit_json` and `summary_json` (`default=dict` on `summary_json` only).
  - Named `UniqueConstraint`s in `__table_args__`.
  - The `Literal` aliases go above `Alert`, with a one-line comment: the columns are plain strings so a new kind needs no migration.

- [ ] **Step 4: Add `detailed` and `our_category` keyword arguments to `tests/factories.add_txn`**, passed to `pf_category_detailed` and `our_category`.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `../venv/bin/python -m pytest tests/test_budget_service.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Generate the migration**

Run (from `backend/`): `../venv/bin/alembic upgrade head && ../venv/bin/alembic revision --autogenerate -m "budget tables"`

Review the generated file:
- it holds exactly five `create_table`s, in FK order (`agent_runs` before `advice_log`, `budget_periods` before `budget_lines` and `alerts`);
- the unique constraints carry the names from Global Constraints;
- the downgrade drops the tables in reverse order;
- nothing touches the existing tables.

Add a one-line comment saying `alerts.stream_id` arrives with TPC-33.

- [ ] **Step 7: Verify the migration on a scratch database**

Run (from `backend/`, with `S` set to this session's scratchpad directory):
```bash
DATABASE_URL=sqlite:///$S/mig.db ../venv/bin/alembic upgrade head && \
DATABASE_URL=sqlite:///$S/mig.db ../venv/bin/alembic check && \
DATABASE_URL=sqlite:///$S/mig.db ../venv/bin/alembic downgrade -1 && \
DATABASE_URL=sqlite:///$S/mig.db ../venv/bin/alembic upgrade head
```
Expected: `No new upgrade operations detected.` from `check`, and no errors. Then run `../venv/bin/alembic upgrade head` on dev.db.

- [ ] **Step 8: Run the full backend suite**

Run: `../venv/bin/python -m pytest -q`
Expected: all PASS (the `add_txn` change is backwards compatible).

- [ ] **Step 9: Write `docs/tests/test_budget_service.md`**, covering the intro (the module and TPC-18), the run command, shared setup (`period()`), the three entries, and the migration check as a note under "Not covered" (it's a manual step).

- [ ] **Step 10: Commit**

```bash
git add backend/app/db/models.py backend/alembic/versions/*_budget_tables.py backend/tests/factories.py \
        backend/tests/test_budget_service.py docs/tests/test_budget_service.md
git commit -m "feat(TPC-18): budget_periods, budget_lines, alerts, advice_log and agent_runs tables"
```

---

### Task 3: `load_txns` and `initialize_budget`

**Files:**
- Create: `backend/app/budget_service/__init__.py` (empty)
- Create: `backend/app/budget_service/initialize.py`
- Modify: `backend/tests/test_budget_service.py`
- Modify: `docs/tests/test_budget_service.md`

**Interfaces:**
- Consumes: `fit_budget`, `FitResult.window` (Task 1); `BudgetPeriod`, `BudgetLine`, and `add_txn(..., detailed=, our_category=)` (Task 2); and `Txn`, `add_months`, `month_start`, `month_end` from `app.budget_engine.txns`.
- Produces:
  - `load_txns(session, user_id: str, start: date, end: date) -> list[Txn]`;
  - `initialize_budget(session, user_id: str, today: date, fixed: frozenset[str] = FIXED_DEFAULT) -> Optional[BudgetPeriod]`;
  - a private `_latest_period(session, user_id) -> Optional[BudgetPeriod]` (most recent by `start_date`), which the race test patches.

- [ ] **Step 1: Write the failing tests** in `test_budget_service.py`. Shared setup: `TODAY = date(2026, 10, 15)` (window Jul-Sep), plus `user(s)`, which calls `add_user` + `add_account` and returns the account.

| Test | Arrange | Assert |
|---|---|---|
| `test_initialize_creates_period_and_lines` | FOOD $400/$450/$500 on the 10th of Jul/Aug/Sep; rent $1,500 on the 1st of each; TRAVEL $200 Jul, $1,200 (merchant "Delta") + $700 Aug, $250 Sep; commit | Returns a period `2026-10-01..2026-10-31`, `open`. **In a fresh `db()` session:** lines FOOD (flexible, 47250), RENT (fixed, 150000), TRAVEL (flexible, 26250), all `source="init"`, `spent_cents=0`. TRAVEL `fit_json == {"monthly_totals": [20000, 190000, 25000], "outliers": [{"month": "2026-08-01", "total_cents": 190000, "median_cents": 25000, "top_txns": [{"date": "2026-08-10", "amount_cents": 120000, "merchant": "Delta"}, {...70000...}]}]}`. Period `fit_json == {"window": ["2026-07-01", "2026-08-01", "2026-09-01"], "skipped": []}` |
| `test_skipped_recorded` | the above + SHOPPING $300 in Aug only | period `fit_json["skipped"] == ["SHOPPING"]`, no SHOPPING line |
| `test_load_txns_mapping` | 6 rows: removed; `our_category="TRAVEL"` with primary FOOD; primary `None`; another user's row; rows on `start` and `end` exactly | `load_txns(s, "u1", start, end)` returns 4 `Txn`s (no removed row, no other user). Categories `TRAVEL` and `UNCATEGORIZED` as mapped; `detailed` and `merchant` copied; both boundary dates included |
| `test_current_month_ignored` | `today = date(2026, 8, 1)`; FOOD $100 on the 10th of May/Jun/Jul; FOOD $5,000 on Aug 1 | the FOOD limit is 10500 |
| `test_second_call_returns_same_period` | 3 months history; call twice | same `id`; exactly 1 period and the same line count |
| `test_existing_older_period_returned` | add a closed `2026-09-01` period first | returns that period; no `2026-10-01` period exists |
| `test_no_history_returns_none` | only October transactions | `None`; 0 periods, 0 lines |
| `test_only_excluded_history_returns_none` | INCOME and TRANSFER_OUT rows only, Jul-Sep | `None`; 0 periods |
| `test_all_skipped_returns_none` | SHOPPING once in Aug only | `None`; 0 periods |
| `test_outlier_without_merchant` | the TRAVEL outlier txns with `merchant=None` | stored `top_txns[0]["merchant"] is None` |
| `test_race_returns_existing_period` | 3 months of FOOD history, plus an October period added for the user; `monkeypatch` `initialize._latest_period` with a wrapper that returns `None` on its first call and calls the real function afterwards | returns the existing period's `id`; 1 period and 0 lines in total |

- [ ] **Step 2: Run them and confirm they fail**

Run: `../venv/bin/python -m pytest tests/test_budget_service.py -v`
Expected: the new tests FAIL with `ModuleNotFoundError: No module named 'app.budget_service'`.

- [ ] **Step 3: Implement `initialize.py`**
  - Follow the spec's `initialize.py` section steps 1-5. The window is `add_months(month_start(today), -HISTORY_MONTHS) .. month_start(today) - timedelta(days=1)`.
  - Two small private serializers, `_period_json(fit)` and `_line_json(line)`, use `date.isoformat()`.
  - On `IntegrityError`: `session.rollback()`, then `return _latest_period(session, user_id)`.
  - Module comment: the DB side of architecture 6.3; `budget_engine` stays pure.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../venv/bin/python -m pytest tests/test_budget_service.py -v`
Expected: all PASS.

- [ ] **Step 5: Add entries for every new test to `docs/tests/test_budget_service.md`**, plus the new shared setup. Review Focus items 2-5 get explicit "why" lines.

- [ ] **Step 6: Commit**

```bash
git add backend/app/budget_service backend/tests/test_budget_service.py docs/tests/test_budget_service.md
git commit -m "feat(TPC-18): initialize_budget stores the first fitted budget with its evidence"
```

---

### Task 4: `mape` and the synthetic budget users

**Files:**
- Modify: `eval/metrics.py`
- Modify: `backend/tests/test_eval_metrics.py`, `docs/tests/test_eval_metrics.md`
- Create: `eval/budget_users.py`
- Create: `backend/tests/test_budget_users.py`, `docs/tests/test_budget_users.md`

**Interfaces:**
- Produces:
  - `eval.metrics.mape(pairs: list[tuple[int, int]]) -> Optional[float]`: pairs are `(limit_cents, actual_cents)` with actual > 0; it returns the mean `|limit − actual| / actual × 100`, rounded to 1 decimal, or `None` for no pairs.
  - `eval.budget_users`, frozen dataclasses:
    - `Weekly(category, merchant, cents, jitter=0.1, weekday=5, growth=0.0)`: one charge per matching weekday, times `(1 + growth) ** months_since_start`, times `uniform(1 − jitter, 1 + jitter)`;
    - `Monthly(category, merchant, cents, day=1, moved: dict[date, date] = {})`: `moved` maps a month's first day to the date it was actually paid;
    - `Once(category, merchant, cents, day: date)`;
    - `Profile(id, description, seed, patterns, today=date(2026, 8, 1), start=date(2026, 5, 1), end=date(2026, 8, 31))`.
  - Functions and constants:
    - `transactions(profile) -> list[tuple[date, int, str, str]]`: `(day, cents, category, merchant)`, sorted, deterministic per seed;
    - `PROFILES: tuple[Profile, ...]`;
    - `seed_profile(session, profile) -> str`: writes `budget-<id>` with one checking account and commits.
  - `moved` replaces the spec's `overrides`: amount changes are written as `Once`s instead.

- [ ] **Step 1: Write the failing tests**

`test_eval_metrics.py`:
```python
def test_mape_known_case():
    assert mape([(110, 100), (90, 100), (200, 100)]) == 40.0
def test_mape_empty():
    assert mape([]) is None
def test_mape_rounds():
    assert mape([(1, 3)]) == 66.7
```

`test_budget_users.py`:
```python
def test_same_seed_same_transactions():
    p = profile("steady")
    assert transactions(p) == transactions(p)
def test_vacation_june_travel():
    june = [c for d, c, cat, _ in transactions(profile("vacation")) if cat == "TRAVEL" and d.month == 6]
    assert sum(june) == 190000
def test_early_rent_doubles_june():
    rent = [(d, c) for d, c, cat, _ in transactions(profile("early-rent")) if cat == "RENT_AND_UTILITIES"]
    assert sum(c for d, c in rent if d.month == 6) == 300000
    assert not [d for d, _ in rent if d.month == 7]
def test_every_profile_has_income_and_transfer():
    for p in PROFILES:
        cats = {cat for _, _, cat, _ in transactions(p)}
        assert {"INCOME", "TRANSFER_OUT"} <= cats
def test_profile_ids():
    assert [p.id for p in PROFILES] == ["steady", "vacation", "early-rent", "rising", "occasional"]
def test_seed_profile_writes_rows(db):
    with db() as s:
        uid = seed_profile(s, profile("steady"))
        assert s.query(Transaction).filter_by(user_id=uid).count() == len(transactions(profile("steady")))
```
(`profile(id)` is a test helper that looks the profile up in `PROFILES`.)

- [ ] **Step 2: Run them and confirm they fail**

Run: `../venv/bin/python -m pytest tests/test_eval_metrics.py tests/test_budget_users.py -v`
Expected: FAIL with `ImportError` (`mape`, `eval.budget_users`).

- [ ] **Step 3: Implement `mape` in `eval/metrics.py`.**

- [ ] **Step 4: Implement `eval/budget_users.py`.** Base patterns shared by every profile:
  - `Monthly("RENT_AND_UTILITIES", "Landlord", 150000)`;
  - `Monthly("INCOME", "Employer", -420000, day=15)`;
  - `Monthly("TRANSFER_OUT", "Savings", 50000, day=16)`;
  - `Weekly("FOOD_AND_DRINK", "Grocer", 11000, weekday=5)`;
  - `Weekly("FOOD_AND_DRINK", "Diner", 2500, weekday=2)` and `Weekly("FOOD_AND_DRINK", "Diner", 2500, weekday=4)`;
  - `Weekly("TRANSPORTATION", "Fuel", 4000, weekday=0)`;
  - `Monthly("ENTERTAINMENT", "Streamly", 1599, day=5)`.

  Per profile:
  - `vacation` adds `Once`s of Travel: $200 (May 12); $1,200 + $500 + $200 (June 10-14, merchants "Airline", "Hotel", "Car hire"); $250 (Jul 12); $220 (Aug 12);
  - `early-rent` sets `moved={date(2026, 7, 1): date(2026, 6, 30)}` on rent;
  - `rising` uses `growth=0.10` on the grocer;
  - `occasional` adds `Once("GENERAL_MERCHANDISE", "Shop", 30000, date(2026, 6, 14))`.

  Amounts are rounded to whole cents with `round_cents(Decimal(str(x)))`. `seed_profile` uses `add_user` / `add_account` / `add_txn` from `tests.factories`, as `eval/fixtures.py` does.

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `../venv/bin/python -m pytest tests/test_eval_metrics.py tests/test_budget_users.py -v`
Expected: all PASS.

- [ ] **Step 6: Docs.** Add `mape` entries to `docs/tests/test_eval_metrics.md`. Write a new `docs/tests/test_budget_users.md` with the intro, run command, shared setup (`profile()`) and one entry per test.

- [ ] **Step 7: Commit**

```bash
git add eval/metrics.py eval/budget_users.py backend/tests/test_eval_metrics.py backend/tests/test_budget_users.py \
        docs/tests/test_eval_metrics.md docs/tests/test_budget_users.md
git commit -m "feat(TPC-18): mape metric and five synthetic budget users"
```

---

### Task 5: `eval/budget_accuracy.py`

**Files:**
- Create: `eval/budget_accuracy.py`
- Create: `backend/tests/test_budget_accuracy.py`, `docs/tests/test_budget_accuracy.md`

**Interfaces:**
- Consumes: `mape` and `PROFILES` / `seed_profile` / `Profile` (Task 4); `load_txns` and `initialize_budget` (Task 3); `fit_budget(center=)` (Task 1); `temp_db` from `eval.fixtures`; `results_dir`, `_git_commit` and `_write` from `eval.run_eval`. The script needs its own path setup first, copied from the top of `run_eval.py` (`REPO/backend` and `REPO` on `sys.path`, then `FERNET_KEY` setdefault): launched as `eval/budget_accuracy.py`, `sys.path[0]` is `eval/`, so `import eval.run_eval` fails without it. and `spending`, `category_totals`, `month_end`, `add_months` and `month_start` from `app.budget_engine.txns`.
- Produces:
  - `score(lines, actuals: dict[str, int]) -> dict`. Each item in `lines` has `.category`, `.kind` and `.limit_cents`. The result is `{"mape": float|None, "lines": [{"category", "kind", "limit_cents", "actual_cents"}], "zero_actual_lines": [categories], "unbudgeted": [categories]}` (both lists sorted).
  - `run_profile(profile) -> dict`: `{"id", "description", "budget": <the printable stored budget as a dict>, "median": score, "mean": score}`.
  - `run(profiles, out_root: Path, day: date) -> Path`: runs every profile, prints the tables, and writes `results.json` (`{"git_commit", "profiles": [...], "pooled": {"median": mape, "mean": mape}}`) to `results_dir(out_root, day, "budget_accuracy")`. It returns the JSON path.
  - `main(argv=None) -> int`, which calls `run(PROFILES, RESULTS_DIR, date.today())`.

- [ ] **Step 1: Write the failing tests**

```python
def test_score_counts():
    lines = [SimpleNamespace(category="FOOD_AND_DRINK", kind="flexible", limit_cents=110),
             SimpleNamespace(category="SHOPPING", kind="flexible", limit_cents=50)]
    s = score(lines, {"FOOD_AND_DRINK": 100, "TRAVEL": 30})
    assert s["mape"] == 10.0
    assert s["zero_actual_lines"] == ["SHOPPING"]
    assert s["unbudgeted"] == ["TRAVEL"]

def test_run_writes_results(tmp_path, capsys):
    path = run([profile("steady"), profile("vacation")], tmp_path, date(2026, 10, 28))
    assert path == tmp_path / "2026-10-28" / "budget_accuracy" / "results.json"
    data = json.loads(path.read_text())
    vac = next(p for p in data["profiles"] if p["id"] == "vacation")
    assert vac["median"]["mape"] < vac["mean"]["mape"]
    assert set(data["pooled"]) == {"median", "mean"}
    assert "vacation" in capsys.readouterr().out

def test_early_rent_fixed_line_is_one_rent():
    rent = next(l for l in run_profile(profile("early-rent"))["median"]["lines"]
                if l["category"] == "RENT_AND_UTILITIES")
    assert rent["limit_cents"] == 150000 and rent["actual_cents"] == 150000

def test_occasional_mean_has_zero_actual_line():
    r = run_profile(profile("occasional"))
    assert "GENERAL_MERCHANDISE" in r["mean"]["zero_actual_lines"]
    assert "GENERAL_MERCHANDISE" not in [l["category"] for l in r["median"]["lines"]]
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `../venv/bin/python -m pytest tests/test_budget_accuracy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'eval.budget_accuracy'`.

- [ ] **Step 3: Implement `eval/budget_accuracy.py`**
  - A module docstring in `run_eval.py`'s style, saying what it does and how to run it.
  - The actuals month is `profile.today .. month_end(profile.today)`.
  - The fit uses `load_txns(s, uid, add_months(month_start(today), -3), month_start(today) - 1 day)`.
  - The printed table has a row per profile with median MAPE, mean MAPE, zero-actual lines and unbudgeted, then a pooled row. Before the table, each profile's stored budget is printed as `category  kind  $limit` with its outlier months and skipped categories.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../venv/bin/python -m pytest tests/test_budget_accuracy.py -v`
Expected: all PASS.

- [ ] **Step 5: Write `docs/tests/test_budget_accuracy.md`** with the intro, run command, shared setup and entries.

- [ ] **Step 6: Run the whole backend suite**

Run: `../venv/bin/python -m pytest -q`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add eval/budget_accuracy.py backend/tests/test_budget_accuracy.py docs/tests/test_budget_accuracy.md
git commit -m "feat(TPC-18): budget accuracy eval, median vs mean on synthetic users"
```

---

### Task 6: Run it, record the results, update the docs

**Files:**
- Create: `eval/results/<today>/budget_accuracy/results.json` (generated)
- Modify: `docs/architecture.md`, `eval/README.md`

- [ ] **Step 1: Run the script**

Run (repo root): `venv/bin/python eval/budget_accuracy.py`
Expected: each profile's first budget is printed with plausible limits:
- steady FOOD ≈ median of its months × 1.05;
- rent $1,500 in every profile, including early-rent;
- vacation's Travel line around $260 with June as an outlier;
- `occasional` lists GENERAL_MERCHANDISE as skipped.

Then the MAPE table follows, and `results.json` is written. If a budget looks wrong, stop and debug before recording anything.

- [ ] **Step 2: Update `docs/architecture.md`**
  - **5:** the `fit_json` columns, the two unique constraints, and a note that `stream_id` arrives with TPC-33.
  - **6.3:**
    - step 2 becomes the median from the first non-zero month, with the double-month reason;
    - add a paragraph on `initialize_budget`: it runs only when the user has no period, returns `None` with no history, and stores evidence in `fit_json`;
    - replace the "learning exercise" sentence with the measured result and a pointer to `eval/budget_accuracy.py`.
  - **10:** an "As built (TPC-18)" note for initial budget accuracy, with how to run it.
  - **13:** add `budget_service/` under `app/`.
  - **16, four rows dated 2026-10-28:**
    - the fixed median rule (superseding the 2026-10-27 row; the double-month example);
    - fit evidence stored as JSON;
    - string alert columns vs Enum for stable sets;
    - the measured median vs mean pooled MAPE and per-profile notes, with the decision to keep the median.

- [ ] **Step 3: Update `eval/README.md`**: a "Budget accuracy" section covering what it measures, the run command, where results go, and how to add a profile (a `Profile` in `PROFILES` plus its planted-fact test).

- [ ] **Step 4: Commit**

```bash
git add eval/results docs/architecture.md eval/README.md
git commit -m "docs(TPC-18): architecture, decision log and first median vs mean budget accuracy"
```
