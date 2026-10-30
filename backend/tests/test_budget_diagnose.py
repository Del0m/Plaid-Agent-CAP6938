from datetime import date

from app.budget_engine.diagnose import overspend_facts, projected_cents, proposed_limit_cents
from app.budget_engine.pace import PaceResult
from app.budget_engine.txns import Txn

OCT = date(2026, 10, 1)


def flag(cat, spent, limit, reason="over_limit", kind="flexible", pace=None):
    return PaceResult(category=cat, kind=kind, limit_cents=limit, spent_cents=spent, days_elapsed=20,
                      days_in_period=31, pace=pace, flagged=True, reason=reason)


def t(m, d, cents, cat="GENERAL_MERCHANDISE", merchant=None):
    return Txn(date=date(2026, m, d), amount_cents=cents, category=cat, merchant=merchant)


def merchant(facts, name):
    return next(mf for mf in facts.merchants if mf.merchant == name)


def test_one_time_largest_and_new_merchant():
    period = [t(10, 12, 60000, merchant="ElectroMart"), t(10, 5, 3500, merchant="Corner Shop")]
    history = [t(m, 4, 3500, merchant="Corner Shop") for m in (7, 8, 9)]
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 63500, 15750), None, period, history, OCT)
    assert f.largest.amount_cents == 60000
    assert f.largest_share == 60000 / 63500
    em, cs = merchant(f, "ElectroMart"), merchant(f, "Corner Shop")
    assert (em.prior_months, em.typical_cents, em.period_cents, em.charges) == (0, None, 60000, 1)
    assert (cs.prior_months, cs.typical_cents) == (3, 3500)


def test_recurring_gym_seen_once_before():
    cat = "PERSONAL_CARE"
    period = [t(10, 3, 9000, cat, "FitClub Gym"), t(10, 20, 4000, cat, "Barber")]
    history = [t(m, 20, 4000, cat, "Barber") for m in (7, 8, 9)] + [t(9, 3, 9000, cat, "FitClub Gym")]
    f = overspend_facts(flag(cat, 13000, 4200), None, period, history, OCT)
    gym = merchant(f, "FitClub Gym")
    assert (gym.prior_months, gym.typical_cents) == (1, 9000)
    assert [mf.merchant for mf in f.merchants] == ["FitClub Gym", "Barber"]


def test_history_from_fit_json():
    fit = {"monthly_totals": [15000, 14800, 15200],
           "outliers": [{"month": "2026-08-01", "total_cents": 1, "median_cents": 1, "top_txns": []}]}
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 20000, 15750), fit, [t(10, 2, 20000)], [], OCT)
    assert f.monthly_totals == (15000, 14800, 15200)
    assert f.outlier_months == (date(2026, 8, 1),)
    assert f.month == OCT
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 20000, 15750), None, [t(10, 2, 20000)], [], OCT)
    assert (f.monthly_totals, f.outlier_months) == ((), ())


def test_other_categories_and_income_ignored():
    period = [t(10, 12, 30000, merchant="Shop"), t(10, 13, 90000, "FOOD_AND_DRINK", "Diner"),
              t(10, 15, -420000, "INCOME", "Employer")]
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 30000, 15750), None, period, [], OCT)
    assert [mf.merchant for mf in f.merchants] == ["Shop"]
    assert f.largest.merchant == "Shop"


def test_merchants_capped_and_unnamed_grouped():
    period = [t(10, 2, 1000 * (i + 1), merchant=f"m{i}") for i in range(7)]
    period += [t(10, 3, 5000), t(10, 4, 5000)]
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 38000, 15750), None, period, [], OCT)
    assert len(f.merchants) == 5
    assert f.merchants[0].merchant is None and f.merchants[0].charges == 2
    assert [mf.period_cents for mf in f.merchants] == [10000, 7000, 6000, 5000, 4000]


def test_largest_share_capped():
    period = [t(10, 2, 10000, merchant="Shop"), t(10, 5, -4000, merchant="Shop")]
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 6000, 5000), None, period, [], OCT)
    assert f.largest_share == 1.0
    assert merchant(f, "Shop").period_cents == 6000


def test_projected_cents():
    assert projected_cents(10000, 20, 31) == 15500
    assert projected_cents(1001, 2, 31) == 15516       # 15515.5 rounds half up


def test_proposed_limit():
    f = overspend_facts(flag("TRAVEL", 10001, 5000), None, [t(10, 2, 10001, "TRAVEL")], [], OCT)
    assert f.projected_cents == 15502
    assert proposed_limit_cents(f) == 16000
    f = overspend_facts(flag("TRAVEL", 10000, 5000), None, [t(10, 2, 10000, "TRAVEL")], [], OCT)
    assert proposed_limit_cents(f) == 15500             # already a multiple of $5
    rent = "RENT_AND_UTILITIES"
    f = overspend_facts(flag(rent, 13000, 12000, kind="fixed"), None, [t(10, 1, 13000, rent)], [], OCT)
    assert proposed_limit_cents(f) == 13000


