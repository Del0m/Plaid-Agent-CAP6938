import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Union

from sqlalchemy.orm import Session

from app.budget_engine.txns import add_months, month_start, round_cents
from tests.factories import add_account, add_txn, add_user

# synthetic users for the budget accuracy eval (spec: tpc-18). each is a few spending patterns run through a
# seeded rng, so ~300 transactions per user are reproducible and the planted facts can't be mis-summed by hand.
# every profile fits on may-july (today = aug 1) and is scored against august


@dataclass(frozen=True)
class Weekly:
    category: str
    merchant: str
    cents: int
    jitter: float = 0.1
    weekday: int = 5            # monday = 0
    growth: float = 0.0         # per month, compounding from the profile's first month


@dataclass(frozen=True)
class Monthly:
    category: str
    merchant: str
    cents: int
    day: int = 1
    moved: dict[date, date] = field(default_factory=dict)    # first of a month -> the day it was actually paid
    first: Optional[date] = None                             # no charge before this date (a new gym)


@dataclass(frozen=True)
class Once:
    category: str
    merchant: str
    cents: int
    day: date


Pattern = Union[Weekly, Monthly, Once]


@dataclass(frozen=True)
class Profile:
    id: str
    description: str
    seed: int
    patterns: tuple[Pattern, ...]
    today: date = date(2026, 8, 1)
    start: date = date(2026, 5, 1)
    end: date = date(2026, 8, 31)
    run_date: Optional[date] = None        # planted users: the day to run the budget graph
    expected_cause: Optional[str] = None   # planted users: the cause diagnose should find


# (day, cents, category, merchant), sorted; the same profile always gives the same list
def transactions(profile: Profile) -> list[tuple[date, int, str, str]]:
    rng = random.Random(profile.seed)
    out = []
    for p in profile.patterns:
        if isinstance(p, Once):
            out.append((p.day, p.cents, p.category, p.merchant))
        elif isinstance(p, Monthly):
            m = month_start(profile.start)
            while m <= profile.end:
                day = p.moved.get(m, m.replace(day=p.day))
                if p.first is None or day >= p.first:
                    out.append((day, p.cents, p.category, p.merchant))
                m = add_months(m, 1)
        else:
            d = profile.start
            while d <= profile.end:
                if d.weekday() == p.weekday:
                    months = (d.year - profile.start.year) * 12 + d.month - profile.start.month
                    amount = p.cents * (1 + p.growth) ** months * rng.uniform(1 - p.jitter, 1 + p.jitter)
                    out.append((d, round_cents(Decimal(str(amount))), p.category, p.merchant))
                d += timedelta(days=1)
    return sorted(out)


def seed_profile(session: Session, profile: Profile) -> str:
    user_id = f"budget-{profile.id}"
    item = add_user(session, user_id, f"{profile.id}@eval.local")
    acct = add_account(session, item, "Checking")
    for day, cents, category, merchant in transactions(profile):
        add_txn(session, acct, cents, category, merchant=merchant, day=day)
    session.commit()
    return user_id


RENT = Monthly("RENT_AND_UTILITIES", "Landlord", 150000)
# income and a savings transfer in every profile, so the spending filter is always exercised
BASE = (
    Monthly("INCOME", "Employer", -420000, day=15),
    Monthly("TRANSFER_OUT", "Savings", 50000, day=16),
    Weekly("FOOD_AND_DRINK", "Diner", 2500, weekday=2),
    Weekly("FOOD_AND_DRINK", "Diner", 2500, weekday=4),
    Weekly("TRANSPORTATION", "Fuel", 4000, weekday=0),
    Monthly("ENTERTAINMENT", "Streamly", 1599, day=5),
)
GROCER = Weekly("FOOD_AND_DRINK", "Grocer", 11000)

