from datetime import date

from app.budget_engine.fit import FitResult, fit_budget
from app.budget_engine.txns import Txn

TODAY = date(2026, 10, 15)    # window: Jul, Aug, Sep 2026


def t(y, m, d, cents, cat="FOOD_AND_DRINK", detailed=None, merchant=None):
    return Txn(date=date(y, m, d), amount_cents=cents, category=cat, detailed=detailed, merchant=merchant)


def monthly(cat, *cents, start_month=7, year=2026):
    return [t(year, start_month + i, 10, c, cat) for i, c in enumerate(cents) if c]


def line(result, cat):
    return next(ln for ln in result.lines if ln.category == cat)


NORMAL = monthly("FOOD_AND_DRINK", 40000, 45000, 50000)


def test_normal_flexible_median():
    result = fit_budget(NORMAL, TODAY)
    assert len(result.lines) == 1
    food = line(result, "FOOD_AND_DRINK")
    assert food.kind == "flexible"
    assert food.limit_cents == 47250
    assert food.monthly_totals == (40000, 45000, 50000)
    assert food.outliers == ()


def test_outlier_month_recorded():
    aug = [t(2026, 8, d, c, "TRAVEL", merchant=f"m{d}") for d, c in [(3, 10000), (5, 120000), (9, 20000),
                                                                      (20, 40000)]]
    txns = [t(2026, 7, 4, 20000, "TRAVEL"), *aug, t(2026, 9, 12, 25000, "TRAVEL")]
    travel = line(fit_budget(txns, TODAY), "TRAVEL")
    assert travel.limit_cents == 26250
    assert len(travel.outliers) == 1
    out = travel.outliers[0]
    assert out.month == date(2026, 8, 1)
    assert out.total_cents == 190000
    assert out.median_cents == 25000
    assert tuple(x.amount_cents for x in out.top_txns) == (120000, 40000, 20000)


def test_fixed_median_no_markup():
    rent = line(fit_budget(monthly("RENT_AND_UTILITIES", 150000, 150000, 150000), TODAY), "RENT_AND_UTILITIES")
    assert rent.kind == "fixed"
    assert rent.limit_cents == 150000
    assert rent.outliers == ()


RENT = "RENT_AND_UTILITIES"


def rent(result):
    return line(result, RENT)


def test_fixed_paid_early_double_month():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT), t(2026, 8, 31, 150000, RENT)]
    r = rent(fit_budget(txns, TODAY))
    assert r.monthly_totals == (150000, 300000, 0)
    assert r.limit_cents == 150000


def test_fixed_double_in_last_month():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT),
            t(2026, 9, 1, 150000, RENT), t(2026, 9, 30, 150000, RENT)]
    assert rent(fit_budget(txns, TODAY)).limit_cents == 150000


def test_fixed_new_lease_ignores_leading_zeros():
    # food in july keeps the window at jul-sep, so rent's totals start with two zeros
    txns = [t(2026, 7, 10, 5000), t(2026, 9, 1, 150000, RENT)]
    r = rent(fit_budget(txns, TODAY))
    assert r.monthly_totals == (0, 0, 150000)
    assert r.limit_cents == 150000


def test_fixed_increase_not_adopted_at_init():
    assert rent(fit_budget(monthly(RENT, 150000, 150000, 160000), TODAY)).limit_cents == 150000


def test_fixed_even_months_takes_lower():
    # first link mid-july: july has no rent, and october's rent was paid early on sep 30
    assert rent(fit_budget(monthly(RENT, 0, 150000, 300000), TODAY)).limit_cents == 150000


def test_fixed_zero_month_between():
    assert rent(fit_budget(monthly(RENT, 0, 150000, 0) + NORMAL, TODAY)).limit_cents == 150000


def test_mean_center_flexible():
    travel = line(fit_budget(monthly("TRAVEL", 20000, 190000, 25000), TODAY, center="mean"), "TRAVEL")
    assert travel.limit_cents == 82250               # 235000 / 3 × 1.05
    assert [o.month for o in travel.outliers] == [date(2026, 8, 1)]


def test_mean_center_keeps_occasional():
    result = fit_budget(monthly("SHOPPING", 0, 30000, 0) + NORMAL, TODAY, center="mean")
    assert line(result, "SHOPPING").limit_cents == 10500
    assert result.skipped == ()


