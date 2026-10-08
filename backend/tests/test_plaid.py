import pytest
from sqlalchemy import select

from app import config
from app.db.models import PlaidItem, User
from app.plaid_service import client as plaid
from app.security import decrypt


# stand-in for plaid's /item/public_token/exchange: hands out the given tokens in order, same item_id
def fake_exchange(monkeypatch, item_id, *tokens):
    it = iter(tokens)
    monkeypatch.setattr(plaid, "exchange_public_token", lambda _public_token: (next(it), item_id))


# every plaid_items row in the test db
def items(db):
    with db() as s:
        return s.scalars(select(PlaidItem)).all()


# link token asks for transactions only and makes liabilities required-if-supported,
# so Link still shows banks without liabilities
def test_link_token_requires_only_transactions(client, monkeypatch):
    sent = {}

    def fake_post(path, body):
        sent.update(body)
        return {"link_token": "link-sandbox-test"}

    monkeypatch.setattr(plaid, "plaid_post", fake_post)

    resp = client.post("/plaid/link-token")

    assert resp.status_code == 200
    assert resp.json() == {"link_token": "link-sandbox-test"}
    # liabilities in products would hide banks that only support transactions
    assert sent["products"] == ["transactions"]
    assert sent["required_if_supported_products"] == ["liabilities"]


# plaid only sends SYNC_UPDATES_AVAILABLE to items created with a webhook url,
# so the link token carries PLAID_WEBHOOK_URL when it's set and omits it otherwise
@pytest.mark.parametrize("url", ["https://example.ngrok.app/plaid/webhook", None])
def test_link_token_sets_webhook_only_when_configured(client, monkeypatch, url):
    sent = {}

    def fake_post(path, body):
        sent.update(body)
        return {"link_token": "link-sandbox-test"}

    monkeypatch.setattr(plaid, "plaid_post", fake_post)
    monkeypatch.setattr(config, "PLAID_WEBHOOK_URL", url)

    assert client.post("/plaid/link-token").status_code == 200
    assert sent.get("webhook") == url


# linking a new bank creates one active row with the access_token encrypted
def test_first_link_stores_item(client, db, monkeypatch):
    fake_exchange(monkeypatch, "item-1", "access-1")

    resp = client.post("/plaid/exchange", json={"public_token": "public-1"})

    assert resp.status_code == 200
    [item] = items(db)
    assert resp.json() == {"item_id": item.id}
    assert item.plaid_item_id == "item-1"
    assert item.status == "active"
    # stored encrypted, never as the plaintext token
    assert item.access_token_enc != "access-1"
    assert decrypt(item.access_token_enc) == "access-1"


# running Link again for an already-stored item returns the same row (no 500),
# swaps in the new token and reactivates it
def test_relinking_same_item_updates_existing_row(client, db, monkeypatch):
    fake_exchange(monkeypatch, "item-1", "access-1", "access-2")
    first = client.post("/plaid/exchange", json={"public_token": "public-1"})
    with db() as s:
        s.scalar(select(PlaidItem)).status = "error"
        s.commit()

    second = client.post("/plaid/exchange", json={"public_token": "public-2"})

    assert second.status_code == 200
    assert second.json() == first.json()
    [item] = items(db)
    assert decrypt(item.access_token_enc) == "access-2"
    assert item.status == "active"


# an item that belongs to a different user gets 409 and their row is left untouched
def test_item_owned_by_another_user_is_409(client, db, monkeypatch):
    fake_exchange(monkeypatch, "item-1", "access-1", "access-2")
    client.post("/plaid/exchange", json={"public_token": "public-1"})
    with db() as s:
        s.add(User(id="someone-else", email="else@example.com", password_hash="!"))
        s.flush()
        s.scalar(select(PlaidItem)).user_id = "someone-else"
        s.commit()

    resp = client.post("/plaid/exchange", json={"public_token": "public-2"})

    assert resp.status_code == 409
    [item] = items(db)
    assert item.user_id == "someone-else"
    assert decrypt(item.access_token_enc) == "access-1"


# real round trip against plaid sandbox: link token, sandbox public_token, exchange,
# then check the stored item; skipped when .env has no plaid credentials
@pytest.mark.skipif(
    not (config.PLAID_CLIENT_ID and config.PLAID_SECRET) or config.PLAID_ENV != "sandbox",
    reason="needs PLAID_CLIENT_ID/PLAID_SECRET with PLAID_ENV=sandbox",
)
def test_sandbox_link_end_to_end(client, db):
    assert client.post("/plaid/link-token").status_code == 200

    resp = client.post("/plaid/exchange", json={"public_token": plaid.sandbox_public_token()})

    assert resp.status_code == 200
    [item] = items(db)
    assert item.plaid_item_id
    assert decrypt(item.access_token_enc).startswith("access-sandbox-")
