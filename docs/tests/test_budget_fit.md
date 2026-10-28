# Tests: `backend/tests/test_budget_fit.py`

These tests cover `app/budget_engine/fit.py`, which turns transaction history into the user's first budget (TPC-17, architecture 6.3). The rules:
- The history is the last 3 complete calendar months.
- **Fixed** categories (rent, loan payments) get the latest month's amount.
- **Flexible** categories get the median month × 1.05.
- A month above 1.5× the median is recorded as an outlier, so the agent can explain it.
- A flexible category whose median is $0 is skipped.

TPC-18's `initialize_budget` calls `fit_budget` and stores the result.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_fit.py -v
```

## Shared setup

- `TODAY` is Oct 15 2026, so the window is July, August and September 2026.
- `t(y, m, d, cents, cat, ...)` builds a `Txn`.
- `monthly(cat, *cents, start_month=7)` builds one transaction per month, skipping zeros.
- `line(result, cat)` finds a fitted line.
- `NORMAL` is FOOD_AND_DRINK at $400, $450 and $500.

---

## `test_normal_flexible_median`

**What it does:** FOOD_AND_DRINK at $400/$450/$500 gives one flexible line with a limit of $472.50 (median $450 × 1.05), the three totals oldest first, and no outliers.

**Why it's there:** It's the "normal" case from the spec's done-when, and it pins the median-plus-5% rule.

## `test_outlier_month_recorded`

**What it does:** TRAVEL at $200, then $1,900 in August (four purchases), then $250 gives a limit of $262.50. August is the one outlier, recorded with its total, the $250 median and its three biggest purchases ($1,200, $400, $200).

**Why it's there:** It's the spec's outlier case. The median keeps one trip from inflating the budget. The recorded month lets the agent say "Travel was $1,900 in August and was left out" instead of an unexplained number.

## `test_fixed_uses_latest_month_no_markup`

**What it does:** Rent of $1,500 each month gives a fixed line of exactly $1,500, with no outliers.

**Why it's there:** A fixed bill isn't padded: 5% extra on rent would be budget that can never be spent.

## `test_fixed_paid_early_uses_previous_month`

**What it does:** Rent paid in July and on Aug 31 (for September), but nothing in September, still gives $1,500. The September total is 0.

**Why it's there:** Paying rent a day early moves it into the previous month. "Most recent amount" has to mean the latest month that has one, or the budget would get a $0 rent line.

## `test_custom_fixed_set`

**What it does:** Passing `fixed={"ENTERTAINMENT"}` makes entertainment a fixed line at its latest month ($15).

**Why it's there:** TPC-33's recurring detector will add subscription categories to the fixed set. This checks the parameter is honoured, not just the default.

## `test_refunds_netted`

**What it does:** September's $500 purchase and $100 refund total $400.

**Why it's there:** A returned purchase isn't spending, so it shouldn't raise next month's limit.

## `test_refund_only_category_ignored`

**What it does:** A category with only refunds in the window gets no line and isn't listed as skipped.

**Why it's there:** A refund for something bought before the window nets to $0. The user never spent in that category during the window, so it's not a budget category at all.

## `test_exclusions_never_lines`

**What it does:** Income, transfers both ways and credit-card payments in every month give an empty result.

**Why it's there:** These would double count (architecture 6.3 step 1). A "TRANSFER_OUT" budget line would be nonsense to the user.

## `test_current_month_ignored`

**What it does:** A $1,000 food purchase on Oct 3 doesn't change the limit fitted from July-September.

**Why it's there:** The current month is partial. Fitting it would drag every median down, or (as here) let one early purchase move the limit.

## `test_short_history_two_months`

**What it does:** With history starting in August, the window is August and September, and the limit is the median of 2 ($450 × 1.05).

**Why it's there:** A newly linked account may have less than 3 months. July would otherwise count as a $0 month and drag the median down.

## `test_no_complete_month_empty`

**What it does:** Transactions only in the current month, or none at all, give an empty result.

**Why it's there:** With no complete month there is nothing to fit, and returning empty is better than inventing limits from a partial month.

## `test_occasional_category_skipped`

**What it does:** A $300 home-improvement purchase in only one of three months is listed in `skipped` and gets no line.

**Why it's there:** Its median is $0. A $0 limit would flag every future purchase as overspending.

## `test_lines_and_skipped_sorted`

**What it does:** Lines come out in alphabetical order (ENTERTAINMENT, FOOD_AND_DRINK, TRAVEL), and so do skipped categories.

**Why it's there:** A stable order makes the stored budget, the agent's explanation and these tests reproducible.

## `test_fit_accepts_generator`

**What it does:** Passing the transactions as a generator gives the same result as a list.

**Why it's there:** `fit_budget` reads the transactions more than once. A one-shot iterator would otherwise come back empty on the second pass and silently produce an empty budget.

## `test_fit_window_crosses_year`

**What it does:** On Jan 10 2027 the window is October-December 2026, and the limit is $472.50.

**Why it's there:** Every January the window runs back across the year. A month-arithmetic slip would fit the wrong months or none.
