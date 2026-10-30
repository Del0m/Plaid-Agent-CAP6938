# TPC-17 Budget Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The deterministic budget maths (architecture 6.3-6.5) as pure functions: fit an initial budget from history, flag today's overspend, and compute next month's limits.

**Architecture:**
- `backend/app/budget_engine/txns.py` holds the `Txn` dataclass, the spending filter, per-category net totals, month helpers and the one rounding rule.
- `fit.py`, `pace.py` and `close.py` each build on `txns.py` and nothing else in the app: no DB, no clock, no config.
- `close.py` imports `BudgetLine` from `pace.py`.

**Tech Stack:** Python 3.12 stdlib only (`dataclasses`, `datetime`, `decimal`, `statistics`, `calendar`), pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-tpc-17-budget-engine-design.md`

## Global Constraints

- **Running things:** tests run from `backend/` with `../venv/bin/python -m pytest`. No new pip dependencies.
- **Purity:** no imports from `app.db`, `app.config` or anything that reads a clock. Every function takes `today` and the transactions as arguments. Every dataclass is `@dataclass(frozen=True)`.
- **Money:** integer cents everywhere. Plaid sign: positive = money out, negative = money in / refund.
- **Rounding:** every cents result that comes from a multiplication goes through `round_cents` (round half **up**, via `Decimal`). Build Decimals with `Decimal(str(x))`, never `Decimal(float)`. Python's `round()` is banker's rounding and is not used for money.
- **Constants (verbatim from the spec):**
  - `txns.py`: `EXCLUDED_PRIMARY = frozenset({"INCOME", "TRANSFER_IN", "TRANSFER_OUT"})`, `EXCLUDED_DETAILED = frozenset({"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"})`
  - `fit.py`: `FIXED_DEFAULT = frozenset({"RENT_AND_UTILITIES", "LOAN_PAYMENTS"})`, `FLEX_MARKUP = 1.05`, `OUTLIER_RATIO = 1.5`, `HISTORY_MONTHS = 3`
  - `pace.py`: `PACE_FLAG = 1.10`, `MIN_DAYS = 5`
  - `close.py`: `SMOOTH_OLD, SMOOTH_ACTUAL = 0.7, 0.3`, `CLAMP = 0.15`
- **Iterables:** any function that walks `txns` more than once does `txns = list(txns)` first.
- **Test docs:** every test function gets an entry in `docs/tests/<test module>.md` in the same commit (architecture section 12), in the existing format: intro, run command, `## \`test_name\`` with **What it does** / **Why it's there**.
- **Comments:** short, lowercase, one-line "why" notes, as in existing files.
- **Commits:** no Claude attribution. Prefixes `feat(TPC-17):`, `docs(TPC-17):`.

## Review Focus

1. **A half-cent result** (e.g. smoothing old 1015¢ toward 1000¢ = 1010.5¢). A person expects ordinary half-up rounding (1011¢), not Python's banker's rounding (1010¢). Pinned by `test_round_cents_half_up` (Task 1) and `test_close_rounds_half_up` (Task 4).
2. **`txns` passed as a generator.** `fit_budget` reads the transactions several times (earliest date, then per-month totals). A one-shot iterator must not silently produce an empty budget. Pinned by `test_fit_accepts_generator` (Task 2).
3. **`today` in January.** The history window crosses the year (Oct, Nov, Dec of last year). Pinned by `test_fit_window_crosses_year` (Task 2).
4. **Transactions dated after `today`** (a future-dated pending charge, or a caller passing the whole ledger). They are not spent yet and must not count toward pace. Pinned by `test_evaluate_ignores_other_periods` (Task 3).
5. **A category with only refunds.** Its net total is 0, so it never produces a fitted line, a skipped entry, or an unbudgeted pace row. Pinned by `test_fit_refund_only_category_ignored` (Task 2) and `test_evaluate_refund_only_not_unbudgeted` (Task 3).

---

### Task 1: Transactions, spending filter and helpers (`txns.py`)

**Files:**
- Create: `backend/app/budget_engine/__init__.py` (empty), `backend/app/budget_engine/txns.py`, `backend/tests/test_budget_txns.py`, `docs/tests/test_budget_txns.md`