PROFILES = (
    Profile("steady", "flat groceries, dining, fuel and rent", 1, (RENT, GROCER, *BASE)),
    Profile("vacation", "steady, plus a $1,900 June trip", 2, (RENT, GROCER, *BASE,
            Once("TRAVEL", "Airline", 20000, date(2026, 5, 12)),
            Once("TRAVEL", "Airline", 120000, date(2026, 6, 10)),
            Once("TRAVEL", "Hotel", 50000, date(2026, 6, 12)),
            Once("TRAVEL", "Car hire", 20000, date(2026, 6, 14)),
            Once("TRAVEL", "Airline", 25000, date(2026, 7, 12)),
            Once("TRAVEL", "Airline", 22000, date(2026, 8, 12)))),
    Profile("early-rent", "steady, but July's rent is paid on June 30", 3,
            (Monthly("RENT_AND_UTILITIES", "Landlord", 150000, moved={date(2026, 7, 1): date(2026, 6, 30)}),
             GROCER, *BASE)),
    Profile("rising", "groceries up 10% a month", 4,
            (RENT, Weekly("FOOD_AND_DRINK", "Grocer", 11000, growth=0.10), *BASE)),
    Profile("occasional", "steady, plus one $300 purchase in June", 5, (RENT, GROCER, *BASE,
            Once("GENERAL_MERCHANDISE", "Shop", 30000, date(2026, 6, 14)))),
)

# planted overspends for the budget graph (spec: tpc-19), one per cause. a minimal base on purpose: a steady
# user's monthly streaming charge and weekday counts already raise pace flags of their own (a tpc-17 follow-up),
# which would add unrelated flags to the demo
PLANTED_BASE = (RENT, Monthly("INCOME", "Employer", -420000, day=15), Monthly("TRANSFER_OUT", "Savings", 50000, day=16))
CORNER = Weekly("GENERAL_MERCHANDISE", "Corner Shop", 3500)
DECEMBER_SHOPS = (("Toy Barn", 1, 6000), ("Gift Gallery", 3, 7500), ("Book Nook", 5, 9000),
                  ("Game Stop Shop", 7, 12000), ("Candle Co", 9, 6500), ("Sweater Hut", 11, 11000),
                  ("Craft Corner", 13, 8000), ("Kitchen Plus", 14, 10000))

PLANTED = (
    Profile("overspend-one-time", "a $600 purchase at a new shop on Oct 12", 11,
            (*PLANTED_BASE, CORNER, Once("GENERAL_MERCHANDISE", "ElectroMart", 60000, date(2026, 10, 12))),
            today=date(2026, 10, 1), start=date(2026, 7, 1), end=date(2026, 10, 31),
            run_date=date(2026, 10, 20), expected_cause="one_time"),
    Profile("overspend-recurring", "a $90 gym that started Sep 3", 12,
            (*PLANTED_BASE, Monthly("PERSONAL_CARE", "Barber", 4000, day=20),
             Monthly("PERSONAL_CARE", "FitClub Gym", 9000, day=3, first=date(2026, 9, 3))),
            today=date(2026, 10, 1), start=date(2026, 7, 1), end=date(2026, 10, 31),
            run_date=date(2026, 10, 20), expected_cause="recurring"),
    Profile("overspend-seasonal", "holiday gifts at eight shops, Dec 1-14", 13,
            (*PLANTED_BASE, CORNER,
             *(Once("GENERAL_MERCHANDISE", shop, cents, date(2026, 12, d)) for shop, d, cents in DECEMBER_SHOPS)),
            today=date(2026, 12, 1), start=date(2026, 9, 1), end=date(2026, 12, 31),
            run_date=date(2026, 12, 15), expected_cause="seasonal"),
    # found in the tpc-19 review: a big one-off at a regular shop was called recurring
    Profile("overspend-known-merchant", "a $520 one-off at a shop that usually comes to $120 a month", 14,
            (*PLANTED_BASE, Monthly("GENERAL_MERCHANDISE", "MegaMart", 12000, day=8),
             Once("GENERAL_MERCHANDISE", "MegaMart", 52000, date(2026, 10, 12))),
            today=date(2026, 10, 1), start=date(2026, 7, 1), end=date(2026, 10, 31),
            run_date=date(2026, 10, 20), expected_cause="one_time"),
)
