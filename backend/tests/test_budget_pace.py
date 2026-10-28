from datetime import date

import pytest

from app.budget_engine.pace import BudgetLine, evaluate
from app.budget_engine.txns import Txn


def flex(cat, cents):
    return BudgetLine(cat, "flexible", cents)


def fixed(cat, cents):
    return BudgetLine(cat, "fixed", cents)


def t(y, m, d, cents, cat="FOOD_AND_DRINK"):
    return Txn(date=date(y, m, d), amount_cents=cents, category=cat)


def by_cat(results):
    return {r.category: r for r in results}


def one(lines, txns, today):
    [result] = evaluate(lines, txns, today)
    return result


def test_mid_month_pace_flags():
    r = one([flex("FOOD_AND_DRINK", 30000)], [t(2026, 10, 3, 12000), t(2026, 10, 14, 8000)], date(2026, 10, 15))
    assert r.days_elapsed == 15
    assert r.days_in_period == 31
    assert r.spent_cents == 20000
    assert r.pace == pytest.approx(1.3778, abs=1e-4)
    assert r.flagged
    assert r.reason == "pace"


def test_under_pace_not_flagged():
    r = one([flex("FOOD_AND_DRINK", 30000)], [t(2026, 10, 3, 10000)], date(2026, 10, 15))
    assert r.flagged is False
    assert r.reason is None


def test_early_month_never_flags_on_pace():
    for day in range(1, 5):
        r = one([flex("FOOD_AND_DRINK", 30000)], [t(2026, 10, 1, 25000)], date(2026, 10, day))
        assert r.pace is not None and r.pace > 1.10
        assert r.flagged is False, day


def test_early_month_over_limit_flags():
    r = one([flex("FOOD_AND_DRINK", 30000)], [t(2026, 10, 1, 35000)], date(2026, 10, 2))
    assert r.flagged
    assert r.reason == "over_limit"


def test_pace_threshold_inclusive():
    # nov has 30 days: day 5 is 1/6 of the month, so $100 of a $600 limit is pace 1.00
    r = one([flex("FOOD_AND_DRINK", 60000)], [t(2026, 11, 2, 11000)], date(2026, 11, 5))
    assert r.pace == 1.10
    assert r.reason == "pace"
    r = one([flex("FOOD_AND_DRINK", 60000)], [t(2026, 11, 2, 10999)], date(2026, 11, 5))
    assert r.flagged is False


def test_fixed_rent_on_first_not_flagged():
    r = one([fixed("RENT_AND_UTILITIES", 150000)], [t(2026, 10, 1, 150000, "RENT_AND_UTILITIES")],
            date(2026, 10, 10))
    assert r.pace is None
    assert r.flagged is False
    assert r.reason is None


def test_fixed_over_limit():
    r = one([fixed("RENT_AND_UTILITIES", 150000)], [t(2026, 10, 1, 155000, "RENT_AND_UTILITIES")],
            date(2026, 10, 10))
    assert r.pace is None
    assert r.reason == "over_limit"


def test_over_limit_beats_pace():
    r = one([flex("FOOD_AND_DRINK", 30000)], [t(2026, 10, 5, 31000)], date(2026, 10, 15))
    assert r.pace > 1.10
    assert r.reason == "over_limit"


def test_unbudgeted_reported_unflagged():
    txns = [t(2026, 10, 2, 1000), t(2026, 10, 3, 5000, "TRAVEL"), t(2026, 10, 4, 2000, "ENTERTAINMENT")]
    results = evaluate([flex("FOOD_AND_DRINK", 30000)], txns, date(2026, 10, 15))
    assert [r.category for r in results] == ["FOOD_AND_DRINK", "ENTERTAINMENT", "TRAVEL"]
    for r in results[1:]:
        assert r.kind == "unbudgeted"
        assert r.limit_cents == 0
        assert r.pace is None
        assert r.flagged is False
    assert by_cat(results)["TRAVEL"].spent_cents == 5000


def test_refund_only_not_unbudgeted():
    results = evaluate([], [t(2026, 10, 2, -3000, "GENERAL_MERCHANDISE")], date(2026, 10, 15))
    assert results == []


def test_month_lengths():
    r = one([flex("FOOD_AND_DRINK", 30000)], [], date(2028, 2, 29))
    assert (r.days_elapsed, r.days_in_period) == (29, 29)
    r = one([flex("FOOD_AND_DRINK", 30000)], [], date(2027, 1, 31))
    assert r.days_in_period == 31


def test_zero_limit():
    r = one([flex("FOOD_AND_DRINK", 0)], [], date(2026, 10, 15))
    assert r.pace is None
    assert r.flagged is False
    r = one([flex("FOOD_AND_DRINK", 0)], [t(2026, 10, 3, 100)], date(2026, 10, 15))
    assert r.pace is None
    assert r.reason == "over_limit"


def test_evaluate_ignores_other_periods():
    txns = [t(2026, 9, 30, 5000), t(2026, 10, 1, 1000), t(2026, 10, 15, 1000), t(2026, 10, 16, 9000)]
    assert one([flex("FOOD_AND_DRINK", 30000)], txns, date(2026, 10, 15)).spent_cents == 2000


def test_lines_keep_given_order_and_excluded_skipped():
    lines = [flex("TRAVEL", 10000), flex("FOOD_AND_DRINK", 30000)]
    results = evaluate(lines, [t(2026, 10, 1, -400000, "INCOME")], date(2026, 10, 15))
    assert [r.category for r in results] == ["TRAVEL", "FOOD_AND_DRINK"]
    assert all(r.spent_cents == 0 for r in results)
