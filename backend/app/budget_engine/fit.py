import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Literal

from app.budget_engine.txns import Txn, add_months, category_totals, month_end, month_start, round_cents, spending

# initial budget from history (architecture 6.3). fixed lines take the latest amount; flexible lines
# take the median month plus a small cushion, so one vacation doesn't inflate the budget.

FIXED_DEFAULT = frozenset({"RENT_AND_UTILITIES", "LOAN_PAYMENTS"})
FLEX_MARKUP = 1.05
OUTLIER_RATIO = 1.5
HISTORY_MONTHS = 3


@dataclass(frozen=True)
class OutlierMonth:
    month: date                  # first of the month
    total_cents: int
    median_cents: int
    top_txns: tuple[Txn, ...]    # up to 3, largest amount first


@dataclass(frozen=True)
class FittedLine:
    category: str
    kind: Literal["fixed", "flexible"]
    limit_cents: int
    monthly_totals: tuple[int, ...]   # oldest month first
    outliers: tuple[OutlierMonth, ...]


@dataclass(frozen=True)
class FitResult:
    lines: tuple[FittedLine, ...]     # sorted by category
    skipped: tuple[str, ...]          # flexible categories whose median is 0, sorted


def fit_budget(txns: Iterable[Txn], today: date, fixed: frozenset[str] = FIXED_DEFAULT) -> FitResult:
    txns = list(txns)    # read more than once below; a generator would come back empty
    if not txns:
        return FitResult((), ())
    # complete months only (a partial month would drag every median down), and none before the history starts
    current = month_start(today)
    first = max(add_months(current, -HISTORY_MONTHS), month_start(min(t.date for t in txns)))
    months = []
    m = first
    while m < current:
        months.append(m)
        m = add_months(m, 1)
    if not months:
        return FitResult((), ())

    spent = spending(txns)
    per_month = [category_totals(spent, m, month_end(m)) for m in months]
    categories = sorted({cat for totals in per_month for cat, total in totals.items() if total > 0})

    lines, skipped = [], []
    for cat in categories:
        totals = tuple(t.get(cat, 0) for t in per_month)
        if cat in fixed:
            # rent paid a day early lands in the previous month, so take the latest month that has it
            latest = next(total for total in reversed(totals) if total > 0)
            lines.append(FittedLine(cat, "fixed", latest, totals, ()))
            continue
        median = Decimal(str(statistics.median(totals)))
        if median == 0:
            # an occasional purchase would get a $0 limit that flags every time
            skipped.append(cat)
            continue
        outliers = tuple(
            OutlierMonth(m, total, round_cents(median), _top_txns(spent, cat, m))
            for m, total in zip(months, totals) if total > median * Decimal(str(OUTLIER_RATIO))
        )
        limit = round_cents(median * Decimal(str(FLEX_MARKUP)))
        lines.append(FittedLine(cat, "flexible", limit, totals, outliers))
    return FitResult(tuple(lines), tuple(skipped))


def _top_txns(spent: list[Txn], category: str, month: date) -> tuple[Txn, ...]:
    end = month_end(month)
    hits = [t for t in spent if t.category == category and month <= t.date <= end and t.amount_cents > 0]
    return tuple(sorted(hits, key=lambda t: (-t.amount_cents, t.date))[:3])
