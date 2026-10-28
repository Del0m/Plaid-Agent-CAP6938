from datetime import date
from decimal import Decimal

import pytest

from app.budget_engine.txns import (Txn, add_months, category_totals, month_end, month_start, round_cents,
                                    spending)


def t(day, cents, cat="FOOD_AND_DRINK", detailed=None, month=10, year=2026):
    return Txn(date=date(year, month, day), amount_cents=cents, category=cat, detailed=detailed)


def test_spending_drops_non_spending():
    car = t(3, 30000, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CAR_PAYMENT")
    food = t(4, 4210)
    txns = [
        t(1, -250000, "INCOME"),
        t(2, -50000, "TRANSFER_IN"),
        car,
        t(2, 50000, "TRANSFER_OUT"),
        t(3, 80000, "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
        food,
    ]
    assert spending(txns) == [car, food]


def test_totals_net_refunds():
    txns = [t(1, 5000), t(2, 3000), t(3, -2000)]
    assert category_totals(txns, date(2026, 10, 1), date(2026, 10, 31)) == {"FOOD_AND_DRINK": 6000}


def test_totals_floor_at_zero():
    txns = [t(1, 1000, "GENERAL_MERCHANDISE"), t(2, -4000, "GENERAL_MERCHANDISE")]
    assert category_totals(txns, date(2026, 10, 1), date(2026, 10, 31)) == {"GENERAL_MERCHANDISE": 0}


def test_totals_range_inclusive():
    txns = [t(30, 100, month=9), t(1, 100), t(31, 100), t(1, 100, month=11)]
    assert category_totals(txns, date(2026, 10, 1), date(2026, 10, 31)) == {"FOOD_AND_DRINK": 200}


def test_totals_omit_absent_categories():
    assert category_totals([t(1, 100, month=9)], date(2026, 10, 1), date(2026, 10, 31)) == {}


def test_month_helpers_leap_year():
    assert month_end(date(2028, 2, 10)) == date(2028, 2, 29)
    assert month_end(date(2027, 2, 1)) == date(2027, 2, 28)
    assert month_start(date(2028, 2, 29)) == date(2028, 2, 1)


def test_add_months_crosses_year():
    assert add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert add_months(date(2027, 1, 1), -1) == date(2026, 12, 1)
    assert add_months(date(2026, 10, 1), -3) == date(2026, 7, 1)
    with pytest.raises(ValueError):
        add_months(date(2026, 10, 15), 1)


def test_round_cents_half_up():
    assert round_cents(Decimal("1010.5")) == 1011
    assert round_cents(Decimal("1010.49")) == 1010
    assert round_cents(Decimal("47250.00")) == 47250
