import json
from datetime import date, timedelta

import pytest

from tests.factories import add_account, add_txn, add_user
from app.privacy import gateway as gw
from app.privacy.gateway import ACCOUNT_FIELDS, DEBT_FIELDS, TXN_FIELDS, PrivacyGateway

TODAY = date(2026, 10, 21)
PII = ["Jane", "Doe", "jane.doe@example.com", "4321", "acc-secret-1", "item-secret-1", "u-jane"]


# every test runs on a fixed "today" so the default 30-day spending window is predictable
@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(gw, "_today", lambda: TODAY)


# the spec's Jane Doe: PII in the email, the account name, the mask, plaid ids and a Zelle merchant
def seed_pii_user(db) -> str:
    with db() as s:
        item = add_user(s, "u-jane", "jane.doe@example.com")
        item.plaid_item_id = "item-secret-1"
        checking = add_account(s, item, "Jane Doe's Checking ••4321", balance=12345, mask="4321",
                               plaid_id="acc-secret-1")
        add_account(s, item, "Plaid Credit Card", type_="credit", balance=41000,
                    liability={"kind": "credit", "apr": 22.0, "min_payment_cents": 2000})
        add_txn(s, checking, 5000, "TRANSFER_OUT", merchant="Zelle to Jane Doe")
        add_txn(s, checking, 650, "FOOD_AND_DRINK", merchant="Starbucks")
        s.commit()
    return "u-jane"


def data(prompt):
    return json.loads(prompt)["data"]


def audit_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# the ticket's done-when: no name, email or account number in any gateway-built prompt
def test_no_pii_in_any_prompt(db, tmp_path):
    uid = seed_pii_user(db)
    with db() as s:
        g = PrivacyGateway(s, uid, audit_path=tmp_path / "audit.jsonl")
        prompts = [g.build_context("accounts"), g.build_context("debts"), g.build_context("spending"),
                   g.build_context("spending", category="TRANSFER_OUT")]

    for prompt in prompts:
        for secret in PII:
            assert secret not in prompt, f"{secret!r} leaked: {prompt}"


# rows are built field by field, so a new column can't leak by accident
def test_rows_have_only_allowlisted_keys(db, tmp_path):
    uid = seed_pii_user(db)
    with db() as s:
        g = PrivacyGateway(s, uid, audit_path=tmp_path / "audit.jsonl")
        accounts = data(g.build_context("accounts"))
        debts = data(g.build_context("debts"))
        txns = data(g.build_context("spending", category="FOOD_AND_DRINK"))["transactions"]

    assert all(set(r) == set(ACCOUNT_FIELDS) for r in accounts)
    assert all(set(r) == set(DEBT_FIELDS) for r in debts)
    assert all(set(r) == set(TXN_FIELDS) for r in txns)


def test_money_in_dollars(db, tmp_path):
    uid = seed_pii_user(db)
    with db() as s:
        accounts = data(PrivacyGateway(s, uid, audit_path=tmp_path / "a.jsonl").build_context("accounts"))

    assert 123.45 in [a["balance"] for a in accounts]


# one pseudonym per gateway (= per chat session), stable across its prompts
def test_pseudonym_format_and_lifetime(db, tmp_path, monkeypatch):
    uid = seed_pii_user(db)
    hexes = iter(["7f3a", "b2c1"])
    monkeypatch.setattr(gw.secrets, "token_hex", lambda n: next(hexes))
    with db() as s:
        first = PrivacyGateway(s, uid, audit_path=tmp_path / "a.jsonl")
        second = PrivacyGateway(s, uid, audit_path=tmp_path / "a.jsonl")
        users = {json.loads(first.build_context(p))["user"] for p in ("accounts", "debts")}

    assert first.pseudonym == "user_7f3a"
    assert users == {"user_7f3a"}
    assert second.pseudonym == "user_b2c1"


# a chat session spans many requests (each with its own db session); passing the pseudonym
# back in keeps the model seeing one consistent user
def test_pseudonym_can_be_reused(db, tmp_path):
    uid = seed_pii_user(db)
    with db() as s:
        g = PrivacyGateway(s, uid, audit_path=tmp_path / "a.jsonl", pseudonym="user_abcd")
        prompt = g.build_context("accounts")

    assert g.pseudonym == "user_abcd"
    assert json.loads(prompt)["user"] == "user_abcd"


def test_other_users_data_hidden(db, tmp_path):
    uid = seed_pii_user(db)
    with db() as s:
        other = add_user(s, "u-other", "other@example.com")
        add_account(s, other, "Theirs", type_="credit", liability={"kind": "credit", "apr": 30.0})
        s.commit()
        g = PrivacyGateway(s, uid, audit_path=tmp_path / "a.jsonl")
        prompts = g.build_context("accounts") + g.build_context("debts")

    assert "Theirs" not in prompts