**Interfaces:**
- Produces:
  - `Txn(date: date, amount_cents: int, category: str, detailed: Optional[str] = None, merchant: Optional[str] = None)`, frozen.
  - `EXCLUDED_PRIMARY`, `EXCLUDED_DETAILED` (see Global Constraints).
  - `spending(txns: Iterable[Txn]) -> list[Txn]`
  - `category_totals(txns: Iterable[Txn], start: date, end: date) -> dict[str, int]`: sums the given txns dated `start..end` inclusive per category, floors each total at 0, and omits categories with no transactions in range. It does **not** call `spending()`; callers pass `spending(txns)`.
  - `month_start(d: date) -> date`, `month_end(d: date) -> date`, `add_months(d: date, n: int) -> date` (`d` must be the 1st, else `ValueError`; `n` may be negative).
  - `round_cents(value: Decimal) -> int`: `ROUND_HALF_UP` to a whole number.

- [ ] **Step 1: Write the failing tests** in `test_budget_txns.py`, with a `t(day, cents, cat="FOOD_AND_DRINK", detailed=None, month=10, year=2026)` helper.
  - `test_spending_drops_non_spending`: from INCOME, TRANSFER_IN, TRANSFER_OUT, a `LOAN_PAYMENTS` txn with detailed `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`, a `LOAN_PAYMENTS` txn with detailed `LOAN_PAYMENTS_CAR_PAYMENT`, and a FOOD_AND_DRINK txn, only the car payment and the food txn remain, in input order.
  - `test_totals_net_refunds`: FOOD 5000 + 3000 − 2000 → `{"FOOD_AND_DRINK": 6000}`.
  - `test_totals_floor_at_zero`: GENERAL_MERCHANDISE 1000 − 4000 → `{"GENERAL_MERCHANDISE": 0}` (present, at 0).
  - `test_totals_range_inclusive`: txns on Sep 30, Oct 1, Oct 31 and Nov 1 (100 each, same category); the range Oct 1..Oct 31 → 200.
  - `test_totals_omit_absent_categories`: a range with no txns → `{}`.
  - `test_month_helpers_leap_year`: `month_end(date(2028, 2, 10)) == date(2028, 2, 29)`, `month_end(date(2027, 2, 1)) == date(2027, 2, 28)`, `month_start(date(2028, 2, 29)) == date(2028, 2, 1)`.
  - `test_add_months_crosses_year`: `add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)`, `add_months(date(2027, 1, 1), -1) == date(2026, 12, 1)`, `add_months(date(2026, 10, 1), -3) == date(2026, 7, 1)`; `add_months(date(2026, 10, 15), 1)` raises `ValueError`.
  - `test_round_cents_half_up`: `round_cents(Decimal("1010.5")) == 1011`, `round_cents(Decimal("1010.49")) == 1010`, `round_cents(Decimal("47250.00")) == 47250`.

- [ ] **Step 2: Run** `../venv/bin/python -m pytest tests/test_budget_txns.py -v`. Expected: collection error, `ModuleNotFoundError: app.budget_engine`.

- [ ] **Step 3: Implement `txns.py`** to the Interfaces above. `month_end` uses `calendar.monthrange`. `add_months` uses divmod over `year * 12 + month - 1`.

- [ ] **Step 4: Run** the same command. Expected: 8 passed.

- [ ] **Step 5: Write `docs/tests/test_budget_txns.md`**, one entry per test.

- [ ] **Step 6: Commit**

```bash
git add backend/app/budget_engine backend/tests/test_budget_txns.py docs/tests/test_budget_txns.md
git commit -m "feat(TPC-17): budget engine transactions, spending filter and month helpers"
```

---

### Task 2: Initial budget fit (`fit.py`)

**Files:**
- Create: `backend/app/budget_engine/fit.py`, `backend/tests/test_budget_fit.py`, `docs/tests/test_budget_fit.md`

**Interfaces:**
- Consumes: everything in Task 1.
- Produces:
  - `OutlierMonth(month: date, total_cents: int, median_cents: int, top_txns: tuple[Txn, ...])`
  - `FittedLine(category: str, kind: Literal["fixed", "flexible"], limit_cents: int, monthly_totals: tuple[int, ...], outliers: tuple[OutlierMonth, ...])`
  - `FitResult(lines: tuple[FittedLine, ...], skipped: tuple[str, ...])`
  - `fit_budget(txns: Iterable[Txn], today: date, fixed: frozenset[str] = FIXED_DEFAULT) -> FitResult`