def test_mean_center_leaves_fixed_alone():
    txns = [t(2026, 7, 1, 150000, RENT), t(2026, 8, 1, 150000, RENT), t(2026, 8, 31, 150000, RENT)]
    assert rent(fit_budget(txns, TODAY, center="mean")).limit_cents == 150000


def test_window_reported():
    assert fit_budget(NORMAL, TODAY).window == (date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1))
    assert fit_budget(monthly("FOOD_AND_DRINK", 100, 200, start_month=8), TODAY).window == \
        (date(2026, 8, 1), date(2026, 9, 1))
    assert fit_budget([], TODAY).window == ()
    assert fit_budget([t(2026, 10, 2, 100)], TODAY).window == ()


def test_custom_fixed_set():
    result = fit_budget(monthly("ENTERTAINMENT", 1000, 2000, 1500), TODAY, fixed=frozenset({"ENTERTAINMENT"}))
    ent = line(result, "ENTERTAINMENT")
    assert ent.kind == "fixed"
    assert ent.limit_cents == 1500


def test_refunds_netted():
    txns = [*monthly("FOOD_AND_DRINK", 40000, 45000), t(2026, 9, 3, 50000), t(2026, 9, 20, -10000)]
    assert line(fit_budget(txns, TODAY), "FOOD_AND_DRINK").monthly_totals == (40000, 45000, 40000)


def test_refund_only_category_ignored():
    txns = [*NORMAL, t(2026, 8, 2, -5000, "GENERAL_MERCHANDISE"), t(2026, 9, 2, -5000, "GENERAL_MERCHANDISE")]
    result = fit_budget(txns, TODAY)
    assert [ln.category for ln in result.lines] == ["FOOD_AND_DRINK"]
    assert result.skipped == ()


def test_exclusions_never_lines():
    txns = []
    for m in (7, 8, 9):
        txns += [t(2026, m, 1, -400000, "INCOME"), t(2026, m, 2, -50000, "TRANSFER_IN"),
                 t(2026, m, 3, 50000, "TRANSFER_OUT"),
                 t(2026, m, 4, 80000, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")]
    result = fit_budget(txns, TODAY)
    assert (result.lines, result.skipped) == ((), ())


def test_current_month_ignored():
    assert line(fit_budget([*NORMAL, t(2026, 10, 3, 100000)], TODAY), "FOOD_AND_DRINK").limit_cents == 47250


def test_short_history_two_months():
    food = line(fit_budget(monthly("FOOD_AND_DRINK", 40000, 50000, start_month=8), TODAY), "FOOD_AND_DRINK")
    assert food.monthly_totals == (40000, 50000)
    assert food.limit_cents == 47250


def test_no_complete_month_empty():
    assert fit_budget([t(2026, 10, 1, 5000), t(2026, 10, 9, 7000)], TODAY) == FitResult((), ())
    assert fit_budget([], TODAY) == FitResult((), ())


def test_occasional_category_skipped():
    result = fit_budget([*NORMAL, t(2026, 8, 14, 30000, "HOME_IMPROVEMENT")], TODAY)
    assert result.skipped == ("HOME_IMPROVEMENT",)
    assert [ln.category for ln in result.lines] == ["FOOD_AND_DRINK"]


def test_lines_and_skipped_sorted():
    txns = [*monthly("TRAVEL", 100, 100, 100), *NORMAL, *monthly("ENTERTAINMENT", 100, 100, 100),
            t(2026, 9, 1, 100, "PERSONAL_CARE"), t(2026, 9, 1, 100, "GENERAL_SERVICES")]
    result = fit_budget(txns, TODAY)
    assert [ln.category for ln in result.lines] == ["ENTERTAINMENT", "FOOD_AND_DRINK", "TRAVEL"]
    assert result.skipped == ("GENERAL_SERVICES", "PERSONAL_CARE")


def test_fit_accepts_generator():
    assert fit_budget((x for x in NORMAL), TODAY) == fit_budget(NORMAL, TODAY)


def test_fit_window_crosses_year():
    txns = monthly("FOOD_AND_DRINK", 40000, 45000, 50000, start_month=10)
    food = line(fit_budget(txns, date(2027, 1, 10)), "FOOD_AND_DRINK")
    assert food.monthly_totals == (40000, 45000, 50000)
    assert food.limit_cents == 47250
