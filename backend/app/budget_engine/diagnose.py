import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Literal, Optional

from app.budget_engine.pace import PaceResult
from app.budget_engine.txns import Txn, add_months, month_end, round_cents, spending

# the facts the budget graph's diagnose step hands the llm (spec: tpc-19). the code works out what a small
# model would get wrong from raw rows (how much one purchase explains, which merchants are new or repeating);
# the llm only reads them and names the cause

TOP_MERCHANTS = 5
HISTORY_MONTHS = 3
PROPOSAL_STEP = 500     # a proposed limit is a round number of dollars: up to the next $5


@dataclass(frozen=True)
class MerchantFact:
    merchant: Optional[str]
    period_cents: int            # net spend this period, up to today
    charges: int                 # purchases this period
    prior_months: int            # how many of the 3 months before the period had spending here
    typical_cents: Optional[int] # median monthly spend here over those months, None if never seen
    # named outright because a small model misread a count of 1 as "new": not seen before / seen in 1-2 of the
    # last 3 months (started since the budget's history) / every month
    pattern: Literal["new", "started_recently", "regular"]


@dataclass(frozen=True)
class OverspendFacts:
    category: str
    reason: Literal["pace", "over_limit"]
    kind: Literal["fixed", "flexible"]
    month: date                  # period start
    limit_cents: int
    spent_cents: int
    pace: Optional[float]
    days_elapsed: int
    days_in_period: int
    projected_cents: int
    largest: Optional[Txn]       # biggest purchase this period
    largest_share: float         # largest / spent, capped at 1 (a refund can make it bigger); 0 when nothing spent
    largest_vs_typical: Optional[float]   # largest / its merchant's typical month; None for a merchant never seen
    merchants: tuple[MerchantFact, ...]   # top 5 by period spend, then name
    monthly_totals: tuple[int, ...]       # from the line's fit_json, oldest first
    outlier_months: tuple[date, ...]


def projected_cents(spent_cents: int, days_elapsed: int, days_in_period: int) -> int:
    return round_cents(Decimal(spent_cents) * days_in_period / days_elapsed)


# fixed: what the bill actually was. flexible: the old limit plus the typical month of any merchant that started
# recently (a new gym), since a charge already posted would inflate a straight-line projection; with no such
# merchant, where the month is heading. rounded up to $5. only used when the cause is recurring
def proposed_limit_cents(facts: OverspendFacts) -> int:
    if facts.kind == "fixed":
        return facts.spent_cents
    new = [m.typical_cents for m in facts.merchants if 1 <= m.prior_months < HISTORY_MONTHS and m.typical_cents]
    base = facts.limit_cents + sum(new) if new else facts.projected_cents
    return -(-base // PROPOSAL_STEP) * PROPOSAL_STEP


def overspend_facts(flag: PaceResult, line_fit: Optional[dict], period_txns: Iterable[Txn],
                    history_txns: Iterable[Txn], period_start: date) -> OverspendFacts:
    today = period_start + timedelta(days=flag.days_elapsed - 1)
    hist_start = add_months(period_start, -HISTORY_MONTHS)
    period = [t for t in spending(period_txns) if t.category == flag.category and period_start <= t.date <= today]
    history = [t for t in spending(history_txns)
               if t.category == flag.category and hist_start <= t.date < period_start]

    purchases = [t for t in period if t.amount_cents > 0]
    largest = max(purchases, key=lambda t: (t.amount_cents, t.date), default=None)
    share = min(1.0, largest.amount_cents / flag.spent_cents) if largest and flag.spent_cents > 0 else 0.0

    merchants = [_merchant_fact(name, period, history, hist_start)
                 for name in {t.merchant for t in period}]
    merchants.sort(key=lambda mf: (-mf.period_cents, mf.merchant or ""))
    # a big one-off at a regular shop: far above what that merchant usually comes to in a month
    typical = next((m.typical_cents for m in merchants if largest and m.merchant == largest.merchant), None)
    vs_typical = largest.amount_cents / typical if largest and typical else None

    fit = line_fit or {}
    return OverspendFacts(
        category=flag.category, reason=flag.reason, kind=flag.kind, month=period_start,
        limit_cents=flag.limit_cents, spent_cents=flag.spent_cents, pace=flag.pace,
        days_elapsed=flag.days_elapsed, days_in_period=flag.days_in_period,
        projected_cents=projected_cents(flag.spent_cents, flag.days_elapsed, flag.days_in_period),
        largest=largest, largest_share=share, largest_vs_typical=vs_typical, merchants=tuple(merchants[:TOP_MERCHANTS]),
        monthly_totals=tuple(fit.get("monthly_totals", ())),
        outlier_months=tuple(date.fromisoformat(o["month"]) for o in fit.get("outliers", ())),
    )


# unnamed purchases are grouped as one merchant (None); a refund nets within its merchant
def _merchant_fact(name: Optional[str], period: list[Txn], history: list[Txn], hist_start: date) -> MerchantFact:
    mine = [t for t in period if t.merchant == name]
    monthly = []
    m = hist_start
    for _ in range(HISTORY_MONTHS):
        total = sum(t.amount_cents for t in history if t.merchant == name and m <= t.date <= month_end(m))
        if total > 0:
            monthly.append(total)
        m = add_months(m, 1)
    typical = round_cents(Decimal(str(statistics.median(monthly)))) if monthly else None
    return MerchantFact(merchant=name, period_cents=sum(t.amount_cents for t in mine),
                        charges=sum(1 for t in mine if t.amount_cents > 0),
                        prior_months=len(monthly), typical_cents=typical,
                        pattern="new" if not monthly else "regular" if len(monthly) == HISTORY_MONTHS
                        else "started_recently")
