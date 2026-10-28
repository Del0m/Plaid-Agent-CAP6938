from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import BudgetLine, BudgetPeriod
from tests.factories import add_user


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
