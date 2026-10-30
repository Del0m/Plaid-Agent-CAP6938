# Tests: `backend/tests/test_budget_close.py`

These tests cover `app/budget_engine/close.py`, which sets next month's limits when a month closes (TPC-17, architecture 6.5). For each budget line, in order:
1. **Accepted:** a value the user accepted (an `adjust_next_budget` action) is used as-is.
2. **Fixed:** the line follows the month's actual amount, or keeps its limit when the actual is $0.
3. **Flexible:** `new = 0.7 × old + 0.3 × actual`, clamped to ±15% of the old limit and rounded half up to the cent.

TPC-22's period close calls `close_period`.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_close.py -v
```

## Shared setup

- `flex(cat, cents)` and `fixed(cat, cents)` build `BudgetLine`s.
- `one(line, actual=None, accepted=None)` closes a single line and returns its `Adjustment`. With no actual, the category is left out of `actuals`.

---

## `test_smoothing_within_clamp`

**What it does:** A $400 limit with $500 actual becomes $430, and with $380 actual becomes $394. Both are marked `"smoothed"`.

**Why it's there:** It pins the 70/30 smoothing from the spec in both directions, inside the clamp.

## `test_clamp_up`

**What it does:** A $400 limit with $1,000 actual becomes $460 (+15%), not $580.

**Why it's there:** One expensive month shouldn't raise the budget by almost half. The clamp is what keeps the budget from chasing a bad month.

## `test_clamp_down`

**What it does:** A $400 limit with $0 actual becomes $340 (−15%), not $280.

**Why it's there:** A month with no spending (away, or the card was lost) shouldn't nearly halve the budget for when normal life resumes.

## `test_accepted_not_clamped`

**What it does:** An accepted $600 on a $400 line gives $600 with source `"accepted"`, though the clamp would allow at most $460. An accepted value also overrides a fixed line's actual.

**Why it's there:** The agent proposes and the user disposes (architecture 6.6). Once the user has agreed to a number, the engine must not quietly change it.

## `test_fixed_follows_actual`

**What it does:** A $1,500 rent line with $1,550 actual becomes $1,550, marked `"fixed"`.

**Why it's there:** A rent increase is the new normal. Smoothing toward it would flag the next three months as over the limit.

## `test_fixed_keeps_old_when_zero`

**What it does:** A $1,500 rent line stays $1,500 when the actual is $0 or missing.

**Why it's there:** Rent paid a day early lands in the previous month. A $0 month means it moved, not that rent stopped, so the line keeps its limit.

## `test_missing_actual_is_zero`

**What it does:** A flexible line missing from `actuals` gets an actual of 0 and is clamped down to $340.

**Why it's there:** `actuals` comes from `category_totals`, which leaves out categories with no transactions. Missing has to mean $0, not an error.

## `test_unlined_actuals_ignored`

**What it does:** Travel spending with no line doesn't produce an adjustment.

**Why it's there:** Adding a line is TPC-22's or a re-fit's decision, not something period close does on its own.

## `test_close_rounds_half_up`

**What it does:** A 1015¢ line with 1000¢ actual gives 1010.5¢, which becomes 1011¢.

**Why it's there:** Python's `round()` would give 1010 (half to even). Money rounds half up, the way a person checking the maths would expect.

## `test_order_preserved`

**What it does:** Lines given as [TRAVEL, FOOD, RENT] come back in that order.

**Why it's there:** The caller writes the adjustments back to its stored lines, and the agent lists them in the order the user is used to.

## `test_period_actuals_move_early_bill`

**What it does:** For October, with rent on Sep 30 (October's, paid early), a $0.01 charge on Oct 1, November's rent on Oct 31, $420 of food and a transfer. `period_actuals` gives rent $1,500.01 and food $420, leaving out the transfer, and `close_period` keeps the rent line at $1,500.01.

**Why it's there:** `close_period` takes the actuals as given, and a fixed line adopts its actual. With raw totals, a month holding next month's early rent became a $3,000 limit, and real overspending then went unflagged for two periods. The review on #15 found it. `period_actuals` applies the same early-bill rule as pace: Sep 30's rent counts toward October, and Oct 31's toward November. Period close (TPC-22) will build its actuals with it.
