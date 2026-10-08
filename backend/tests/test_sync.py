import time

import pytest
from sqlalchemy import func, select

from app import config
from app.db.models import Account, Liability, PlaidItem, Transaction, User
from app.plaid_service import client as plaid
from app.plaid_service.client import PlaidError
from app.plaid_service.sync import refresh_liabilities, sync_transactions
from app.security import encrypt


# stand-in for plaid_post. /transactions/sync pages are looked up by the cursor in the request
# (None = first page), like real plaid; a list of pages is served in order, one per call.
# any value can be a PlaidError, which is raised instead of returned.
class FakePlaid:
    def __init__(self):
        self.sync_pages = {}
        self.responses = {}
        self.calls = []

    def __call__(self, path, body):
        self.calls.append(path)
        if path == "/transactions/sync":
            resp = self.sync_pages[body.get("cursor")]
            if isinstance(resp, list):
                resp = resp.pop(0)
        else:
            resp = self.responses[path]
        if isinstance(resp, PlaidError):
            raise resp
        return resp


@pytest.fixture
def fake(monkeypatch):
    f = FakePlaid()
    monkeypatch.setattr(plaid, "plaid_post", f)
    return f


def account(account_id="acc-1", current=100.0, type_="depository"):
    return {"account_id": account_id, "name": f"Account {account_id}", "mask": "0000",
            "type": type_, "subtype": None, "balances": {"current": current, "available": None, "limit": None}}


def txn(txn_id, amount=12.29, account_id="acc-1"):
    return {"transaction_id": txn_id, "account_id": account_id, "amount": amount,
            "date": "2026-10-01", "merchant_name": "Cafe", "pending": False,
            "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"}}


def page(added=(), modified=(), removed=(), next_cursor="c-end", has_more=False):
    return {"added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": r} for r in removed],
            "accounts": [account()], "next_cursor": next_cursor, "has_more": has_more,
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}


# a user with one linked item, written straight to the db (no exchange call)
def seed_item(db, user_id="u-1", email="sync@test"):
    with db() as s:
        s.add(User(id=user_id, email=email, password_hash="!"))
        item = PlaidItem(user_id=user_id, plaid_item_id=f"item-{user_id}",
                         access_token_enc=encrypt("access-sandbox-test"))
        s.add(item)
        s.commit()
        return item.id


def run_sync(db, item_id):
    with db() as s:
        return sync_transactions(s, s.get(PlaidItem, item_id))


def count(db, model):
    with db() as s:
        return s.scalar(select(func.count()).select_from(model))


def get_txn(db, txn_id):
    with db() as s:
        return s.scalar(select(Transaction).where(Transaction.plaid_txn_id == txn_id))


def get_item(db, item_id):
    with db() as s:
        return s.get(PlaidItem, item_id)


# follows has_more across pages, stores every txn in cents, and saves the last cursor
def test_sync_follows_pages_and_saves_cursor(db, fake):
    item_id = seed_item(db)
    fake.sync_pages[None] = page(added=[txn("t1"), txn("t2")], next_cursor="c1", has_more=True)
    fake.sync_pages["c1"] = page(added=[txn("t3")], next_cursor="c2")

    result = run_sync(db, item_id)

    assert result["added"] == 3
    assert count(db, Transaction) == 3
    assert count(db, Account) == 1
    assert get_item(db, item_id).txn_cursor == "c2"
    assert get_txn(db, "t1").amount_cents == 1229     # int(12.29 * 100) would give 1228


# the ticket's done-when: replaying the same pages (a crash before the cursor was saved)
# leaves the same rows, no duplicates
def test_sync_twice_adds_no_duplicates(db, fake):
    item_id = seed_item(db)
    fake.sync_pages[None] = page(added=[txn("t1"), txn("t2")], next_cursor="c1")
    fake.sync_pages["c1"] = page(next_cursor="c1")    # nothing new since c1

    run_sync(db, item_id)
    run_sync(db, item_id)                            # normal second run from the saved cursor
    with db() as s:
        s.get(PlaidItem, item_id).txn_cursor = None  # forget the cursor and replay from scratch
        s.commit()
    run_sync(db, item_id)

    assert count(db, Transaction) == 2
    assert count(db, Account) == 1


