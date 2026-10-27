import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import Account, Liability, Transaction, User
from eval.fixtures import ScenarioError, load_scenario, load_scenarios, seed, temp_db

REPO = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 27)


def scenario_dict(id="debt-highest-apr"):
    return {
        "id": id,
        "question": "Which of my debts should I pay off first?",
        "user": {
            "accounts": [
                {"name": "Everyday Checking", "type": "depository", "subtype": "checking", "balance": 2150.00},
                {"name": "High Yield Savings", "type": "depository", "subtype": "savings", "balance": 500.00},
                {"name": "Platinum Card", "type": "credit", "balance": 3200.00,
                 "liability": {"kind": "credit", "apr": 24.99, "min_payment": 95.00, "due_in_days": 12}},
            ],
            "transactions": [
                {"account": "Everyday Checking", "days_ago": 4, "amount": 42.10,
                 "category": "FOOD_AND_DRINK", "merchant": "Chipotle"},
                {"account": "Everyday Checking", "days_ago": 6, "amount": -12.50,
                 "category": "FOOD_AND_DRINK", "merchant": "Chipotle"},
            ],
        },
        "golden_answer": "Pay the Platinum Card first.",
        "expect": {"facts": [{"text": "Platinum Card"}, {"number": 24.99, "tol": 0.01}],
                   "forbid": ["http"], "tools": ["get_debts"]},
    }


# writes <id>.json (or `name`) under tmp_path; `edit` mutates the dict first
def write(tmp_path, edit=None, name=None, raw=None):
    d = scenario_dict()
    if edit:
        edit(d)
    path = tmp_path / (name or f"{d['id']}.json")
    path.write_text(raw if raw is not None else json.dumps(d))
    return path


def rejected(path, *fragments):
    with pytest.raises(ScenarioError) as err:
        load_scenario(path)
    for f in (path.name, *fragments):
        assert f in str(err.value)


def test_seed_creates_rows(tmp_path):
    scenario = load_scenario(write(tmp_path))
    with temp_db() as Session, Session() as s:
        seed(s, scenario, TODAY)
        accts = {a.name: a for a in s.scalars(select(Account))}
        liab = s.scalar(select(Liability))
        txns = sorted(s.scalars(select(Transaction)), key=lambda t: t.amount_cents)

    assert accts["Platinum Card"].current_balance_cents == 320000
    assert accts["High Yield Savings"].subtype == "savings"
    assert (liab.apr, liab.min_payment_cents, liab.next_due_date) == (24.99, 9500, TODAY + timedelta(days=12))
    assert liab.account_id == accts["Platinum Card"].id
    assert [(t.amount_cents, t.date) for t in txns] == [(-1250, TODAY - timedelta(days=6)),
                                                       (4210, TODAY - timedelta(days=4))]
    assert txns[1].pf_category_primary == "FOOD_AND_DRINK" and txns[1].merchant_name == "Chipotle"


def test_seed_user_is_scenario_scoped(tmp_path):
    scenario = load_scenario(write(tmp_path))
    with temp_db() as Session, Session() as s:
        uid = seed(s, scenario, TODAY)
        owners = {a.user_id for a in s.scalars(select(Account))}
        users = [u.id for u in s.scalars(select(User))]

    assert uid == "eval-debt-highest-apr"
    assert owners == {uid} and users == [uid]


def test_unknown_category_rejected(tmp_path):
    rejected(write(tmp_path, lambda d: d["user"]["transactions"][0].update(category="GROCERIES")), "category")


def test_unknown_account_rejected(tmp_path):
    rejected(write(tmp_path, lambda d: d["user"]["transactions"][0].update(account="Nope")), "account")


def test_negative_days_ago_rejected(tmp_path):
    rejected(write(tmp_path, lambda d: d["user"]["transactions"][0].update(days_ago=-1)), "days_ago")


def test_empty_facts_rejected(tmp_path):
    rejected(write(tmp_path, lambda d: d["expect"].update(facts=[])), "facts")


def test_missing_question_rejected(tmp_path):
    rejected(write(tmp_path, lambda d: d.pop("question")), "question")


def test_bad_json_rejected(tmp_path):
    rejected(write(tmp_path, raw="{not json"), "json")


@pytest.mark.parametrize("fact", [{"text": "a", "number": 1}, {}])
def test_fact_needs_exactly_one_of_text_or_number(tmp_path, fact):
    rejected(write(tmp_path, lambda d: d["expect"].update(facts=[fact])), "facts")


def test_id_must_match_filename(tmp_path):
    rejected(write(tmp_path, name="other.json"), "id")


def test_only_unknown_id_rejected(tmp_path):
    write(tmp_path)
    with pytest.raises(ScenarioError, match="no scenario"):
        load_scenarios(tmp_path, only="nope")


@pytest.mark.xfail(strict=True, reason="the five scenario files arrive in Task 6")
def test_committed_scenarios_load():
    assert len(load_scenarios(REPO / "eval" / "scenarios")) == 5
