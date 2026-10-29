from datetime import date

from app.api.deps import DEV_USER_EMAIL
from app.db.models import Account, Liability, PlaidItem, User


# the user every request runs as (see api/deps.py); made here so tests can seed rows for it
def dev_user_id(db) -> str:
    with db() as s:
        s.add(User(id="dev", email=DEV_USER_EMAIL, password_hash="!"))
        s.commit()
    return "dev"


# one item for user_id with the given accounts, written straight to the db. each account is a dict:
# {"name", "type", "balance": cents|None, "mask"?, "liability"?: {"kind", "apr", "balance", "min"?, "due"?}}.
# creates the User row when email is given (for "another user" tests). cursor=None = never synced.
# returns account ids in order
def seed(db, user_id, accounts, institution="Test Bank", email=None, cursor="c-1") -> list[int]:
    ids = []
    with db() as s:
        if email:
            s.add(User(id=user_id, email=email, password_hash="!"))
        item = PlaidItem(user_id=user_id, plaid_item_id=f"item-{user_id}-{institution}",
                         access_token_enc="x", institution_name=institution, txn_cursor=cursor)
        s.add(item)
        s.flush()
        for i, a in enumerate(accounts):
            acct = Account(user_id=user_id, item_id=item.id, plaid_account_id=f"acc-{item.id}-{i}",
                           name=a["name"], mask=a.get("mask"), type=a["type"],
                           current_balance_cents=a["balance"])
            s.add(acct)
            s.flush()
            if "liability" in a:
                l = a["liability"]
                s.add(Liability(user_id=user_id, account_id=acct.id, kind=l["kind"], apr=l["apr"],
                                balance_cents=l["balance"], min_payment_cents=l.get("min"),
                                next_due_date=l.get("due")))
            ids.append(acct.id)
        s.commit()
    return ids


def acct(name, type_, balance, **extra):
    return {"name": name, "type": type_, "balance": balance, **extra}


def credit(apr, balance, **extra):
    return {"kind": "credit", "apr": apr, "balance": balance, **extra}


# a user who hasn't linked anything gets an empty dashboard, not a 404; the page shows its connect card
def test_empty_user_gets_zeros(client, db):
    dev_user_id(db)

    resp = client.get("/dashboard")

    assert resp.status_code == 200
    assert resp.json() == {
        "net_worth_cents": 0, "assets_cents": 0, "debts_cents": 0,
        "accounts_missing_balance": 0, "accounts": [], "debts": [], "unsynced_items": [],
    }


# depository + investment are assets, credit + loan are debts
def test_net_worth_is_assets_minus_debts(client, db):
    seed(db, dev_user_id(db), [
        acct("checking", "depository", 10000),
        acct("brokerage", "investment", 50000),
        acct("card", "credit", 3000),
        acct("loan", "loan", 20000),
    ])

    body = client.get("/dashboard").json()

    assert body["assets_cents"] == 60000
    assert body["debts_cents"] == 23000
    assert body["net_worth_cents"] == 37000


# types we can't place on either side are still shown but don't move the totals
def test_other_types_listed_not_totaled(client, db):
    seed(db, dev_user_id(db), [acct("checking", "depository", 10000), acct("misc", "other", 999)])

    body = client.get("/dashboard").json()

    assert body["assets_cents"] == 10000
    assert body["debts_cents"] == 0
    assert len(body["accounts"]) == 2


# plaid can omit a balance; treating it as 0 would quietly understate the total, so it's counted instead
def test_null_balance_skipped_and_counted(client, db):
    seed(db, dev_user_id(db), [
        acct("checking", "depository", 10000),
        acct("savings", "depository", None),
        acct("card", "credit", None),
    ])

    body = client.get("/dashboard").json()

    assert body["assets_cents"] == 10000
    assert body["debts_cents"] == 0
    assert body["accounts_missing_balance"] == 2


# overdrawn checking lowers assets; a card with a credit balance lowers debts. no abs()
def test_negative_balances_are_summed_as_is(client, db):
    seed(db, dev_user_id(db), [
        acct("checking", "depository", -5000),
        acct("savings", "depository", 8000),
        acct("card", "credit", -2000),
    ])

    body = client.get("/dashboard").json()

    assert body["assets_cents"] == 3000
    assert body["debts_cents"] == -2000
    assert body["net_worth_cents"] == 5000


