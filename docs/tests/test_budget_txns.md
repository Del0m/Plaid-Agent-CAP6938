# Tests: `backend/tests/test_budget_txns.py`

These tests cover `app/budget_engine/txns.py`, the shared base of the budget engine (TPC-17). It holds:
- the `Txn` value the engine works on;
- the filter for what counts as spending;
- per-category totals net of refunds;
- calendar-month helpers;
- the one rounding rule for money.

Fit, pace and close all build on these, so a mistake here would show up in every budget number.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_txns.py -v
```

## Shared setup

`t(day, cents, cat="FOOD_AND_DRINK", detailed=None, month=10, year=2026)` builds a `Txn` in October 2026 unless told otherwise.

---

## `test_spending_drops_non_spending`

**What it does:** From income, both transfer categories, a credit-card payment, a car-loan payment and a food purchase, only the car payment and the food purchase are kept, in input order.

**Why it's there:** Income isn't spending, a transfer moves money between the user's own accounts, and a card payment pays for purchases already counted. Counting any of them would double count. A car or student loan payment *is* the user's money going out, so only the card-payment subcategory is dropped from `LOAN_PAYMENTS`.

## `test_totals_net_refunds`

**What it does:** Purchases of $50 and $30 with a $20 refund total $60.

**Why it's there:** A returned purchase isn't spending. Counting it would raise false overspend alerts (the false-alarm metric).

## `test_totals_floor_at_zero`

**What it does:** A $10 purchase and a $40 refund in one category total $0, not −$30. The category is still listed.

**Why it's there:** A refund for something bought last month can land in a quiet month. Negative spending would lower medians and make pace look better than it is.

## `test_totals_range_inclusive`

**What it does:** With transactions on Sep 30, Oct 1, Oct 31 and Nov 1, the range Oct 1-Oct 31 counts only the middle two.

**Why it's there:** Month totals are built from first-to-last-day ranges. An off-by-one at either end would drop or double count rent paid on the 1st.

## `test_totals_omit_absent_categories`

**What it does:** A range with no transactions gives an empty dict.

**Why it's there:** Callers rely on "absent means no transactions" when they fill zeros for missing months.

## `test_month_helpers_leap_year`

**What it does:** February 2028 ends on the 29th, February 2027 on the 28th, and Feb 29 2028 starts on Feb 1.

**Why it's there:** `days_in_period` comes from `month_end`, so a leap-year slip would skew every pace in that February.

## `test_add_months_crosses_year`

**What it does:** Dec 2026 + 1 is Jan 2027, Jan 2027 − 1 is Dec 2026, and Oct 2026 − 3 is Jul 2026. A date that isn't the 1st is rejected.

**Why it's there:** The fit window runs back across the year boundary every January. The function only means something on the 1st (there's no Feb 31), so it fails loudly instead of guessing.

## `test_round_cents_half_up`

**What it does:** 1010.5¢ rounds to 1011, 1010.49¢ to 1010, and 47250.00¢ stays 47250.

**Why it's there:** Python's `round()` rounds halves to even, so it would give 1010. People expect money to round half up, and every limit the engine computes goes through this function.
