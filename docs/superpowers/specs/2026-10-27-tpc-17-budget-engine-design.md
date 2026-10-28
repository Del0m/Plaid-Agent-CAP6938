# TPC-17: Budget engine pure functions

Jira: TPC-17 (epic TPC-4, M3). Branch: `TPC-17`, cut from `TPC-16` (rebase onto `main` once PRs #5-#10 merge).

## Goal

The deterministic maths behind the budget agent (architecture sections 6.3-6.5). It is the core design rule: the LLM never does arithmetic that matters. Three modules, plus a shared spending filter:
- **Fit:** the initial budget from history.
- **Pace:** today's overspend flags.
- **Close:** next month's limits.

TPC-18 (`initialize_budget`), TPC-19 (`detect_overspend`) and TPC-22 (period close) call them.

**Done when:** pytest covers the normal, early-month, fixed-category and outlier cases (plus the edge cases below).

**Out of scope:**
- database tables and reading or writing them (TPC-18);
- graph nodes (TPC-19);
- recurring-stream detection (TPC-33), which will later feed the `fixed` set;
- deciding what to do about unbudgeted spending (TPC-19).

## Decisions

| Decision | Why |
|---|---|
| Pure functions over frozen dataclasses: integer cents, `date`s, strings. No DB, clock or config; the caller passes `today` and the transactions | Testable without fixtures, and the doc's "plain Python functions" rule. Callers already hold the data |
| Refunds are **netted** within a category (purchases − refunds, floored at $0) | A returned purchase isn't spending, and counting it would raise false overspend alerts (the false-alarm metric). The privacy gateway's `get_spending` still excludes refunds, so chat totals can differ from budget pace until it's aligned (follow-up) |
| Outliers are **months**: a month above 1.5× the category median is recorded with its biggest transactions. Explanation only, so the median is unchanged | Keeps the doc's maths exactly; the agent can still say "Travel was $1,900 in August and was left out" |
| History is the last **3 complete calendar months**; the current partial month is never fitted | A partial month would drag every median down |
| A fixed limit is the **most recent non-zero** month, with no ×1.05; at close a fixed line follows the actual amount | Rent paid a day early lands in the previous month; a rent increase should be adopted, not smoothed toward |
| A flexible category whose median is $0 gets **no line** and is listed in `skipped` | A once-a-quarter purchase would otherwise get a $0 limit that flags every time |

## Files

```
backend/app/budget_engine/
├── __init__.py
├── txns.py     Txn, what counts as spending, category totals, month helpers
├── fit.py      fit_budget
├── pace.py     evaluate
└── close.py    close_period
```

## `txns.py`

```python
@dataclass(frozen=True)
class Txn:
    date: date
    amount_cents: int            # Plaid sign: positive = money out, negative = money in / refund
    category: str                # pf_category_primary; the caller maps None to "UNCATEGORIZED"
    detailed: Optional[str] = None
    merchant: Optional[str] = None

EXCLUDED_PRIMARY = frozenset({"INCOME", "TRANSFER_IN", "TRANSFER_OUT"})
EXCLUDED_DETAILED = frozenset({"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"})
```

- `spending(txns) -> list[Txn]`: drops transactions whose `category` is in `EXCLUDED_PRIMARY` or whose `detailed` is in `EXCLUDED_DETAILED`. These would double count: income isn't spending, a transfer moves money between your own accounts, and a card payment pays for purchases already counted.
- `category_totals(txns, start, end) -> dict[str, int]`: spending dated `start..end` inclusive, summed per category net of refunds. A total below 0 becomes 0. Categories with no transactions are absent.
- `month_start(d) -> date`, `month_end(d) -> date`, `add_months(d, n) -> date` (on the 1st of a month): calendar-month helpers, leap years included.

The caller (TPC-18) excludes `removed` transactions before building `Txn`s. Pending transactions are included: they're real spending, and the soft delete already prevents a pending/posted double count.

## `fit.py`

```python
FIXED_DEFAULT = frozenset({"RENT_AND_UTILITIES", "LOAN_PAYMENTS"})
FLEX_MARKUP = 1.05
OUTLIER_RATIO = 1.5
HISTORY_MONTHS = 3

@dataclass(frozen=True)
class OutlierMonth:
    month: date                  # first of the month
    total_cents: int
    median_cents: int
    top_txns: tuple[Txn, ...]    # up to 3, largest amount first

@dataclass(frozen=True)
class FittedLine:
    category: str
    kind: Literal["fixed", "flexible"]
    limit_cents: int
    monthly_totals: tuple[int, ...]   # oldest month first
    outliers: tuple[OutlierMonth, ...]

@dataclass(frozen=True)
class FitResult:
    lines: tuple[FittedLine, ...]     # sorted by category
    skipped: tuple[str, ...]          # flexible categories whose median is 0, sorted

def fit_budget(txns, today: date, fixed: frozenset[str] = FIXED_DEFAULT) -> FitResult
```

1. **Window.** The window is the 3 complete months before `today`'s month. It starts no earlier than the month of the earliest transaction, so short history shrinks the window. With 0 complete months, return an empty `FitResult`.
2. **Totals.** Per window month, `category_totals(spending(txns), ...)`. Only categories with a total above 0 in at least one window month are considered. A month with no spending in a category counts as 0.
3. **Fixed categories** (in `fixed`): `limit` = the most recent window month with a total above 0. No outliers.
4. **Flexible categories:**
   - `median` = `statistics.median` of the window totals, zeros included.
   - If `median == 0`, the category goes in `skipped` with no line.
   - Otherwise `limit = round(median × 1.05)`.
   - Outliers: each window month whose total is above `1.5 × median`, with its 3 largest spending transactions in that category.

## `pace.py`

```python
PACE_FLAG = 1.10
MIN_DAYS = 5

@dataclass(frozen=True)
class BudgetLine:
    category: str
    kind: Literal["fixed", "flexible"]
    limit_cents: int

@dataclass(frozen=True)
class PaceResult:
    category: str
    kind: Literal["fixed", "flexible", "unbudgeted"]
    limit_cents: int              # 0 for unbudgeted
    spent_cents: int
    days_elapsed: int
    days_in_period: int
    pace: Optional[float]         # None for fixed, unbudgeted, or a limit of 0
    flagged: bool
    reason: Optional[Literal["pace", "over_limit"]]

def evaluate(lines, txns, today: date) -> list[PaceResult]   # lines first in given order, then unbudgeted by category
```

- The period is `month_start(today) .. today` inclusive. `days_elapsed = today.day`; `days_in_period` is the month's length; `elapsed_fraction = days_elapsed / days_in_period`.
- `spent` = `category_totals(spending(txns), period)`.
- **Flexible:** `pace = spent / (limit × elapsed_fraction)` when `limit > 0`.
- **`over_limit`:** any line with `spent > limit`. This takes precedence over `"pace"`.
- **`pace`:** a flexible line with `days_elapsed ≥ MIN_DAYS` and `pace ≥ PACE_FLAG`.
- **Fixed lines** are never judged on pace.
- **Unbudgeted spending:** a category with spending above 0 and no line becomes `kind="unbudgeted"`, `flagged=False`. TPC-19 decides what to do with it.

`BudgetLine` is what TPC-18 stores. `FittedLine` converts to it with `BudgetLine(category, kind, limit_cents)`.

## `close.py`

```python
SMOOTH_OLD, SMOOTH_ACTUAL = 0.7, 0.3
CLAMP = 0.15

@dataclass(frozen=True)
class Adjustment:
    category: str
    old_cents: int
    actual_cents: int
    new_cents: int
    source: Literal["accepted", "smoothed", "fixed"]

def close_period(lines: list[BudgetLine], actuals: dict[str, int], accepted: dict[str, int]) -> list[Adjustment]
```

For each line, in the given order (a category missing from `actuals` has an actual of 0):
1. **Accepted:** if the category is in `accepted`, `new` = that value, used as-is with no clamp.
2. **Fixed:** `new = actual if actual > 0 else old`.
3. **Flexible:** `new = round(0.7 × old + 0.3 × actual)`, clamped to `[round(old × 0.85), round(old × 1.15)]`.

Categories in `actuals` with no line are ignored; adding lines is TPC-22's or a re-fit's job. `actuals` comes from `category_totals` over the closed month.

## Tests (`backend/tests/`, each with a `docs/tests/<module>.md`)

**`test_budget_txns.py`**
- Income, both transfer categories and card payments are dropped; other loan payments are kept.
- A refund nets a category down, and a category that goes negative becomes 0.
- The date range is inclusive at both ends.
- Month helpers: Feb 2028 (leap year), Dec → Jan.

**`test_budget_fit.py`**
- **Normal:** 3 months of FOOD_AND_DRINK at $400/$450/$500 → limit $472.50 (median 450 × 1.05).
- **Outlier:** TRAVEL at $200/$1,900/$250 → limit $262.50 (median 250 × 1.05). August is recorded with its top transactions.
- **Fixed:** rent of $1,500 in the latest month → limit $1,500, with no markup. Rent paid a day early (latest month $0) → uses the previous month.
- **Refunds:** netted in the totals.
- **Exclusions:** income, transfers and card payments never produce lines.
- **Window:** current-month transactions are ignored. Short history (2 months) uses a median of 2. No complete month → empty.
- **Occasional:** a category spent in only 1 of 3 months is skipped.
- **Ordering:** lines and skipped categories come out sorted.

**`test_budget_pace.py`**
- **Mid-month:** a $300 limit and $200 spent on Oct 15 → pace ≈ 1.378 → flagged `"pace"`.
- **Early month:** days 1-4 never flag on pace, even at a huge pace. Spending past the limit on day 2 flags `"over_limit"`.
- **Threshold:** a pace of exactly 1.10 on day 5 flags.
- **Fixed:** rent paid on the 1st → not flagged and pace `None`. Fixed spending past its limit → `"over_limit"`.
- **Precedence:** `over_limit` beats `pace`.
- **Unbudgeted:** spending with no line is reported unflagged.
- **Month lengths:** Feb 29 2028 → `days_in_period == 29`; Jan 31 → 31.
- **Zero limit:** pace `None`, and any spending → `over_limit`.

**`test_budget_close.py`**
- **Smoothing:** old $400 → actual $500 gives $430 (within the clamp); old $400 → actual $380 gives $394.
- **Clamp:** actual $1,000 on old $400 gives $460 (clamped up); actual $0 on old $400 gives $340 (clamped down).
- **Accepted:** an accepted value of $600 on old $400 gives $600 (no clamp).
- **Fixed:** follows an actual of $1,550; keeps the old amount when the actual is 0.
- **Other:** a category missing from `actuals` is treated as 0; categories with no line are ignored; amounts round to whole cents.

## Docs

- Architecture 6.3-6.5:
  - refunds are netted;
  - outliers are months (1.5× the median);
  - fixed = the latest non-zero month, and it follows actual at close;
  - median-$0 categories are skipped;
  - unbudgeted spending is reported to the graph;
  - the module names.
- Decision log (2026-10-27): netted refunds, outlier months, fixed-line rules.
- Follow-up noted in section 7: align `get_spending` with netted refunds.

## Risks

| Risk | Mitigation |
|---|---|
| Netted budget pace disagrees with chat `get_spending` totals | Noted in the decision log and as a follow-up; small change to the gateway later |
| 1.5× outlier ratio, 1.10 pace and 5-day guard are untuned | Module constants; M5 tunes them on the benchmark and records the values |
| Three months of history is thin for seasonal categories | The doc's period-close smoothing adapts limits over time; seasonality is a known limit for the writeup |
