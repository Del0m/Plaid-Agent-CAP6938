# Tests: `backend/tests/test_plaid.py`

These tests cover linking a bank through Plaid: getting a link token, then exchanging the `public_token` from Link for an access token we store. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_plaid.py -v
```

## Shared setup (`backend/tests/conftest.py`)

| Fixture | What it does | Why |
|---|---|---|
| `db` | Gives each test its own empty in-memory SQLite database and returns a session factory for it. | Tests never touch `dev.db` and can't see each other's rows. |
| `client` | A FastAPI `TestClient` with `get_session` overridden to use `db`. | Requests run through the real routes, dependencies and models, just against the throwaway database. |

`app.security` refuses to import without a `FERNET_KEY`, so `conftest.py` generates a temporary one when none is set. A key from your `.env` takes precedence.

The test file has two helpers:
- `fake_exchange(monkeypatch, item_id, *tokens)` swaps out `plaid_service.client.exchange_public_token`. Each call hands back the next token in `tokens`, always with the same `item_id`. That's how the tests make Plaid "return" an item we've already stored.
- `items(db)` returns all `plaid_items` rows so a test can count them and look at what's in them.

---

## `test_link_token_requires_only_transactions`

**What it does:** Replaces `plaid_post` with a fake that records the request body, calls `POST /plaid/link-token`, and checks that the link token comes back. It also checks that the body sent to Plaid had `products: ["transactions"]` and `required_if_supported_products: ["liabilities"]`.

**Why it's there:** Link only lists institutions that support everything in `products`. Putting `liabilities` there hides every bank that has Transactions but not Liabilities. Under `required_if_supported_products`, those banks still show up, and liabilities is still enabled at banks that have it. If someone moves liabilities back into `products`, this test fails.

## `test_first_link_stores_item`

**What it does:** Exchanges a token for an item we haven't seen before. It checks that the response is 200 with the new row's id, and that there's exactly one row with the right `plaid_item_id` and `status == "active"`. It also checks that `access_token_enc` isn't the plaintext token but decrypts back to it.

**Why it's there:** It's the normal first-time link, which nothing else works without. It also checks the privacy rule from section 7 of the architecture doc: access tokens are encrypted at rest.

## `test_relinking_same_item_updates_existing_row`

**What it does:** Links `item-1` and sets the stored row's status to `error`, then exchanges `item-1` again with a different access token. The second call should return 200 with the same `item_id`. There should still be one row, now holding the new token with its status back to `active`.

**Why it's there:** A user can run Link again for a bank they've already connected, and Plaid can come back with an `item_id` we already have. `plaid_item_id` is unique, so a plain insert fails and the request returns a 500. The endpoint updates the existing row instead. This test keeps it that way and makes sure the newest token is the one we store.

## `test_item_owned_by_another_user_is_409`

**What it does:** Links `item-1`, moves the row to a different user, and then exchanges `item-1` again as the dev user. It expects a 409 and checks that the row still belongs to the other user and still holds the original token.

**Why it's there:** Updating an existing item on a repeat link is only safe when the item belongs to the person linking it. Otherwise, one user could overwrite another user's access token. The test confirms that anyone else gets a 409 and the stored row isn't changed.

## `test_sandbox_link_end_to_end`

**What it does:** Calls the real Plaid Sandbox, with nothing mocked. It creates a link token through the API, gets a sandbox `public_token` for First Platypus Bank, and exchanges it through `POST /plaid/exchange`. It then checks that one row was stored and that its decrypted token starts with `access-sandbox-`.

**Why it's there:** The other tests mock Plaid, so they pass even if Plaid would reject our request because of a renamed field, a product combination it doesn't allow, or bad credentials. This one confirms that Plaid actually accepts the requests. It's skipped when `.env` is missing `PLAID_CLIENT_ID` or `PLAID_SECRET`, or when `PLAID_ENV` isn't `sandbox`, so the rest of the suite still runs offline.

---

## Not covered

- **Two exchanges for the same item at the same moment.** `/plaid/exchange` catches the unique-constraint error and returns 409. That only happens when another request inserts the item between our lookup and our commit, which can't be reproduced without mocking the lookup, so there's no test for it.