# the debts list needs liability data (APR); totals go by account type
def test_debt_type_without_liability_counts_in_totals_only(client, db):
    seed(db, dev_user_id(db), [
        acct("loan", "loan", 20000),
        acct("card", "credit", 3000, liability=credit(20.0, 3000)),
    ])

    body = client.get("/dashboard").json()

    assert body["debts_cents"] == 23000
    assert [d["name"] for d in body["debts"]] == ["card"]


# highest APR first (avalanche order), unknown APR last, bigger balance first on a tie
def test_debts_sorted_by_apr_then_balance(client, db):
    seed(db, dev_user_id(db), [
        acct("A", "credit", 100, liability=credit(15.0, 100)),
        acct("B", "credit", 100, liability=credit(24.99, 100)),
        acct("C", "credit", 900, liability=credit(None, 900)),
        acct("D", "credit", 500, liability=credit(15.0, 500)),
    ])

    body = client.get("/dashboard").json()

    assert [d["name"] for d in body["debts"]] == ["B", "D", "A", "C"]


# assets, then debts, then everything else; by name within each group
def test_accounts_ordered_assets_debts_other(client, db):
    seed(db, dev_user_id(db), [
        acct("zeta", "depository", 1),
        acct("alpha", "credit", 1),
        acct("mid", "other", 1),
        acct("beta", "investment", 1),
    ])

    body = client.get("/dashboard").json()

    assert [a["name"] for a in body["accounts"]] == ["beta", "zeta", "alpha", "mid"]


# every row is scoped by user_id (architecture section 4.2)
def test_other_users_data_hidden(client, db):
    seed(db, dev_user_id(db), [acct("mine", "depository", 100)])
    seed(db, "someone-else", [acct("theirs", "depository", 999),
                              acct("their card", "credit", 50, liability=credit(30.0, 50))],
         email="else@example.com")

    body = client.get("/dashboard").json()

    assert [a["name"] for a in body["accounts"]] == ["mine"]
    assert body["debts"] == []
    assert body["assets_cents"] == 100


def test_institution_name_from_item(client, db):
    seed(db, dev_user_id(db), [acct("checking", "depository", 100)], institution="First Platypus Bank")

    body = client.get("/dashboard").json()

    assert body["accounts"][0]["institution_name"] == "First Platypus Bank"


# the exact debt shape the frontend's Debt type mirrors
def test_debt_fields(client, db):
    [card_id] = seed(db, dev_user_id(db), [
        acct("Plaid Credit Card", "credit", 41000, mask="3333",
             liability=credit(15.24, 41000, min=2000, due=date(2026, 11, 15))),
    ])

    body = client.get("/dashboard").json()

    assert body["debts"] == [{
        "account_id": card_id, "name": "Plaid Credit Card", "mask": "3333", "kind": "credit",
        "apr": 15.24, "balance_cents": 41000, "min_payment_cents": 2000, "next_due_date": "2026-11-15",
    }]


# an item whose first sync never finished stays visible (with Retry sync) across page refreshes;
# otherwise the user would only see "Connect a bank" and link the same bank again
def test_unsynced_items_listed(client, db):
    uid = dev_user_id(db)
    seed(db, uid, [], institution="First Platypus Bank", cursor=None)
    seed(db, uid, [acct("checking", "depository", 100)], institution="Synced Bank")
    seed(db, "someone-else", [], institution="Their Bank", email="else@example.com", cursor=None)

    body = client.get("/dashboard").json()

    assert [i["institution_name"] for i in body["unsynced_items"]] == ["First Platypus Bank"]
    assert set(body["unsynced_items"][0]) == {"id", "institution_name", "syncing"}
    assert body["unsynced_items"][0]["syncing"] is False


def test_debt_balance_uses_current_account_not_liability_snapshot(client, db):
    seed(db, dev_user_id(db), [
        acct("Card", "credit", 12000, liability=credit(20, 99000)),
        acct("Unknown card", "credit", None, liability=credit(10, 88888)),
    ])
    body = client.get("/dashboard").json()
    assert body["debts_cents"] == 12000
    assert [(d["name"], d["balance_cents"]) for d in body["debts"]] == [
        ("Card", 12000), ("Unknown card", None),
    ]


def test_account_groups_include_legacy_brokerage_and_sort_case_insensitively(client, db):
    seed(db, dev_user_id(db), [acct("Zeta", "depository", 100), acct("ally", "brokerage", 200),
                              acct("Card", "credit", 50), acct("Other", "other", 999)])
    body = client.get("/dashboard").json()
    assert body["assets_cents"] == 300
    assert [(a["name"], a["group"]) for a in body["accounts"]] == [
        ("ally", "asset"), ("Zeta", "asset"), ("Card", "debt"), ("Other", "other"),
    ]
