# Tests: `backend/tests/test_plaid.py`

These tests cover linking a bank through Plaid: getting a link token, exchanging the `public_token` from Link for an access token we store, and the first sync that the exchange runs before returning. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_plaid.py -v
```

## Shared setup (`backend/tests/conftest.py`)

| Fixture | What it does | Why |
|---|---|---|
| `db` | Gives each test its own empty in-memory SQLite database and returns a session factory for it. | Tests never touch `dev.db` and can't see each other's rows. |
| `client` | A FastAPI `TestClient` with `get_session` overridden to use `db`. | Requests run through the real routes, dependencies and models, just against the throwaway database. |
| `checkpoint_paths` (autouse) | Points `BUDGET_CHECKPOINT_PATH` and `CHAT_CHECKPOINT_PATH` at the test's `tmp_path`, and clears the cached savers before and after. | Code that opens config's path, such as the budget CLI, used to write to and delete threads in the developer's `backend/data/*.db` (review on #13). |

`app.security` refuses to import without a `FERNET_KEY`, so `conftest.py` generates a temporary one when none is set. A key from your `.env` takes precedence.

The test file has two helpers and one fixture:
- `fake_exchange(monkeypatch, item_id, *tokens)` swaps out `plaid_service.client.exchange_public_token`. Each call hands back the next token in `tokens`, always with the same `item_id`. That's how the tests make Plaid "return" an item we've already stored.
- `items(db)` returns all `plaid_items` rows so a test can count them and look at what's in them.
- The `synced` fixture replaces `sync_item` in `app.api.plaid` with a stub that records the `plaid_item_id` of each item it's called with, and returns that list. Every exchange test that isn't about sync failures uses it. Without it, the exchange would call Plaid for real with a fake token.

---

## `test_link_token_requires_only_transactions`

**What it does:** Replaces `plaid_post` with a fake that records the request body, calls `POST /plaid/link-token`, and checks that the link token comes back. It also checks that the body sent to Plaid had `products: ["transactions"]` and `required_if_supported_products: ["liabilities"]`.

**Why it's there:** Link only lists institutions that support everything in `products`. Putting `liabilities` there hides every bank that has Transactions but not Liabilities. Under `required_if_supported_products`, those banks still show up, and liabilities is still enabled at banks that have it. If someone moves liabilities back into `products`, this test fails.

## `test_link_token_sets_webhook_only_when_configured`

**What it does:** Parametrized over a webhook URL and `None`. It sets `config.PLAID_WEBHOOK_URL`, calls `POST /plaid/link-token`, and checks that the body sent to Plaid has `webhook` set to that URL, or has no `webhook` key when it's unset.

**Why it's there:** Plaid only sends `SYNC_UPDATES_AVAILABLE` to items that were created with a webhook URL, and the URL is fixed when the item is linked. If the link token drops the URL, new transactions stop arriving without any error. Sending an empty URL when none is configured would be rejected by Plaid.

## `test_first_link_stores_item`

**What it does:** Exchanges a token for an item we haven't seen before. It checks that the response is 200 with the new row's id and `synced: false, syncing: true`, and that there's exactly one row with the right `plaid_item_id` and `status == "active"`. It also checks that `access_token_enc` isn't the plaintext token but decrypts back to it.

**Why it's there:** It's the normal first-time link, which nothing else works without. It also checks the privacy rule from section 7 of the architecture doc: access tokens are encrypted at rest.

## `test_relinking_same_item_updates_existing_row`

**What it does:** Links `item-1` and sets the stored row's status to `error`, then exchanges `item-1` again with a different access token. The second call should return 200 with the same `item_id`. There should still be one row, now holding the new token with its status back to `active`.

**Why it's there:** A user can run Link again for a bank they've already connected, and Plaid can come back with an `item_id` we already have. `plaid_item_id` is unique, so a plain insert fails and the request returns a 500. The endpoint updates the existing row instead. This test keeps it that way and makes sure the newest token is the one we store.

## `test_item_owned_by_another_user_is_409`

**What it does:** Links `item-1`, moves the row to a different user, and then exchanges `item-1` again as the dev user. It expects a 409 and checks that the row still belongs to the other user and still holds the original token.

**Why it's there:** Updating an existing item on a repeat link is only safe when the item belongs to the person linking it. Otherwise, one user could overwrite another user's access token. The test confirms that anyone else gets a 409 and the stored row isn't changed.

## `test_exchange_syncs_new_item`

**What it does:** Exchanges a token for a new item and checks that the response says `syncing: true` and that `sync_item` was called once, for that item.

**Why it's there:** The dashboard reads only from our database. If linking doesn't sync, the user links a bank and then sees an empty page until a webhook or a manual sync arrives. This test makes sure the first sync is part of linking.

## `test_exchange_syncs_on_relink`

**What it does:** Links `item-1` twice and checks that `sync_item` ran both times.

**Why it's there:** Re-linking is how a user repairs a broken connection, and the new token may reach data the old one couldn't. The re-link path returns early, so it's easy to forget the sync there. This test catches that.

## `test_sync_failure_keeps_item_and_reports_unsynced`

**What it does:** Parametrized over the errors a sync can raise: a `PlaidError`, an `httpx` network error, the `RuntimeError` from pagination that keeps changing, and a `KeyError` standing in for an unexpected Plaid payload or any other bug. For each, the background `sync_item` raises it. The test expects the queued response (`synced: false, syncing: true`) and checks that the stored item becomes `sync_failed` and appears in the dashboard retry list.

**Why it's there:** By the time the sync runs, the exchange has already succeeded and the access token is valid. Returning an error, or deleting the item, would make the user link the same bank again for a problem that a retry would fix. Any exception counts, not just the expected three, because a 500 here would tell the user that linking failed when the item is in fact stored. The dashboard lists the item under `unsynced_items` and offers a Retry sync button.

## `test_institution_name_stored_and_kept`

**What it does:** Exchanges with `institution_name: "First Platypus Bank"` and checks that it's stored on the item. Then it re-links without the field and checks that the name is still there.

**Why it's there:** The dashboard shows which bank each account belongs to, and Link only gives us the name in its `onSuccess` metadata. A re-link that leaves the field out shouldn't wipe a name we already have.

## `test_failed_exchange_does_not_sync`

**What it does:** Makes the exchange raise a Plaid 400 (`INVALID_PUBLIC_TOKEN`). It expects a 400, no call to `sync_item`, and no stored item.

**Why it's there:** When the exchange itself fails, there's no item to sync and no token to use. This test keeps the existing error behavior and makes sure the new sync step only runs after a successful exchange.

## `test_sandbox_link_end_to_end`

**What it does:** Calls the real Plaid Sandbox, with nothing mocked. It creates a link token through the API, gets a sandbox `public_token` for First Platypus Bank, and exchanges it through `POST /plaid/exchange`, which also runs the real first sync. It then checks that the response says `syncing: true`, that one row was stored, and that its decrypted token starts with `access-sandbox-`.

**Why it's there:** The other tests mock Plaid, so they pass even if Plaid would reject our request because of a renamed field, a product combination it doesn't allow, or bad credentials. This one confirms that Plaid actually accepts the requests. It's skipped when `.env` is missing `PLAID_CLIENT_ID` or `PLAID_SECRET`, or when `PLAID_ENV` isn't `sandbox`, so the rest of the suite still runs offline.

---

## Not covered

- **Two exchanges for the same item at the same moment.** `/plaid/exchange` catches the unique-constraint error and returns 409. That only happens when another request inserts the item between our lookup and our commit, which can't be reproduced without mocking the lookup, so there's no test for it.


## `test_exchange_returns_before_background_sync_and_background_owns_session`

**What it does:** Captures the background task instead of executing it. Exchange returns a queued-sync response with no sync calls yet; the dashboard reports syncing. Executing the task after the request session closes completes the sync in its own session and returns the item to active.

**Why it is there:** Loading a long bank history must not delay the link response beyond the proxy timeout, and background work must not reuse the closed request session.
