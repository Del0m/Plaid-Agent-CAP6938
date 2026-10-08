# Tests: `backend/tests/test_sync.py`

These tests cover pulling data from Plaid after an item is linked (`app/plaid_service/sync.py`, `app/plaid_service/webhooks.py`):
- the `/transactions/sync` cursor loop
- idempotent upserts of accounts, transactions and liabilities
- the `POST /plaid/items/{id}/sync` route
- the `SYNC_UPDATES_AVAILABLE` webhook

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_sync.py -v
```

## Shared setup

Uses the `db` and `client` fixtures from `conftest.py` (see [test_plaid.md](test_plaid.md#shared-setup-backendtestsconftestpy)).

Helpers in the test file:
- **`FakePlaid` / `fake` fixture:** replaces `plaid_service.client.plaid_post`.
  - `/transactions/sync` responses are looked up by the request's `cursor` (`None` for the first page), the way real Plaid behaves.
  - A list of responses is served one per call, so a test can make the same cursor fail once and then succeed.
  - Other endpoints read from `fake.responses[path]`.
  - Any response can be a `PlaidError`, which is raised instead of returned.
  - `fake.calls` records every path that was called.
- **`account`, `txn`, `page`, `liabilities_response`:** build Plaid-shaped payloads with only the fields we read.
- **`seed_item(db, ...)`:** writes a user and a linked item (with an encrypted token) straight to the db, skipping `/plaid/exchange`.
- **`run_sync`, `count`, `get_txn`, `get_item`:** run a sync and read rows back, each in its own session, the same way separate requests would.

---

## `test_sync_follows_pages_and_saves_cursor`

**What it does:** Serves two pages (`has_more` true, then false) and runs one sync. It checks that all three transactions and the one account are stored, that the item's `txn_cursor` is the last page's `next_cursor`, and that `12.29` is stored as `1229` cents.

**Why it's there:** If a sync stops after the first page, it silently loses data. If it saves the wrong cursor, the next run either re-pulls everything or skips transactions. The cents check covers `int(12.29 * 100) == 1228`: every amount has to go through `round`.

## `test_sync_twice_adds_no_duplicates`

**What it does:** Syncs twice in the normal way, then clears the cursor and syncs again, so Plaid replays the full history. It expects 2 transactions and 1 account after all three runs.

**Why it's there:** This is TPC-9's done-when. The rows and the cursor are committed together, so a crash before the commit means the next run replays pages we may already have. Upserting on `plaid_txn_id` / `plaid_account_id` is what makes the replay safe. If someone switches the upsert to a plain insert, this test fails (or the unique constraint raises).

## `test_modified_and_removed_update_in_place`

**What it does:** The second page modifies `t1`'s amount and removes `t2` plus an id we never stored. It checks that there are still 2 rows, `t1` has the new amount, `t2` has `removed=True`, and the unknown id is ignored.

**Why it's there:** `/transactions/sync` returns three lists, and each needs different handling. Removed transactions are soft-deleted (flagged, not deleted) so the history and anything that referenced them stays. A pending transaction that posts arrives as "removed (pending id) + added (posted id)", and Plaid can remove ids we never saw, so that case must not crash.

## `test_mutation_during_pagination_restarts_from_start_cursor`

**What it does:**
1. The first attempt fetches a page with a `stale` transaction.
2. The next page raises `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`.
3. The retry gets fresh pages.

It checks that `stale` was never written, that both fresh transactions were, and that the cursor is the final one.

**Why it's there:** Plaid's docs say that when this error happens, you must restart from the cursor the loop *started* with, not the last one. Pages are collected in memory and only written at the end, so a restart throws them away cleanly.

## `test_failed_sync_writes_nothing`

**What it does:** Page 1 succeeds, page 2 raises `ITEM_LOGIN_REQUIRED`. It expects the error to propagate, no transactions to be stored, and the cursor to still be `None`.

**Why it's there:** Saving page 1's rows with a stale cursor, or the cursor without the rows, would leave the db out of step with Plaid. All-or-nothing keeps the next run correct.

## `test_liabilities_store_aprs_and_upsert`

**What it does:** Serves a credit card with a cash APR and a purchase APR, a student loan, and `mortgage: null`, then refreshes twice. It checks:
- there are two rows
- the card stores the purchase APR (15.24, not 27.99)
- `min_payment_cents` and `balance_cents` are in cents
- the student loan stores its interest rate

**Why it's there:** The second half of TPC-9's done-when is "liabilities show APRs", and the dashboard sorts debts by APR. Plaid gives a credit card a *list* of APRs, so we have to choose one; purchase APR is what applies to normal spending. Refreshing twice with no new rows is the `liabilities.account_id` unique constraint at work.

## `test_liabilities_not_supported_is_skipped`

**What it does:** `/liabilities/get` raises `PRODUCTS_NOT_SUPPORTED`. It expects an empty result and no liability rows.

**Why it's there:** Liabilities is only `required_if_supported_products` (see `test_link_token_requires_only_transactions`). That means some linked banks won't have it, and a full sync of those items shouldn't fail.

## `test_sync_route_runs_full_sync`

**What it does:** Calls `POST /plaid/items/{id}/sync` for the dev user's item. It checks that the response reports the added transaction and the credit APR, and that the account's balance was updated by `/accounts/balance/get`.

**Why it's there:** It tests the route wiring (auth dependency, all three Plaid calls in order, response shape) that `scripts/sandbox_link.py` and the frontend rely on.

## `test_same_txn_twice_in_one_run`

**What it does:** Serves two pages in one sync. Page 1 adds `t9`. Page 2 (fetched with cursor `c1`) lists `t9` again as modified, with amount 15.00. The test checks that there's one row and that it holds the modified amount (1500 cents).

**Why it's there:** A transaction can change while we're paging (for example, a pending charge's amount updates), so the same id can show up in both `added` and `modified` within one run. The database lookup in `sync_transactions` only finds rows stored by *earlier* runs, so `t9` isn't there yet. The line `existing[pt["transaction_id"]] = txn` puts the new object in the dict, so the second sighting updates it instead of creating another. Without that line, two objects with the same `plaid_txn_id` reach the commit, the unique constraint raises `IntegrityError`, and the whole sync fails. This was confirmed by commenting the line out and watching the test fail. No other mocked test sends one id twice in a single run, so this is the only test that catches that bug.

## `test_sync_route_hides_other_users_items`

**What it does:** Syncs an item owned by a different user. It expects a 404 and checks that Plaid was never called.

**Why it's there:** Item ids are sequential integers. Without the ownership check, any user could pull another user's bank data by guessing ids. Returning 404 instead of 403 also hides which ids exist.

## `test_webhook_triggers_sync`

**What it does:** Posts a `TRANSACTIONS` / `SYNC_UPDATES_AVAILABLE` webhook for the seeded item. It expects a 200, and checks that the transaction was stored and the cursor advanced (`TestClient` runs background tasks before returning).

**Why it's there:** The webhook is how new transactions arrive without polling. The sync runs as a background task in its own session (`sync_in_background`), because the request's session is closed by then. This test makes sure that session uses the same database.

## `test_webhook_ignores_other_codes_and_unknown_items`

**What it does:** Parametrized over a different code (`DEFAULT_UPDATE`) and an unknown `item_id`. Both should return 200 without calling Plaid.

**Why it's there:** Plaid sends many webhook types and retries anything that isn't a 2xx. Ignoring the ones we don't handle, while still acknowledging them, avoids pointless retries and keeps unrelated events from triggering syncs.

## `test_sandbox_sync_end_to_end`

**What it does:** Calls the real Plaid Sandbox with nothing mocked.
1. Links First Platypus Bank.
2. Syncs until a run adds nothing.
3. Clears the cursor and syncs again, replaying the whole history.

It checks that the row count didn't change and that at least one credit liability has an APR.

**Why it's there:** It's the done-when, checked against real Plaid responses instead of our guess at their shape. A new Sandbox item loads history over several seconds and kept sending new transactions for one sync *after* reporting `HISTORICAL_UPDATE_COMPLETE`. So the test waits until a run adds nothing instead of trusting the status value. It's skipped without Sandbox credentials, like `test_sandbox_link_end_to_end`.

---

## Not covered

- **Webhook signature verification.** `/plaid/webhook` trusts the payload. Anyone who can reach the URL can trigger a sync (but can't read data or choose what's stored). Verifying the `Plaid-Verification` JWT is a TODO in `app/api/plaid.py`.
- **Two syncs of one item at the same time** (webhook and manual sync together). Both would fetch the same pages. Whichever commits second hits the unique constraint on a newly inserted transaction and fails, and the next sync retries it. There's no test, because that requires real concurrency.
- **`/accounts/balance/get` errors** go through the same `_plaid_failure` path as other routes and aren't tested separately.
