# Tests: `backend/tests/test_budget_users.py`

These tests cover `eval/budget_users.py`, the synthetic users for the budget accuracy eval (TPC-18). Each profile is a few spending patterns (weekly, monthly, one-off) run through a seeded random number generator. Every profile fits on May-July (`today` = Aug 1 2026) and is scored against August.

| Profile | Planted |
|---|---|
| `steady` | groceries ~$110 a week, dining twice a week, fuel, streaming, rent $1,500 |
| `vacation` | steady, plus $1,900 of travel in June |
| `early-rent` | steady, but July's rent is paid on June 30 |
| `rising` | groceries up 10% a month |
| `occasional` | steady, plus one $300 purchase in June |

`PLANTED` (TPC-19) holds three more users for the budget graph, each with one planted overspend and the cause `diagnose` should find. They use a minimal base (rent, paycheck, savings transfer), so the planted category is the only flag.

| Profile | Planted | Run on | Cause |
|---|---|---|---|
| `overspend-one-time` | $600 at ElectroMart on Oct 12, against ~$150/month of general merchandise | Oct 20 | `one_time` |
| `overspend-recurring` | a $90 gym first charged Sep 3, again Oct 3, against a $40/month barber | Oct 20 | `recurring` |
| `overspend-seasonal` | eight $60-$120 purchases at eight shops, Dec 1-14 | Dec 15 | `seasonal` |
| `overspend-known-merchant` | a $520 one-off on Oct 12 at MegaMart, which usually comes to $120 a month | Oct 20 | `one_time` |

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_users.py -v
```

## Shared setup

- `profile(id)` looks a profile up in `PROFILES`, and `planted(id)` in `PLANTED`.
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

## `test_planted_one_time_purchase`

**What it does:** The one-time user has a $600 ElectroMart purchase on Oct 12.

**Why it's there:** It's the overspend `diagnose` must call `one_time`. If it moved or shrank, the demo would test something else.

## `test_planted_gym_starts_september`

**What it does:** The gym is charged exactly on Sep 3 and Oct 3, and never before.

**Why it's there:** Starting in the last history month keeps the gym out of the fitted median, so October is over the limit. Starting earlier would leave nothing to flag. It also checks `Monthly(first=...)`.

## `test_planted_december_shops`

**What it does:** The seasonal user has eight different shops in December, besides the usual corner shop.

**Why it's there:** Spread across many shops, no single purchase dominates, and that is what separates `seasonal` from `one_time` in the facts.

## `test_planted_flags_only_the_planted_category`

**What it does:** For each planted user, it fits the three months before the run date and evaluates pace on the run date. Exactly one category is flagged: personal care for the gym, general merchandise for the other two.

**Why it's there:** An unrelated flag would add an alert and LLM calls to the demo. A steady user's streaming charge and weekday counts already raise flags of their own (a TPC-17 follow-up), which is why planted users use a minimal base.

## `test_planted_carry_run_date_and_cause`

**What it does:** Each planted user has its run date and expected cause: Oct 20 for one-time, recurring and known-merchant, and Dec 15 for seasonal.

**Why it's there:** The seed script prints the run command from `run_date`, and the live test checks `diagnose` against `expected_cause`.

## `test_profiles_unchanged`

**What it does:** The accuracy-eval profiles are the same five as before, and none has a run date.

**Why it's there:** The planted users are a separate tuple, so the TPC-18 accuracy results stay comparable.

## `test_planted_known_merchant_big_purchase`

**What it does:** The known-merchant user has its regular $120 MegaMart charge on the 8th of each month, July to October, plus a $520 purchase on Oct 12.

**Why it's there:** It was found in the TPC-19 review: a big one-off at a regular shop was diagnosed `recurring`, which, before the guard, could propose a higher budget for a one-off. The live check now covers it.

## Seeding up to a start date (TPC-21)

`seeded_rows(db, pid, until)` seeds a profile, then reads back its transactions and the user's `prefs_json`.

## `test_seed_until_sets_sim`

**What it does:** Seeding the one-time user until Sep 30 writes only transactions up to that day, exactly as many as were generated by then. The user is marked as simulated at Sep 30.

**Why it's there:** A simulated user's later days are injected one at a time (TPC-21). Anything seeded ahead would already be in the ledger, and the dashboard would show it early.

## `test_seed_without_until_unchanged`

**What it does:** Without `until`, every transaction is written and the user isn't simulated.

**Why it's there:** The accuracy eval and the TPC-19 demo seed the full history, and must keep doing so.

## `test_seed_ids_stable`

**What it does:** The seeded Plaid ids are `sim-overspend-one-time-0`, `-1` and so on, the same in two separate databases.

**Why it's there:** Injecting a simulated day checks these ids, so retrying a day can never insert its transactions twice.

## `test_until_before_profile_start`

**What it does:** A start date before the profile's first transaction writes nothing, and still marks the user as simulated at that date.

**Why it's there:** Every day is then injected by the simulation. It mustn't fail just because no history exists yet.

## `test_default_start`

**What it does:** `--start default` means Sep 30 for the one-time user, Nov 30 for the seasonal user, and Jul 31 for the steady accuracy user.

**Why it's there:** It's the day before the month with the planted overspend, so the first advanced day creates that month's budget.
