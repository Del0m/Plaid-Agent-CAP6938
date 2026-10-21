# Tests: `backend/tests/test_dashboard.py`

These tests cover `GET /dashboard`, the endpoint behind the dashboard page. It returns account balances, net worth, and debts sorted by APR, read straight from our database. Nothing here calls Plaid. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_dashboard.py -v
```

## Shared setup

The `db` and `client` fixtures from `conftest.py` are described in [`test_plaid.md`](test_plaid.md#shared-setup-backendtestsconftestpy).

The test file has these helpers:
- `dev_user_id(db)` inserts the `dev@localhost` user that `get_current_user` returns, and returns its id. Each request runs as that user, so seeding rows for this id is how a test gives "the current user" data.
- `seed(db, user_id, accounts, institution=..., email=None, cursor="c-1")` writes one `plaid_items` row and its accounts straight to the database. An account can also carry a `liability`. It returns the account ids in order. Passing `email` creates the user row too, which the "another user" test uses. `cursor=None` marks the item as never synced.
- `acct(...)` and `credit(...)` are small builders for the dicts `seed` takes, so the tests read as data.

---

## `test_empty_user_gets_zeros`

**What it does:** Calls the endpoint for a user with nothing linked and expects a 200 with zero totals and empty lists, including `unsynced_items`.

**Why it's there:** A first-time user hasn't linked a bank yet. The page decides whether to show the "Connect a bank" card by checking for an empty `accounts` list, so the endpoint must return a normal empty response, not a 404 or an error.

## `test_net_worth_is_assets_minus_debts`

**What it does:** Seeds one account of each type: depository, investment, credit and loan. It checks that assets = 60000, debts = 23000, and net worth = 37000.

**Why it's there:** This is the net worth definition from the spec: depository and investment balances count as assets, and credit and loan balances count as debts. Plaid reports both sides as positive numbers. If a type ends up on the wrong side, or a debt is added instead of subtracted, this test fails.

## `test_other_types_listed_not_totaled`

**What it does:** Seeds a checking account and an `other`-type account. It checks that only the checking balance counts toward the totals, but both accounts are listed.

**Why it's there:** Plaid has account types we can't sensibly place on either side. They should still appear in the accounts table, but they shouldn't change net worth.

## `test_null_balance_skipped_and_counted`

**What it does:** Seeds a checking account with a balance, plus a savings account and a card that both have a `None` balance. It checks that the totals include only the checking balance and that `accounts_missing_balance == 2`.

**Why it's there:** Plaid can leave a balance out. Treating a missing balance as 0 would understate net worth with no sign anything was wrong. Counting the missing ones lets the page say that some accounts weren't included.

## `test_negative_balances_are_summed_as_is`

**What it does:** Seeds an overdrawn checking account (-5000), a savings account (8000), and a card with a credit balance (-2000). It expects assets = 3000, debts = -2000, and net worth = 5000.

**Why it's there:** An overdrawn account really does lower what you own, and a card you overpaid really does lower what you owe. Applying `abs()` to "clean up" negatives would get both wrong. This was one of the Review Focus risks in the plan.

## `test_debt_type_without_liability_counts_in_totals_only`

**What it does:** Seeds a loan account with no liability row, and a card that has one. It checks that both count toward `debts_cents`, but only the card appears in the `debts` list.

**Why it's there:** The debts list is built from liability rows, because that's where the APR is stored. The totals, though, are built from account types, so a loan from a bank without Liabilities support still reduces net worth. This test pins that split, so a change to one side can't quietly change the other.

## `test_debts_sorted_by_apr_then_balance`

**What it does:** Seeds four cards. They are A at 15% with a 100 balance, B at 24.99% with 100, C with an unknown APR and 900, and D at 15% with 500. It expects the order B, D, A, C.

**Why it's there:** The ticket asks for debts sorted by APR, which is avalanche order, the same order the M2 agent will recommend paying them off in. The test checks the three rules together: highest APR first, an unknown APR last (not first, which is where a plain descending sort on a null would put it), and the bigger balance first when two APRs tie.

## `test_accounts_ordered_assets_debts_other`

**What it does:** Seeds four accounts with mixed types and names. It expects the order beta (investment), zeta (depository), alpha (credit), then mid (other).

**Why it's there:** The accounts table groups assets, then debts, then everything else, and sorts by name within each group. Without a fixed order the table would come out in insertion order, which changes after every sync.

## `test_other_users_data_hidden`

**What it does:** Seeds the dev user with one account, then seeds a second user with an account and a card that has a liability. It checks that the response contains only the dev user's account, an empty debts list, and the dev user's total.

**Why it's there:** Every row is scoped by `user_id` (architecture section 4.2). The endpoint makes two queries, accounts and liabilities, and each needs its own user filter. Forgetting the filter on either one would show another person's money, and this test catches that.

## `test_institution_name_from_item`

**What it does:** Seeds an account under an item named "First Platypus Bank" and checks that the account's `institution_name` matches.

**Why it's there:** The institution name is stored on the Plaid item, not on the account (it comes from Link at exchange time), so the endpoint has to join through the item to get it. This test makes sure that join stays in place.

## `test_debt_fields`

**What it does:** Seeds a credit card with a 15.24% APR, a balance of 41000, a minimum payment of 2000, and a due date of 2026-11-15. It checks the whole debt object field by field, including `next_due_date` serialized as `"2026-11-15"`.

**Why it's there:** The frontend's `Debt` type copies these field names exactly. Renaming or dropping a field here would break the debts table without any backend test failing, so this test pins the whole shape.

## `test_unsynced_items_listed`

**What it does:** Seeds three items: one for the dev user that was never synced (`txn_cursor` is `None`), one for the dev user that was synced, and a never-synced item belonging to another user. It expects `unsynced_items` to list only the dev user's never-synced item, with exactly `id` and `institution_name`.

**Why it's there:** When the first sync fails during linking, the page needs a way to retry. If only the browser remembered the failed sync, a refresh would lose it. The user would see "Connect a bank", link the same bank again, and end up with a duplicate item. Listing never-synced items from the database keeps Retry sync on screen across refreshes. `txn_cursor` is only written once a transactions sync finishes, which is why the endpoint uses it as the marker. The other user's item checks that this third query is scoped to the user too.

---

## Not covered

- **Account balance vs liability balance.** The totals use `accounts.current_balance_cents`, but each debt row shows `liabilities.balance_cents`, which is copied from the account when liabilities are refreshed. Both are written during the same `sync_item`, but if a balance refresh succeeds and the liability refresh then fails, the two can differ until the next sync. No test sets up that state.
