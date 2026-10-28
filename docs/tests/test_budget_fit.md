# Tests: `backend/tests/test_budget_fit.py`

These tests cover `app/budget_engine/fit.py`, which turns transaction history into the user's first budget (TPC-17, architecture 6.3). The rules:
- The history is the last 3 complete calendar months.
- **Fixed** categories (rent, loan payments) get the lower median of the months that had the bill (TPC-18 replaced "the latest month").
- **Flexible** categories get the median month × 1.05. `center="mean"` uses the mean instead; it exists only for the median-vs-mean accuracy comparison (TPC-18).
- A month above 1.5× the median is recorded as an outlier, so the agent can explain it.
- A flexible category whose median is $0 is skipped.

The result also reports its `window`, the months it fitted. TPC-18's `initialize_budget` calls `fit_budget` and stores the result.

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
- `RENT` is `"RENT_AND_UTILITIES"`, and `rent(result)` finds its line.

---

## `test_normal_flexible_median`

**What it does:** FOOD_AND_DRINK at $400/$450/$500 gives one flexible line with a limit of $472.50 (median $450 × 1.05), the three totals oldest first, and no outliers.

**Why it's there:** It's the "normal" case from the spec's done-when, and it pins the median-plus-5% rule.

## `test_outlier_month_recorded`

**What it does:** TRAVEL at $200, then $1,900 in August (four purchases), then $250 gives a limit of $262.50. August is the one outlier, recorded with its total, the $250 median and its three biggest purchases ($1,200, $400, $200).

**Why it's there:** It's the spec's outlier case. The median keeps one trip from inflating the budget. The recorded month lets the agent say "Travel was $1,900 in August and was left out" instead of an unexplained number.

## `test_fixed_median_no_markup`

**What it does:** Rent of $1,500 each month gives a fixed line of exactly $1,500, with no outliers.

**Why it's there:** A fixed bill isn't padded: 5% extra on rent would be budget that can never be spent.

## `test_fixed_paid_early_double_month`

**What it does:** Rent paid Jul 1, Aug 1 and Aug 31 (September's, a day early) gives totals of $1,500 / $3,000 / $0 and a limit of $1,500.

**Why it's there:** Paying early puts two rents in one month. The old "latest non-zero month" rule turned this into a $3,000 rent line, 100% off the next month (TPC-18). The lower median of the paying months is one rent.

## `test_fixed_double_in_last_month`

**What it does:** Rent paid Jul 1, Aug 1, Sep 1 and Sep 30 gives $1,500, even though the latest month holds $3,000.

**Why it's there:** The early payment can land in the last month of the window, with no $0 month after it. Taking the latest month would double the rent.

## `test_fixed_new_lease_ignores_leading_zeros`

**What it does:** Rent first paid in September, with food in July keeping the window at July-September, gives totals $0 / $0 / $1,500 and a limit of $1,500.

**Why it's there:** A median over all three months would be $0. Months with no rent (here, before the lease) don't count.

## `test_fixed_increase_not_adopted_at_init`

**What it does:** Rent of $1,500 / $1,500 / $1,600 fits at $1,500.

**Why it's there:** It pins the cost of the median rule. A rent increase isn't adopted at init; period close (where a fixed line follows the actual amount) adopts it one month later.

## `test_fixed_even_months_takes_lower`

**What it does:** Rent totals of $0 / $1,500 / $3,000 give a limit of $1,500, not $2,250.

**Why it's there:** This is the usual first-link case: Plaid's history starts mid-July, so July has no rent, and October's rent was paid early on Sep 30. With two paying months, a plain median averages one rent with two. The lower median is always a real month's amount (found in the TPC-18 final review).

## `test_fixed_zero_month_between`

**What it does:** Rent totals of $0 / $1,500 / $0 give $1,500, not $750.

**Why it's there:** A fixed bill is never really $0 in a month; a zero is an artifact of when the payment landed. Only months that had the bill count.

## `test_mean_center_flexible`

**What it does:** With `center="mean"`, TRAVEL at $200 / $1,900 / $250 gives $822.50 (the $783.33 mean × 1.05), and August is still the outlier.

**Why it's there:** The mean is the comparison the accuracy eval measures (architecture 6.3). It is computed exactly with `Decimal`, so the expected cents are exact.

## `test_mean_center_keeps_occasional`

**What it does:** With `center="mean"`, SHOPPING at $0 / $300 / $0 gets a $105 line instead of being skipped.

**Why it's there:** This is the mean's known weakness, and the accuracy eval counts it: a line for a purchase that won't happen next month.

## `test_mean_center_leaves_fixed_alone`

**What it does:** With `center="mean"`, early-paid rent still fits at $1,500.

**Why it's there:** `center` only changes flexible lines, so the median-vs-mean comparison differs in exactly one thing.

## `test_window_reported`

**What it does:** The window is July, August and September for `NORMAL`; August and September for history starting in August; and empty with no transactions or only current-month ones.

**Why it's there:** `initialize_budget` stores the window so the agent can say which months a budget came from. It has to match the months actually fitted, including for short history.

## `test_custom_fixed_set`

**What it does:** Passing `fixed={"ENTERTAINMENT"}` makes $10 / $20 / $15 of entertainment a fixed line at its median ($15).

**Why it's there:** TPC-33's recurring detector will add subscription categories to the fixed set. This checks the parameter is honoured, not just the default.

## `test_refunds_netted`

**What it does:** September's $500 purchase and $100 refund total $400.

**Why it's there:** A returned purchase isn't spending, so it shouldn't raise next month's limit.

## `test_refund_only_category_ignored`

**What it does:** A category with only refunds in the window gets no line and isn't listed as skipped.

**Why it's there:** A refund for something bought before the window nets to $0. The user never spent in that category during the window, so it's not a budget category at all.

## `test_exclusions_never_lines`

**What it does:** Income, transfers both ways and credit-card payments in every month give no lines and no skipped categories.

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
