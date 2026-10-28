from app.budget_engine.close import close_period
from app.budget_engine.pace import BudgetLine


def flex(cat, cents):
    return BudgetLine(cat, "flexible", cents)


def fixed(cat, cents):
    return BudgetLine(cat, "fixed", cents)


def one(line, actual=None, accepted=None):
    actuals = {} if actual is None else {line.category: actual}
    [adj] = close_period([line], actuals, accepted or {})
    return adj


def test_smoothing_within_clamp():
    up = one(flex("FOOD_AND_DRINK", 40000), 50000)
    assert up.new_cents == 43000
    assert up.source == "smoothed"
    down = one(flex("FOOD_AND_DRINK", 40000), 38000)
    assert down.new_cents == 39400
    assert down.source == "smoothed"


def test_clamp_up():
    assert one(flex("FOOD_AND_DRINK", 40000), 100000).new_cents == 46000


def test_clamp_down():
    assert one(flex("FOOD_AND_DRINK", 40000), 0).new_cents == 34000


def test_accepted_not_clamped():
    adj = one(flex("FOOD_AND_DRINK", 40000), 50000, {"FOOD_AND_DRINK": 60000})
    assert (adj.old_cents, adj.actual_cents, adj.new_cents, adj.source) == (40000, 50000, 60000, "accepted")
    rent = one(fixed("RENT_AND_UTILITIES", 150000), 155000, {"RENT_AND_UTILITIES": 140000})
    assert (rent.new_cents, rent.source) == (140000, "accepted")


def test_fixed_follows_actual():
    adj = one(fixed("RENT_AND_UTILITIES", 150000), 155000)
    assert (adj.new_cents, adj.source) == (155000, "fixed")


def test_fixed_keeps_old_when_zero():
    assert one(fixed("RENT_AND_UTILITIES", 150000), 0).new_cents == 150000
    assert one(fixed("RENT_AND_UTILITIES", 150000)).new_cents == 150000


def test_missing_actual_is_zero():
    adj = one(flex("FOOD_AND_DRINK", 40000))
    assert adj.actual_cents == 0
    assert adj.new_cents == 34000


def test_unlined_actuals_ignored():
    adjs = close_period([flex("FOOD_AND_DRINK", 40000)], {"FOOD_AND_DRINK": 40000, "TRAVEL": 90000}, {})
    assert [a.category for a in adjs] == ["FOOD_AND_DRINK"]


def test_close_rounds_half_up():
    # 0.7 × 1015 + 0.3 × 1000 = 1010.5
    assert one(flex("FOOD_AND_DRINK", 1015), 1000).new_cents == 1011


def test_order_preserved():
    lines = [flex("TRAVEL", 10000), flex("FOOD_AND_DRINK", 40000), fixed("RENT_AND_UTILITIES", 150000)]
    adjs = close_period(lines, {}, {})
    assert [a.category for a in adjs] == ["TRAVEL", "FOOD_AND_DRINK", "RENT_AND_UTILITIES"]
