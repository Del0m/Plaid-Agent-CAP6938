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
- `monthly(cat, *cents, start_month=7)` builds one transaction per month on the 2nd, skipping zeros. It's on the 2nd because history that starts after the 3rd drops its first month.
- `line(result, cat)` finds a fitted line.
- `NORMAL` is FOOD_AND_DRINK at $400, $450 and $500.

---

## `test_normal_flexible_median`

**What it does:** FOOD_AND_DRINK at $400/$450/$500 gives one flexible line with a limit of $472.50 (median $450 × 1.05), the three totals oldest first, and no outliers.

**Why it's there:** It's the "normal" case from the spec's done-when, and it pins the median-plus-5% rule.

## `test_outlier_month_recorded`

**What it does:** TRAVEL at $200 (on Jul 2, so July counts as a full month), then $1,900 in August (four purchases), then $250 gives a limit of $262.50. August is the one outlier, recorded with its total, the $250 median and its three biggest purchases ($1,200, $400, $200).

**Why it's there:** It's the spec's outlier case. The median keeps one trip from inflating the budget. The recorded month lets the agent say "Travel was $1,900 in August and was left out" instead of an unexplained number.

## `test_fixed_uses_latest_month_no_markup`

**What it does:** Rent of $1,500 each month gives a fixed line of exactly $1,500, with no outliers.

**Why it's there:** A fixed bill isn't padded: 5% extra on rent would be budget that can never be spent.

## `test_fixed_paid_early_moves_to_next_month`

**What it does:** Rent paid in July and on Aug 31 (for September) gives $1,500, with monthly totals $1,500 / $0 / $1,500: the Aug 31 payment counts toward September.

**Why it's there:** Rent paid in a month's last 3 days, within 10% of the usual bill, belongs to the next month. This used to assert totals of $1,500 / $1,500 / $0, with the payment left in August. That only worked here because August had no rent of its own; see `test_early_rent_counts_toward_next_month`.

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

## `test_first_month_after_the_3rd_not_fitted`

**What it does:** History starts Aug 25 ($100 of food), with $450 in September. Only September is fitted, giving totals `(45000,)` and a $472.50 limit.

**Why it's there:** A first month that's only partly covered was fitted as if it were complete. A user who links mid-month got one or two weeks of spending as a full month, which pulled the median, and every limit, down. The rule: the first month counts only if its earliest transaction is on day 1–3. The review on #15 (and #11) found it.

## `test_first_month_from_the_3rd_counts`

**What it does:** History that starts on Aug 3 keeps August: totals `(40000, 50000)`.

**Why it's there:** It pins the edge of the rule. Day 3 still counts, because a quiet first couple of days doesn't mean the month is missing.

## `test_early_rent_counts_toward_next_month`

**What it does:** Rent posts Jul 1, Aug 1 and Aug 30 (September's). The totals are $1,500 for each month, and the limit is $1,500.

**Why it's there:** Under the latest-non-zero rule, August held two rents and fitted $3,000, twice the real bill. The review on #15 found it. The Aug 30 charge is within 10% of the usual bill (the lower median of each month's largest rent charge) and in August's last 3 days, so it moves to September.