# modified updates the existing row; removed flags it instead of deleting
def test_modified_and_removed_update_in_place(db, fake):
    item_id = seed_item(db)
    fake.sync_pages[None] = page(added=[txn("t1"), txn("t2")], next_cursor="c1")
    fake.sync_pages["c1"] = page(modified=[txn("t1", amount=20.00)], removed=["t2", "never-seen"], next_cursor="c2")

    run_sync(db, item_id)
    run_sync(db, item_id)

    assert count(db, Transaction) == 2
    assert get_txn(db, "t1").amount_cents == 2000
    assert get_txn(db, "t2").removed is True


# plaid's data changed mid-loop: pages already fetched are dropped and the loop
# restarts from the cursor the run began with
def test_mutation_during_pagination_restarts_from_start_cursor(db, fake):
    item_id = seed_item(db)
    mutated = PlaidError(400, "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "changed")
    fake.sync_pages[None] = [page(added=[txn("stale")], next_cursor="c1", has_more=True),
                             page(added=[txn("t1")], next_cursor="c1", has_more=True)]
    fake.sync_pages["c1"] = [mutated, page(added=[txn("t2")], next_cursor="c2")]

    run_sync(db, item_id)

    assert get_txn(db, "stale") is None
    assert {t for t in ("t1", "t2") if get_txn(db, t)} == {"t1", "t2"}
    assert get_item(db, item_id).txn_cursor == "c2"


# any other plaid error aborts the sync with nothing written and the cursor unchanged
def test_failed_sync_writes_nothing(db, fake):
    item_id = seed_item(db)
    fake.sync_pages[None] = page(added=[txn("t1")], next_cursor="c1", has_more=True)
    fake.sync_pages["c1"] = PlaidError(400, "ITEM_LOGIN_REQUIRED", "relink")

    with pytest.raises(PlaidError):
        run_sync(db, item_id)

    assert count(db, Transaction) == 0
    assert get_item(db, item_id).txn_cursor is None


def liabilities_response():
    return {
        "accounts": [account("card-1", current=410.5, type_="credit"), account("loan-1", current=6500, type_="loan")],
        "liabilities": {
            "credit": [{"account_id": "card-1", "minimum_payment_amount": 20,
                        "next_payment_due_date": "2026-11-01",
                        "aprs": [{"apr_type": "cash_apr", "apr_percentage": 27.99},
                                 {"apr_type": "purchase_apr", "apr_percentage": 15.24}]}],
            "student": [{"account_id": "loan-1", "interest_rate_percentage": 5.25,
                         "minimum_payment_amount": 25, "next_payment_due_date": "2026-11-05"}],
            "mortgage": None,
        },
    }


# credit cards store the purchase APR (not cash), student loans their rate; refreshing twice
# updates the same rows
def test_liabilities_store_aprs_and_upsert(db, fake):
    item_id = seed_item(db)
    fake.responses["/liabilities/get"] = liabilities_response()

    for _ in range(2):
        with db() as s:
            refresh_liabilities(s, s.get(PlaidItem, item_id))

    with db() as s:
        rows = {l.kind: l for l in s.scalars(select(Liability))}
    assert set(rows) == {"credit", "student"}
    assert rows["credit"].apr == 15.24
    assert rows["credit"].min_payment_cents == 2000
    assert rows["credit"].balance_cents == 41050
    assert rows["student"].apr == 5.25
    assert count(db, Liability) == 2


# liabilities is only required-if-supported, so a bank without it is skipped, not an error
def test_liabilities_not_supported_is_skipped(db, fake):
    item_id = seed_item(db)
    fake.responses["/liabilities/get"] = PlaidError(400, "PRODUCTS_NOT_SUPPORTED", "nope")

    with db() as s:
        assert refresh_liabilities(s, s.get(PlaidItem, item_id)) == []
    assert count(db, Liability) == 0


# the route runs transactions, balances and liabilities and reports what it stored
def test_sync_route_runs_full_sync(client, db, fake):
    from app.api.deps import DEV_USER_EMAIL
    item_id = seed_item(db, email=DEV_USER_EMAIL)
    fake.sync_pages[None] = page(added=[txn("t1")])
    fake.responses["/accounts/balance/get"] = {"accounts": [account(current=250.0)]}
    fake.responses["/liabilities/get"] = liabilities_response()

    resp = client.post(f"/plaid/items/{item_id}/sync")

    assert resp.status_code == 200
    body = resp.json()
    assert body["transactions"]["added"] == 1
    assert {"kind": "credit", "apr": 15.24} in body["liabilities"]
    with db() as s:
        assert s.scalar(select(Account).where(Account.plaid_account_id == "acc-1")).current_balance_cents == 25000

# check to see that an updated transaction in one run updates with the latest information
def test_same_txn_twice_in_one_run(db, fake):
    item_id = seed_item(db)

    # make first entry, overwrite entry with new information
    # relies on existing[pt["transaction_id"]] = txn in sync.py to run correctly
    fake.sync_pages[None] = page(added=[txn("t9")], next_cursor="c1", has_more=True)
    fake.sync_pages["c1"] = page(modified=[txn("t9", amount=15.00)], next_cursor="c2")

    run_sync(db, item_id)

    assert count(db, Transaction) == 1
    assert get_txn(db, "t9").amount_cents == 1500

# another user's item is a 404, the same as a missing one, and plaid is never called
def test_sync_route_hides_other_users_items(client, db, fake):
    item_id = seed_item(db, user_id="someone-else", email="other@test")

    resp = client.post(f"/plaid/items/{item_id}/sync")

    assert resp.status_code == 404
    assert fake.calls == []


def webhook(code="SYNC_UPDATES_AVAILABLE", item_id="item-u-1"):
    return {"webhook_type": "TRANSACTIONS", "webhook_code": code, "item_id": item_id}


# SYNC_UPDATES_AVAILABLE syncs that item's transactions after the 200 is sent
def test_webhook_triggers_sync(client, db, fake):
    item_id = seed_item(db)
    fake.sync_pages[None] = page(added=[txn("t1")])

    resp = client.post("/plaid/webhook", json=webhook())

    assert resp.status_code == 200
    assert count(db, Transaction) == 1
    assert get_item(db, item_id).txn_cursor == "c-end"


# other webhook codes and unknown items still get 200 (so plaid stops retrying) but do nothing
@pytest.mark.parametrize("payload", [webhook(code="DEFAULT_UPDATE"), webhook(item_id="item-unknown")])
def test_webhook_ignores_other_codes_and_unknown_items(client, db, fake, payload):
    seed_item(db)

    resp = client.post("/plaid/webhook", json=payload)

    assert resp.status_code == 200
    assert fake.calls == []


# real sandbox: link, sync until plaid stops sending new transactions, then forget the cursor
# and replay all of history. the replay must add no rows, and the credit card must have an APR.
@pytest.mark.skipif(
    not (config.PLAID_CLIENT_ID and config.PLAID_SECRET) or config.PLAID_ENV != "sandbox",
    reason="needs PLAID_CLIENT_ID/PLAID_SECRET with PLAID_ENV=sandbox",
)
def test_sandbox_sync_end_to_end(client, db):
    resp = client.post("/plaid/exchange", json={"public_token": plaid.sandbox_public_token()})
    item_id = resp.json()["item_id"]

    # sandbox keeps delivering history for a sync or two even after HISTORICAL_UPDATE_COMPLETE,
    # so "settled" means a run that added nothing, not a status value
    for _ in range(15):
        time.sleep(2)
        added = client.post(f"/plaid/items/{item_id}/sync").json()["transactions"]["added"]
        if added == 0 and count(db, Transaction) > 0:
            break
    settled = count(db, Transaction)
    with db() as s:
        s.get(PlaidItem, item_id).txn_cursor = None
        s.commit()
    client.post(f"/plaid/items/{item_id}/sync")

    assert settled > 0
    assert count(db, Transaction) == settled
    with db() as s:
        assert any(l.apr is not None for l in s.scalars(select(Liability).where(Liability.kind == "credit")))