def test_largest_vs_typical():
    cat = GM = "GENERAL_MERCHANDISE"
    period = [t(10, 8, 12000, merchant="MegaMart"), t(10, 12, 52000, merchant="MegaMart")]
    history = [t(m, 8, 12000, merchant="MegaMart") for m in (7, 8, 9)]
    f = overspend_facts(flag(GM, 64000, 12600), None, period, history, OCT)
    assert round(f.largest_vs_typical, 2) == 4.33
    f = overspend_facts(flag(GM, 60000, 15750), None, [t(10, 12, 60000, merchant="ElectroMart")], [], OCT)
    assert f.largest_vs_typical is None


def test_recurring_proposal_adds_new_merchants_typical():
    # the gym started last month: next month needs the old limit plus a gym, not a straight-line projection
    cat = "PERSONAL_CARE"
    period = [t(10, 3, 9000, cat, "FitClub Gym"), t(10, 20, 4000, cat, "Barber")]
    history = [t(m, 20, 4000, cat, "Barber") for m in (7, 8, 9)] + [t(9, 3, 9000, cat, "FitClub Gym")]
    f = overspend_facts(flag(cat, 13000, 4200), None, period, history, OCT)
    assert proposed_limit_cents(f) == 13500      # 4200 + 9000, up to $5


def test_merchant_pattern():
    # a small model misread "months_seen_before: 1" as new, so the code names the pattern outright
    cat = "PERSONAL_CARE"
    period = [t(10, 3, 9000, cat, "FitClub Gym"), t(10, 20, 4000, cat, "Barber"), t(10, 9, 2500, cat, "Spa")]
    history = [t(m, 20, 4000, cat, "Barber") for m in (7, 8, 9)] + [t(9, 3, 9000, cat, "FitClub Gym")]
    f = overspend_facts(flag(cat, 15500, 4200), None, period, history, OCT)
    assert {m.merchant: m.pattern for m in f.merchants} == {
        "FitClub Gym": "started_recently", "Barber": "regular", "Spa": "new"}


# review on #12: with only September stored, a grocer seen in September is regular, not started_recently
def test_short_history_merchant_is_regular():
    cat = "FOOD_AND_DRINK"
    period = [t(10, 5, 30000, cat, "Grocer")]
    history = [t(9, d, 10000, cat, "Grocer") for d in (3, 10, 17)]
    f = overspend_facts(flag(cat, 30000, 31500, reason="pace", pace=1.5), None, period, history, OCT)
    assert merchant(f, "Grocer").pattern == "regular"
    assert proposed_limit_cents(f) == -(-f.projected_cents // 500) * 500      # no new merchant: the projection


# review on #12: a gym in 2 of the 3 history months is already in the limit's median, so it's added once
def test_recurring_merchant_already_in_the_median_not_added_twice():
    cat = "PERSONAL_CARE"
    period = [t(10, 3, 9000, cat, "FitClub Gym"), t(10, 20, 4000, cat, "Barber")]
    history = [t(m, 20, 4000, cat, "Barber") for m in (7, 8, 9)] + [t(m, 3, 9000, cat, "FitClub Gym") for m in (8, 9)]
    f = overspend_facts(flag(cat, 13000, 13650), None, period, history, OCT)
    assert merchant(f, "FitClub Gym").pattern == "started_recently"
    assert proposed_limit_cents(f) == 13500      # the barber-only month ($42 with the 5%) plus one gym, up to $5


# review on #12: a shop seen once in July and again in October didn't start recently; it's occasional
def test_merchant_seen_long_ago_is_occasional():
    period = [t(10, 12, 50000, merchant="Camera Shop")]
    history = [t(7, 2, 50000, merchant="Camera Shop")] + [t(m, 4, 3500, merchant="Corner") for m in (7, 8, 9)]
    f = overspend_facts(flag("GENERAL_MERCHANDISE", 50000, 3675), None, period, history, OCT)
    assert merchant(f, "Camera Shop").pattern == "occasional"


# review on #12: a fixed line's proposal is rounded up to $5 too
def test_fixed_proposal_rounded_up():
    rent = "RENT_AND_UTILITIES"
    f = overspend_facts(flag(rent, 152337, 150000, kind="fixed"), None, [t(10, 1, 152337, rent)], [], OCT)
    assert proposed_limit_cents(f) == 152500
