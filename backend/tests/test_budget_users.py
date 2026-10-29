from datetime import date

from app.budget_engine.fit import fit_budget
from app.budget_engine.pace import BudgetLine, evaluate
from app.budget_engine.txns import Txn, month_start
from app.db.models import Transaction
from eval.budget_users import PLANTED, PROFILES, seed_profile, transactions


def profile(pid):
    return next(p for p in PROFILES if p.id == pid)


def test_same_seed_same_transactions():
    p = profile("steady")
    assert transactions(p) == transactions(p)


def test_vacation_june_travel():
    june = [c for d, c, cat, _ in transactions(profile("vacation")) if cat == "TRAVEL" and d.month == 6]
    assert sum(june) == 190000


def test_early_rent_doubles_june():
    rent = [(d, c) for d, c, cat, _ in transactions(profile("early-rent")) if cat == "RENT_AND_UTILITIES"]
    assert sum(c for d, c in rent if d.month == 6) == 300000
    assert not [d for d, _ in rent if d.month == 7]


def test_rising_groceries_grow():
    p = profile("rising")
    by_month = {}
    for d, c, _, merchant in transactions(p):
        if merchant == "Grocer":
            by_month.setdefault(d.month, []).append(c)
    avg = {m: sum(cs) / len(cs) for m, cs in by_month.items()}
    assert avg[5] < avg[6] < avg[7] < avg[8]


def test_every_profile_has_income_and_transfer():
    for p in PROFILES:
        cats = {cat for _, _, cat, _ in transactions(p)}
        assert {"INCOME", "TRANSFER_OUT"} <= cats


def test_transactions_stay_in_range():
    for p in PROFILES:
        days = [d for d, _, _, _ in transactions(p)]
        assert min(days) >= p.start and max(days) <= p.end


def test_profile_ids():
    assert [p.id for p in PROFILES] == ["steady", "vacation", "early-rent", "rising", "occasional"]


def test_seed_profile_writes_rows(db):
    p = profile("steady")
    with db() as s:
        uid = seed_profile(s, p)
    with db() as s:
        assert s.query(Transaction).filter_by(user_id=uid).count() == len(transactions(p))


# --- planted overspend users (TPC-19) ---

def planted(pid):
    return next(p for p in PLANTED if p.id == pid)


def test_planted_one_time_purchase():
    assert (date(2026, 10, 12), 60000, "GENERAL_MERCHANDISE", "ElectroMart") in transactions(planted("overspend-one-time"))


def test_planted_gym_starts_september():
    gym = [d for d, _, _, m in transactions(planted("overspend-recurring")) if m == "FitClub Gym"]
    assert gym == [date(2026, 9, 3), date(2026, 10, 3)]


def test_planted_december_shops():
    dec = {m for d, _, cat, m in transactions(planted("overspend-seasonal"))
           if d.month == 12 and cat == "GENERAL_MERCHANDISE" and m != "Corner Shop"}
    assert len(dec) == 8


def test_planted_flags_only_the_planted_category():
    # fit on the three months before run_date, pace on run_date: exactly one flag, the planted one
    for p in PLANTED:
        txns = [Txn(d, c, cat, merchant=m) for d, c, cat, m in transactions(p)]
        start = month_start(p.run_date)
        fit = fit_budget([t for t in txns if t.date < start], p.run_date)
        lines = [BudgetLine(ln.category, ln.kind, ln.limit_cents) for ln in fit.lines]
        flagged = [r.category for r in evaluate(lines, [t for t in txns if t.date <= p.run_date], p.run_date)
                   if r.flagged]
        assert flagged == [{"overspend-recurring": "PERSONAL_CARE"}.get(p.id, "GENERAL_MERCHANDISE")], p.id


def test_planted_carry_run_date_and_cause():
    assert {p.id: (p.run_date, p.expected_cause) for p in PLANTED} == {
        "overspend-one-time": (date(2026, 10, 20), "one_time"),
        "overspend-recurring": (date(2026, 10, 20), "recurring"),
        "overspend-seasonal": (date(2026, 12, 15), "seasonal"),
        "overspend-known-merchant": (date(2026, 10, 20), "one_time")}


def test_planted_known_merchant_big_purchase():
    mega = [(d, c) for d, c, _, m in transactions(planted("overspend-known-merchant")) if m == "MegaMart"]
    assert (date(2026, 10, 12), 52000) in mega
    assert [d for d, c in mega if c == 12000] == [date(2026, m, 8) for m in (7, 8, 9, 10)]


def test_profiles_unchanged():
    assert [p.id for p in PROFILES] == ["steady", "vacation", "early-rent", "rising", "occasional"]
    assert all(p.run_date is None for p in PROFILES)
