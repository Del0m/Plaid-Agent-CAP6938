# Tests: `backend/tests/test_budget_users.py`

These tests cover `eval/budget_users.py`, the synthetic users for the budget accuracy eval (TPC-18). Each profile is a few spending patterns (weekly, monthly, one-off) run through a seeded random number generator. Every profile fits on May-July (`today` = Aug 1 2026) and is scored against August.

| Profile | Planted |
|---|---|
| `steady` | groceries ~$110 a week, dining twice a week, fuel, streaming, rent $1,500 |
| `vacation` | steady, plus $1,900 of travel in June |
| `early-rent` | steady, but July's rent is paid on June 30 |
| `rising` | groceries up 10% a month |
| `occasional` | steady, plus one $300 purchase in June |

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_users.py -v
```

## Shared setup

- `profile(id)` looks a profile up in `PROFILES`.
- `test_seed_profile_writes_rows` uses the `db` fixture from `conftest.py`.

---

## `test_same_seed_same_transactions`

**What it does:** Generating the steady profile twice gives identical transactions.

**Why it's there:** Accuracy results are compared across runs and code versions. A different random draw each time would make a change in MAPE meaningless.

## `test_vacation_june_travel`

**What it does:** The vacation profile's June travel adds up to exactly $1,900.

**Why it's there:** That's the planted outlier the median is supposed to ignore. If the trip moved or changed size, the eval would no longer test what it says it tests.

## `test_early_rent_doubles_june`

**What it does:** The early-rent profile has $3,000 of rent in June and none in July.

**Why it's there:** It's the case the fixed-line median rule was changed for. The eval checks that the fitted rent is one month's rent, so the planted double month has to be there.

## `test_rising_groceries_grow`

**What it does:** The rising profile's average grocery charge goes up every month, May to August.

**Why it's there:** The trend is the one case where the mean could beat the median. The ±10% noise must not hide the 10% monthly growth.

## `test_every_profile_has_income_and_transfer`

**What it does:** Every profile has a paycheck and a savings transfer.

**Why it's there:** The spending filter must drop them in every run. A profile without them wouldn't notice if the filter broke.

## `test_transactions_stay_in_range`

**What it does:** Every generated transaction falls between the profile's start (May 1) and end (Aug 31).

**Why it's there:** A transaction outside the range would quietly change either the fit window or the scored month.

## `test_profile_ids`

**What it does:** The profiles are steady, vacation, early-rent, rising and occasional, in that order.

**Why it's there:** The ids name result rows and decision-log notes. Renaming or reordering one should be a deliberate change.

## `test_seed_profile_writes_rows`

**What it does:** Seeding the steady profile writes one transaction row per generated transaction, readable from a fresh session.

**Why it's there:** The eval runs the real `initialize_budget` on these rows. A dropped row would change the budget without any error.