# same avalanche order as GET /dashboard: highest APR first, unknown APR last
def test_debts_sorted_like_dashboard(db, tmp_path):
    with db() as s:
        item = add_user(s, "u-1", "a@example.com")
        for name, apr in [("A", 15.0), ("B", 24.99), ("C", None)]:
            add_account(s, item, name, type_="credit", liability={"kind": "credit", "apr": apr})
        s.commit()
        debts = data(PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl").build_context("debts"))

    assert [d["apr"] for d in debts] == [24.99, 15.0, None]


# spending = positive amounts (money out) in range, not refunds, not removed rows
def test_spending_totals_positive_only_in_range(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u-1", "a@example.com"), "Checking")
        add_txn(s, acct, 650, "FOOD_AND_DRINK")
        add_txn(s, acct, -200, "FOOD_AND_DRINK")                       # refund
        add_txn(s, acct, 100, "FOOD_AND_DRINK", day=date(2026, 9, 1))  # before start
        add_txn(s, acct, 999, "FOOD_AND_DRINK", removed=True)
        s.commit()
        g = PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl")
        totals = data(g.build_context("spending", start=date(2026, 10, 1), end=TODAY))

    assert totals == {"FOOD_AND_DRINK": 6.5}


# minimization: a category question gets that category's rows only, newest first, capped
def test_spending_category_lists_capped_newest_first(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u-1", "a@example.com"), "Checking")
        for i in range(60):
            add_txn(s, acct, 100, "FOOD_AND_DRINK", day=TODAY - timedelta(days=i % 25))
        add_txn(s, acct, 5000, "TRANSFER_OUT")
        s.commit()
        out = data(PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl")
                   .build_context("spending", category="FOOD_AND_DRINK"))

    assert out["category"] == "FOOD_AND_DRINK"
    assert len(out["transactions"]) == 50
    assert out["transactions"][0]["date"] == TODAY.isoformat()
    assert "TRANSFER_OUT" not in json.dumps(out)


# models lowercase enum values; and "no rows in that category" must read as $0, not as nothing
def test_spending_category_case_insensitive_and_zero(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u-1", "a@example.com"), "Checking")
        add_txn(s, acct, 650, "FOOD_AND_DRINK")
        s.commit()
        g = PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl")
        dining = data(g.build_context("spending", category="food_and_drink"))
        travel = data(g.build_context("spending", category="TRAVEL"))

    assert (dining["category"], dining["total"]) == ("FOOD_AND_DRINK", 6.5)
    assert travel == {"category": "TRAVEL", "total": 0.0, "transactions": []}


def test_spending_defaults_to_last_30_days(db, tmp_path):
    with db() as s:
        acct = add_account(s, add_user(s, "u-1", "a@example.com"), "Checking")
        add_txn(s, acct, 100, "IN_WINDOW", day=date(2026, 9, 21))
        add_txn(s, acct, 100, "TOO_OLD", day=date(2026, 9, 20))
        s.commit()
        totals = data(PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl").build_context("spending"))

    assert totals == {"IN_WINDOW": 1.0}


def test_empty_user_builds_empty_context(db, tmp_path):
    with db() as s:
        add_user(s, "u-1", "a@example.com")
        s.commit()
        g = PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl")
        results = [data(g.build_context(p)) for p in ("accounts", "debts", "spending")]

    assert results == [[], [], {}]
    assert len(audit_lines(tmp_path / "a.jsonl")) == 3


# the audit log is the evidence of what left the system: exact prompt, pseudonym, never the user id
def test_audit_line_per_call(db, tmp_path):
    uid = seed_pii_user(db)
    path = tmp_path / "a.jsonl"
    with db() as s:
        g = PrivacyGateway(s, uid, audit_path=path)
        p1 = g.build_context("accounts")
        p2 = g.build_context("spending", category="FOOD_AND_DRINK")

    lines = audit_lines(path)
    assert [l["prompt"] for l in lines] == [p1, p2]
    assert {l["pseudonym"] for l in lines} == {g.pseudonym}
    assert [l["purpose"] for l in lines] == ["accounts", "spending"]
    assert lines[1]["params"] == {"category": "FOOD_AND_DRINK"}
    assert uid not in path.read_text()


def test_audit_creates_missing_directory(db, tmp_path):
    path = tmp_path / "new" / "dir" / "a.jsonl"
    with db() as s:
        add_user(s, "u-1", "a@example.com")
        s.commit()
        PrivacyGateway(s, "u-1", audit_path=path).build_context("accounts")

    assert path.exists()


def test_unknown_purpose_raises(db, tmp_path):
    with db() as s:
        with pytest.raises(ValueError):
            PrivacyGateway(s, "u-1", audit_path=tmp_path / "a.jsonl").build_context("transactions")
