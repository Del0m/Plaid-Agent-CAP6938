import calendar
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

# the budget engine's input: plain values the caller (TPC-18) builds from `transactions` rows.
# no DB or clock in here, so every budget rule is testable with literals.


@dataclass(frozen=True)
class Txn:
    date: date
    amount_cents: int            # plaid sign: positive = money out, negative = money in / refund
    category: str                # pf_category_primary; the caller maps None to "UNCATEGORIZED"
    detailed: Optional[str] = None
    merchant: Optional[str] = None


# not spending: income isn't, a transfer moves money between your own accounts,
# and a card payment pays for purchases already counted
EXCLUDED_PRIMARY = frozenset({"INCOME", "TRANSFER_IN", "TRANSFER_OUT"})
EXCLUDED_DETAILED = frozenset({"LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"})


def spending(txns: Iterable[Txn]) -> list[Txn]:
    return [t for t in txns if t.category not in EXCLUDED_PRIMARY and t.detailed not in EXCLUDED_DETAILED]


def category_totals(txns: Iterable[Txn], start: date, end: date) -> dict[str, int]:
    totals: dict[str, int] = {}
    for t in txns:
        if start <= t.date <= end:
            totals[t.category] = totals.get(t.category, 0) + t.amount_cents
    # refunds net a category down, but a refund larger than the month's purchases isn't negative spending
    return {cat: max(total, 0) for cat, total in totals.items()}


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    if d.day != 1:
        raise ValueError(f"add_months needs the 1st of a month, got {d}")
    year, month = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(year, month + 1, 1)


# money rounds half up; python's round() is banker's rounding
def round_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