**Algorithm** (the spec's steps 1-4, with the choices it leaves open):
- Window: `first = add_months(month_start(today), -HISTORY_MONTHS)`, raised to `month_start(min txn date)` if that is later. The months are `first` up to, but not including, `month_start(today)`. If there are no txns or no months, return `FitResult((), ())`. The earliest date is taken over **all** txns passed, not only spending.
- `monthly_totals` holds one entry per window month, oldest first, with 0 where a category is absent.
- `median_cents` in `OutlierMonth` = `round_cents(Decimal(str(median)))`. `limit` = `round_cents(Decimal(str(median)) * Decimal(str(FLEX_MARKUP)))`.
- Outlier test: `total > median * OUTLIER_RATIO` (strict), compared in Decimal.
- `top_txns`: the outlier month's spending txns in that category with `amount_cents > 0`, sorted by `(-amount_cents, date)`, first 3.

- [ ] **Step 1: Write the failing tests** in `test_budget_fit.py`. `TODAY = date(2026, 10, 15)`, so the window is Jul, Aug, Sep 2026. Use a `t(y, m, d, cents, cat, detailed=None, merchant=None)` helper and a `line(result, cat)` lookup.
  - `test_normal_flexible_median`: FOOD_AND_DRINK at 40000 / 45000 / 50000 (Jul/Aug/Sep) → one flexible line, `limit_cents == 47250`, `monthly_totals == (40000, 45000, 50000)`, no outliers.
  - `test_outlier_month_recorded`: TRAVEL Jul 20000, Aug 190000 (as four txns: 120000, 40000, 20000, 10000), Sep 25000 → `limit_cents == 26250`. There is exactly one outlier, with `month == date(2026, 8, 1)`, `total_cents == 190000`, `median_cents == 25000`, and `top_txns` amounts `(120000, 40000, 20000)`.
  - `test_fixed_uses_latest_month_no_markup`: RENT_AND_UTILITIES at 150000 each month → `kind == "fixed"`, `limit_cents == 150000`, `outliers == ()`.
  - `test_fixed_paid_early_uses_previous_month`: rent 150000 in Jul, 150000 in Aug (paid Aug 31 for September), none in Sep → `limit_cents == 150000`, `monthly_totals == (150000, 150000, 0)`.
  - `test_custom_fixed_set`: `fixed=frozenset({"ENTERTAINMENT"})` with ENTERTAINMENT 1000 / 2000 / 1500 → fixed, limit 1500.
  - `test_refunds_netted`: FOOD Sep 50000 and a −10000 refund in Sep → that month's total is 40000.
  - `test_refund_only_category_ignored`: GENERAL_MERCHANDISE with only −5000 txns → not in `lines`, not in `skipped`.
  - `test_exclusions_never_lines`: INCOME, TRANSFER_IN, TRANSFER_OUT and a credit-card payment in every month → `lines == ()`, `skipped == ()`.
  - `test_current_month_ignored`: FOOD 100000 on Oct 3 plus the normal history → limit still 47250.
  - `test_short_history_two_months`: FOOD only in Aug 40000 and Sep 50000 → `monthly_totals == (40000, 50000)`, `limit_cents == 47250`.
  - `test_no_complete_month_empty`: txns only in Oct 2026 → `FitResult((), ())`. `fit_budget([], TODAY)` gives the same.
  - `test_occasional_category_skipped`: HOME_IMPROVEMENT only in Aug (30000) → `skipped == ("HOME_IMPROVEMENT",)`, and no line for it.
  - `test_lines_and_skipped_sorted`: TRAVEL, FOOD_AND_DRINK and ENTERTAINMENT lines, plus PERSONAL_CARE and GENERAL_SERVICES skipped → category order is alphabetical in both tuples.
  - `test_fit_accepts_generator`: `fit_budget((x for x in txns), TODAY)` equals `fit_budget(txns, TODAY)` for the normal-case txns.
  - `test_fit_window_crosses_year`: `today = date(2027, 1, 10)`, FOOD in Oct/Nov/Dec 2026 at 40000 / 45000 / 50000 → limit 47250, `monthly_totals == (40000, 45000, 50000)`.

- [ ] **Step 2: Run** `../venv/bin/python -m pytest tests/test_budget_fit.py -v`. Expected: `ImportError` on `app.budget_engine.fit`.

- [ ] **Step 3: Implement `fit.py`** to the Interfaces and Algorithm above.

- [ ] **Step 4: Run** the same command. Expected: 15 passed.

- [ ] **Step 5: Write `docs/tests/test_budget_fit.md`.**

- [ ] **Step 6: Commit** `feat(TPC-17): fit an initial budget from three months of history`.

---

### Task 3: Overspend pace (`pace.py`)

**Files:**
- Create: `backend/app/budget_engine/pace.py`, `backend/tests/test_budget_pace.py`, `docs/tests/test_budget_pace.md`

**Interfaces:**
- Consumes: `Txn`, `spending`, `category_totals`, `month_start`, `month_end` (Task 1).
- Produces:
  - `BudgetLine(category: str, kind: Literal["fixed", "flexible"], limit_cents: int)`
  - `PaceResult(category, kind: Literal["fixed", "flexible", "unbudgeted"], limit_cents, spent_cents, days_elapsed, days_in_period, pace: Optional[float], flagged: bool, reason: Optional[Literal["pace", "over_limit"]])`
  - `evaluate(lines: Sequence[BudgetLine], txns: Iterable[Txn], today: date) -> list[PaceResult]`: one result per line in the given order (with `spent_cents=0` when nothing was spent), then one per unbudgeted category with a total above 0, sorted by category.

**Pace formula:** compute `pace = spent * days_in_period / (limit * days_elapsed)`. It is algebraically the spec's `spent / (limit × elapsed_fraction)`, but it divides once, so a pace of exactly 1.10 equals `PACE_FLAG` in floating point and isn't 1.0999999.

- [ ] **Step 1: Write the failing tests** in `test_budget_pace.py`, with helpers `flex(cat, cents)`, `fixed(cat, cents)`, `t(y, m, d, cents, cat)` and `by_cat(results)`.
  - `test_mid_month_pace_flags`: FOOD limit 30000, 20000 spent by Oct 15 2026 → `days_elapsed == 15`, `days_in_period == 31`, `pace == pytest.approx(1.3778, abs=1e-4)`, `flagged`, `reason == "pace"`.
  - `test_under_pace_not_flagged`: same limit, 10000 spent by Oct 15 → `flagged is False`, `reason is None`.
  - `test_early_month_never_flags_on_pace`: for days 1-4 of Oct 2026, limit 30000 and 25000 spent on Oct 1 → never flagged, and pace is still reported (not None).
  - `test_early_month_over_limit_flags`: limit 30000, 35000 spent by Oct 2 → `reason == "over_limit"`.
  - `test_pace_threshold_inclusive`: Nov 5 2026 (30-day month), limit 60000, spent 11000 → `pace == 1.10` exactly, flagged `"pace"`. Spent 10999 → not flagged.
  - `test_fixed_rent_on_first_not_flagged`: RENT limit 150000, 150000 spent on Oct 1, evaluated Oct 10 → `pace is None`, not flagged.
  - `test_fixed_over_limit`: RENT limit 150000, 155000 spent → `"over_limit"`, pace None.
  - `test_over_limit_beats_pace`: Oct 15, limit 30000, 31000 spent → `reason == "over_limit"`.
  - `test_unbudgeted_reported_unflagged`: lines for FOOD only, plus TRAVEL 5000 and ENTERTAINMENT 2000 spent → results are `[FOOD, ENTERTAINMENT, TRAVEL]`. The two unbudgeted rows have `kind == "unbudgeted"`, `limit_cents == 0`, `pace is None` and `flagged is False`.
  - `test_refund_only_not_unbudgeted`: a −3000 GENERAL_MERCHANDISE refund with no line → no result for it.
  - `test_month_lengths`: `today = date(2028, 2, 29)` → `days_in_period == 29`, `days_elapsed == 29`. `date(2027, 1, 31)` → 31.
  - `test_zero_limit`: a flexible line with limit 0. With 0 spent: pace None, not flagged. With 100 spent: pace None, `"over_limit"`.
  - `test_evaluate_ignores_other_periods`: Oct 15, with FOOD txns on Sep 30 (5000), Oct 1 (1000), Oct 15 (1000) and Oct 16 (9000) → `spent_cents == 2000`.
  - `test_lines_keep_given_order_and_excluded_skipped`: lines `[TRAVEL, FOOD]` come out in that order, and an INCOME txn produces no row.

- [ ] **Step 2: Run** `../venv/bin/python -m pytest tests/test_budget_pace.py -v`. Expected: `ImportError`.

- [ ] **Step 3: Implement `pace.py`.** Spending is `category_totals(spending(txns), month_start(today), today)`. Fixed and unbudgeted rows always have `pace = None`.

- [ ] **Step 4: Run** the same command. Expected: 14 passed.

- [ ] **Step 5: Write `docs/tests/test_budget_pace.md`.**

- [ ] **Step 6: Commit** `feat(TPC-17): overspend pace and over-limit flags`.

---

### Task 4: Period close (`close.py`)

**Files:**
- Create: `backend/app/budget_engine/close.py`, `backend/tests/test_budget_close.py`, `docs/tests/test_budget_close.md`

**Interfaces:**
- Consumes: `BudgetLine` (Task 3), `round_cents` (Task 1).
- Produces:
  - `Adjustment(category: str, old_cents: int, actual_cents: int, new_cents: int, source: Literal["accepted", "smoothed", "fixed"])`
  - `close_period(lines: Sequence[BudgetLine], actuals: dict[str, int], accepted: dict[str, int]) -> list[Adjustment]`: one per line, in the given order.

**Rounding:** smoothing rounds once, on `Decimal(str(SMOOTH_OLD)) * old + Decimal(str(SMOOTH_ACTUAL)) * actual`. The clamp bounds are `round_cents(old × (1 ∓ CLAMP))`, also in Decimal.

- [ ] **Step 1: Write the failing tests** in `test_budget_close.py`, with `flex(cat, cents)`, `fixed(cat, cents)` and a `one(lines, actuals, accepted={})` helper that returns the single `Adjustment`.
  - `test_smoothing_within_clamp`: old 40000 → actual 50000 gives 43000; old 40000 → actual 38000 gives 39400. Both have `source == "smoothed"`.
  - `test_clamp_up`: old 40000, actual 100000 → 46000.
  - `test_clamp_down`: old 40000, actual 0 → 34000.
  - `test_accepted_not_clamped`: old 40000, actual 50000, `accepted={"FOOD_AND_DRINK": 60000}` → 60000, `source == "accepted"`. Accepted also overrides a fixed line.
  - `test_fixed_follows_actual`: old 150000, actual 155000 → 155000, `source == "fixed"`.
  - `test_fixed_keeps_old_when_zero`: old 150000, actual 0 (or missing) → 150000.
  - `test_missing_actual_is_zero`: a flexible old 40000 with `actuals={}` → `actual_cents == 0`, `new_cents == 34000`.
  - `test_unlined_actuals_ignored`: `actuals` has TRAVEL with no line → the output has one entry per line only.
  - `test_close_rounds_half_up`: old 1015, actual 1000 → 1011 (1010.5 rounded half up).
  - `test_order_preserved`: lines `[TRAVEL, FOOD, RENT]` come out in that order.

- [ ] **Step 2: Run** `../venv/bin/python -m pytest tests/test_budget_close.py -v`. Expected: `ImportError`.

- [ ] **Step 3: Implement `close.py`.**

- [ ] **Step 4: Run** the same command, then the whole suite with `../venv/bin/python -m pytest -q`. Expected: 10 passed, then 237 + 47 = 284 passed.

- [ ] **Step 5: Write `docs/tests/test_budget_close.md`.**

- [ ] **Step 6: Commit** `feat(TPC-17): period close with smoothing, clamp and accepted adjustments`.

---

### Task 5: Architecture and decision log

**Files:**
- Modify: `docs/architecture.md`: sections 6.3, 6.4, 6.5, 7 and 16.

- [ ] **Step 1: Edit 6.3.**
  - Name `budget_engine/fit.py`.
  - The history is 3 complete calendar months; the current month is never fitted.
  - Refunds are netted within a category, floored at $0.
  - Fixed = the latest non-zero month, with no markup.
  - A flexible category with a median of $0 is skipped.
  - Outliers are months above 1.5× the median, kept with their top 3 transactions for explanation, and don't change the median.
- [ ] **Step 2: Edit 6.4.** Name `pace.py`. Spending with no budget line is reported as `unbudgeted` (unflagged) for the graph to handle.
- [ ] **Step 3: Edit 6.5.**
  - Name `close.py`.
  - A fixed line follows the actual amount (keeping the old limit when the actual is $0).
  - Accepted adjustments are used as-is, with no clamp.
  - Amounts round half up to the cent.
- [ ] **Step 4: Section 7.** Add the follow-up: `get_spending` still excludes refunds, so chat totals can differ from budget pace until it nets them too.
- [ ] **Step 5: Decision log.** Add three rows dated 2026-10-27 (netted refunds; outlier months; fixed-line rules), with the "Why" taken from the spec's Decisions table. Insert them before the trailing empty `| | | |` row.
- [ ] **Step 6: Commit** `docs(TPC-17): architecture 6.3-6.5 and decision log for the budget engine`.
