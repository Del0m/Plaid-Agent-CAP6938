from app.db.models import Transaction
from eval.budget_users import PROFILES, seed_profile, transactions


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
