# Tests: `backend/tests/test_budget_pace.py`

These tests cover `app/budget_engine/pace.py`, which decides each day which budget lines are overspending (TPC-17, architecture 6.4). Spending is counted from the 1st of the month to `today`:

```
pace = spent / (limit × days_elapsed / days_in_period)
flag "pace"        if a flexible line has pace ≥ 1.10 and days_elapsed ≥ 5
flag "over_limit"  if any line has spent > limit (this wins over "pace")
```

Spending in a category with no budget line is reported as `unbudgeted`, unflagged. TPC-19's `detect_overspend` calls `evaluate` and decides what to do with each row.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_pace.py -v
```

## Shared setup

- `flex(cat, cents)` and `fixed(cat, cents)` build `BudgetLine`s.
- `t(y, m, d, cents, cat="FOOD_AND_DRINK")` builds a `Txn`.
- `one(lines, txns, today)` runs `evaluate` and expects exactly one result.
- `by_cat` indexes results by category.

---

## `test_mid_month_pace_flags`

**What it does:** A $300 limit with $200 spent by Oct 15 gives 15 of 31 days, pace ≈ 1.378, flagged as `"pace"`.

**Why it's there:** It's the spec's mid-month case. Half the month gone with two-thirds of the budget spent is what the alert exists for.

## `test_under_pace_not_flagged`

**What it does:** $100 of $300 by Oct 15 isn't flagged and has no reason.

**Why it's there:** Every false alarm counts against the agent in the false-alarm metric, so normal spending must stay quiet.

## `test_early_month_never_flags_on_pace`

**What it does:** On each of days 1-4, $250 of a $300 limit (a huge pace) is not flagged, though the pace is still reported.

**Why it's there:** The 5-day guard keeps one grocery trip on the 1st from triggering an alert. The pace is still returned so the dashboard can show it.

## `test_early_month_over_limit_flags`

**What it does:** $350 spent against a $300 limit on Oct 2 is flagged `"over_limit"`.

**Why it's there:** The 5-day guard only applies to pace. Going past the limit is overspending on any day.

## `test_pace_threshold_inclusive`

**What it does:** On Nov 5 (a 30-day month), $110 of a $600 limit is a pace of exactly 1.10 and flags. $109.99 doesn't.

**Why it's there:** The rule is "≥ 1.10". It pins the edge, and that floating point doesn't turn an exact 1.10 into 1.0999.

## `test_fixed_rent_on_first_not_flagged`

**What it does:** $1,500 rent paid on Oct 1 against a $1,500 fixed line, checked on Oct 10, isn't flagged and has no pace.

**Why it's there:** Rent is spent in one go on the 1st. Judged on pace it would look like a 1,000% overspend every month.

## `test_fixed_over_limit`

**What it does:** $1,550 rent against a $1,500 fixed line is flagged `"over_limit"`, with no pace.

**Why it's there:** A fixed bill that went up is worth telling the user about, even though pace never applies to it.

## `test_over_limit_beats_pace`

**What it does:** $310 of $300 on Oct 15 is over pace too, but the reason is `"over_limit"`.

**Why it's there:** Over the limit is the stronger message, and the agent words its alert from the reason.

## `test_unbudgeted_reported_unflagged`

**What it does:** With a line for food only, travel and entertainment spending come back after the food row, sorted by category. Each is `kind="unbudgeted"`, with limit 0, no pace and not flagged.

**Why it's there:** Spending with no budget line shouldn't vanish, and it shouldn't be called overspending either. TPC-19 decides whether to suggest a new line.

## `test_refund_only_not_unbudgeted`

**What it does:** A refund in a category with no line produces no row.

**Why it's there:** Its net spending is $0, so there's nothing unbudgeted to report. Without this, a refund would prompt "you spent $0 on General Merchandise".

## `test_month_lengths`

**What it does:** Feb 29 2028 gives 29 of 29 days. Jan 31 2027 gives a 31-day period.

**Why it's there:** `days_in_period` is the month's real length (architecture 6.4). A fixed 30 would skew pace at the end of long months and all through February.

## `test_zero_limit`

**What it does:** A $0 flexible line has no pace. With nothing spent it isn't flagged. With $1 spent it's `"over_limit"`.

**Why it's there:** Pace divides by the limit. A user who set a $0 limit means "don't spend here", so any spending at all is over.

## `test_evaluate_ignores_other_periods`

**What it does:** Of transactions on Sep 30, Oct 1, Oct 15 and Oct 16, only the two in Oct 1-15 count when today is Oct 15.

**Why it's there:** Last month's spending isn't this month's, and a future-dated pending charge hasn't happened yet. Either would push pace up.

## `test_lines_keep_given_order_and_excluded_skipped`

**What it does:** Lines given as [TRAVEL, FOOD] come back in that order, each with $0 spent. A paycheck produces no row.

**Why it's there:** Callers match results to their stored lines by position, so the order must hold. Income is never spending, budgeted or not.
