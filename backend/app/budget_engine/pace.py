from dataclasses import dataclass
from datetime import date
from typing import Iterable, Literal, Optional, Sequence

from app.budget_engine.txns import Txn, category_totals, month_end, month_start, shift_early_bills, spending

# today's overspend flags (architecture 6.4). budget periods are calendar months.

PACE_FLAG = 1.10
MIN_DAYS = 5      # one grocery trip on day 1 isn't overspending


@dataclass(frozen=True)
class BudgetLine:
    category: str
    kind: Literal["fixed", "flexible"]
    limit_cents: int


@dataclass(frozen=True)
class PaceResult:
    category: str
    kind: Literal["fixed", "flexible", "unbudgeted"]
    limit_cents: int              # 0 for unbudgeted
    spent_cents: int
    days_elapsed: int
    days_in_period: int
    pace: Optional[float]         # None for fixed, unbudgeted, or a limit of 0
    flagged: bool
    reason: Optional[Literal["pace", "over_limit"]]


def evaluate(lines: Sequence[BudgetLine], txns: Iterable[Txn], today: date) -> list[PaceResult]:
    # a fixed bill paid early for next month is next month's (judged against the line's limit as the usual bill)
    bills = {line.category: line.limit_cents for line in lines if line.kind == "fixed"}
    spent = category_totals(shift_early_bills(spending(txns), bills), month_start(today), today)
    days_elapsed, days_in_period = today.day, month_end(today).day

    results = []
    for line in lines:
        amount = spent.get(line.category, 0)
        pace = None
        # fixed bills are lumpy (rent lands on the 1st), so they're only judged against the limit
        if line.kind == "flexible" and line.limit_cents > 0:
            # one division, so a pace of exactly 1.10 isn't 1.0999999
            pace = amount * days_in_period / (line.limit_cents * days_elapsed)
        reason = None
        if amount > line.limit_cents:
            reason = "over_limit"
        elif pace is not None and days_elapsed >= MIN_DAYS and pace >= PACE_FLAG:
            reason = "pace"
        results.append(PaceResult(line.category, line.kind, line.limit_cents, amount, days_elapsed,
                                  days_in_period, pace, reason is not None, reason))

    # spending with no line is reported, not judged; the graph (TPC-19) decides what to do with it
    budgeted = {line.category for line in lines}
    for cat in sorted(c for c, amount in spent.items() if amount > 0 and c not in budgeted):
        results.append(PaceResult(cat, "unbudgeted", 0, spent[cat], days_elapsed, days_in_period,
                                  None, False, None))
    return results
