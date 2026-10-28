from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.budget_service import initialize
from app.budget_service.initialize import initialize_budget, load_txns
from app.db.models import Account, BudgetLine, BudgetPeriod
from tests.factories import add_account, add_txn, add_user


def period(user_id="u1", start=date(2026, 10, 1)):
    return BudgetPeriod(user_id=user_id, start_date=start, end_date=date(2026, 10, 31))


def test_one_period_per_user_month(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        s.add_all([period(), period()])
        with pytest.raises(IntegrityError):
            s.commit()


def test_one_line_per_category(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        p = period()
        s.add(p)
        s.flush()
        s.add_all([BudgetLine(period_id=p.id, category="FOOD_AND_DRINK", kind="flexible", limit_cents=1,
                              source="init") for _ in range(2)])
        with pytest.raises(IntegrityError):
            s.commit()


def test_period_defaults(db):
    with db() as s:
        add_user(s, "u1", "u1@x")
        p = period()
        s.add(p)
        s.flush()
        s.add(BudgetLine(period_id=p.id, category="TRAVEL", kind="flexible", limit_cents=100, source="init"))
        s.commit()
        assert p.status == "open" and p.fit_json is None
        assert p.lines[0].spent_cents == 0 and p.lines[0].last_pace is None


# --- initialize_budget ---

TODAY = date(2026, 10, 15)    # window: Jul, Aug, Sep 2026


def user(s, user_id="u1"):
    item = add_user(s, user_id, f"{user_id}@x")
    return add_account(s, item, "Checking")


def history(s, acct, travel_merchant="Delta"):
    for m, food in ((7, 40000), (8, 45000), (9, 50000)):
        add_txn(s, acct, food, "FOOD_AND_DRINK", day=date(2026, m, 10))
        add_txn(s, acct, 150000, "RENT_AND_UTILITIES", merchant="Landlord", day=date(2026, m, 1))
    add_txn(s, acct, 20000, "TRAVEL", day=date(2026, 7, 12))
    add_txn(s, acct, 120000, "TRAVEL", merchant=travel_merchant, day=date(2026, 8, 10))
    add_txn(s, acct, 70000, "TRAVEL", merchant=travel_merchant, day=date(2026, 8, 11))
    add_txn(s, acct, 25000, "TRAVEL", day=date(2026, 9, 12))


def counts(db):
    with db() as s:
        return s.query(BudgetPeriod).count(), s.query(BudgetLine).count()


def test_initialize_creates_period_and_lines(db):
    with db() as s:
        history(s, user(s))
        s.commit()
        p = initialize_budget(s, "u1", TODAY)
        assert (p.start_date, p.end_date, p.status) == (date(2026, 10, 1), date(2026, 10, 31), "open")
        pid = p.id
    # read back in a fresh session, so fit_json has really been through json
    with db() as s:
        p = s.get(BudgetPeriod, pid)
        assert p.fit_json == {"window": ["2026-07-01", "2026-08-01", "2026-09-01"], "skipped": []}
        lines = {ln.category: ln for ln in p.lines}
        assert {c: (ln.kind, ln.limit_cents) for c, ln in lines.items()} == {
            "FOOD_AND_DRINK": ("flexible", 47250), "RENT_AND_UTILITIES": ("fixed", 150000),
            "TRAVEL": ("flexible", 26250)}
        assert all(ln.source == "init" and ln.spent_cents == 0 and ln.last_pace is None for ln in p.lines)
        assert lines["TRAVEL"].fit_json == {
            "monthly_totals": [20000, 190000, 25000],
            "outliers": [{"month": "2026-08-01", "total_cents": 190000, "median_cents": 25000,
                          "top_txns": [{"date": "2026-08-10", "amount_cents": 120000, "merchant": "Delta"},
                                       {"date": "2026-08-11", "amount_cents": 70000, "merchant": "Delta"}]}]}
        assert lines["RENT_AND_UTILITIES"].fit_json == {"monthly_totals": [150000, 150000, 150000], "outliers": []}


def test_skipped_recorded(db):
    with db() as s:
        acct = user(s)
        history(s, acct)
        add_txn(s, acct, 30000, "GENERAL_MERCHANDISE", day=date(2026, 8, 20))
        s.commit()
        p = initialize_budget(s, "u1", TODAY)
        assert p.fit_json["skipped"] == ["GENERAL_MERCHANDISE"]
        assert "GENERAL_MERCHANDISE" not in {ln.category for ln in p.lines}


def test_load_txns_mapping(db):
    start, end = date(2026, 7, 1), date(2026, 9, 30)
    with db() as s:
        acct = user(s)
        other = user(s, "u2")
        add_txn(s, acct, 100, "FOOD_AND_DRINK", day=date(2026, 8, 1), removed=True)
        add_txn(s, acct, 200, "FOOD_AND_DRINK", merchant="Air", day=date(2026, 8, 2), our_category="TRAVEL",
                detailed="FOOD_AND_DRINK_COFFEE")
        add_txn(s, acct, 300, None, day=date(2026, 8, 3))
        add_txn(s, other, 400, "FOOD_AND_DRINK", day=date(2026, 8, 4))
        add_txn(s, acct, 500, "FOOD_AND_DRINK", day=start)
        add_txn(s, acct, 600, "FOOD_AND_DRINK", day=end)
        add_txn(s, acct, 700, "FOOD_AND_DRINK", day=date(2026, 10, 1))
        s.commit()
        got = sorted(load_txns(s, "u1", start, end), key=lambda t: t.amount_cents)
    assert [t.amount_cents for t in got] == [200, 300, 500, 600]
    assert (got[0].category, got[0].detailed, got[0].merchant) == ("TRAVEL", "FOOD_AND_DRINK_COFFEE", "Air")
    assert got[1].category == "UNCATEGORIZED"
    assert (got[2].date, got[3].date) == (start, end)


def test_current_month_ignored(db):
    with db() as s:
        acct = user(s)
        for m in (5, 6, 7):
            add_txn(s, acct, 10000, "FOOD_AND_DRINK", day=date(2026, m, 10))
        add_txn(s, acct, 500000, "FOOD_AND_DRINK", day=date(2026, 8, 1))
        s.commit()
        p = initialize_budget(s, "u1", date(2026, 8, 1))
        assert [(ln.category, ln.limit_cents) for ln in p.lines] == [("FOOD_AND_DRINK", 10500)]


def test_second_call_returns_same_period(db):
    with db() as s:
        history(s, user(s))
        s.commit()
        first = initialize_budget(s, "u1", TODAY).id
        assert initialize_budget(s, "u1", TODAY).id == first
    assert counts(db) == (1, 3)


def test_existing_older_period_returned(db):
    with db() as s:
        history(s, user(s))
        s.add(BudgetPeriod(user_id="u1", start_date=date(2026, 9, 1), end_date=date(2026, 9, 30), status="closed"))
        s.commit()
        assert initialize_budget(s, "u1", TODAY).start_date == date(2026, 9, 1)
    assert counts(db) == (1, 0)


def test_no_history_returns_none(db):
    with db() as s:
        add_txn(s, user(s), 5000, "FOOD_AND_DRINK", day=date(2026, 10, 3))
        s.commit()
        assert initialize_budget(s, "u1", TODAY) is None
    assert counts(db) == (0, 0)


def test_only_excluded_history_returns_none(db):
    with db() as s:
        acct = user(s)
        for m in (7, 8, 9):
            add_txn(s, acct, -400000, "INCOME", day=date(2026, m, 15))
            add_txn(s, acct, 50000, "TRANSFER_OUT", day=date(2026, m, 16))
        s.commit()
        assert initialize_budget(s, "u1", TODAY) is None
    assert counts(db) == (0, 0)


def test_all_skipped_returns_none(db):
    with db() as s:
        add_txn(s, user(s), 30000, "GENERAL_MERCHANDISE", day=date(2026, 8, 20))
        add_txn(s, s.query(Account).one(), 1000, "INCOME", day=date(2026, 7, 2))
        s.commit()
        assert initialize_budget(s, "u1", TODAY) is None
    assert counts(db) == (0, 0)


def test_outlier_without_merchant(db):
    with db() as s:
        history(s, user(s), travel_merchant=None)
        s.commit()
        p = initialize_budget(s, "u1", TODAY)
        travel = next(ln for ln in p.lines if ln.category == "TRAVEL")
        assert travel.fit_json["outliers"][0]["top_txns"][0]["merchant"] is None


def test_race_returns_existing_period(db, monkeypatch):
    with db() as s:
        history(s, user(s))
        s.add(period())
        s.commit()
        existing = s.query(BudgetPeriod).one().id
        real, calls = initialize._latest_period, []

        # the first look misses the period, as if another run committed it just after we checked
        def racy(session, user_id):
            calls.append(user_id)
            return None if len(calls) == 1 else real(session, user_id)

        monkeypatch.setattr(initialize, "_latest_period", racy)
        assert initialize_budget(s, "u1", TODAY).id == existing
    assert counts(db) == (1, 0)
