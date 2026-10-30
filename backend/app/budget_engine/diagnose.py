import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Literal, Optional

from app.budget_engine.fit import FLEX_MARKUP, HISTORY_MONTHS
from app.budget_engine.pace import PaceResult
from app.budget_engine.txns import Txn, add_months, month_end, month_start, round_cents, spending

# the facts the budget graph's diagnose step hands the llm (spec: tpc-19). the code works out what a small
# model would get wrong from raw rows (how much one purchase explains, which merchants are new or repeating);
# the llm only reads them and names the cause

TOP_MERCHANTS = 5
PROPOSAL_STEP = 500     # a proposed limit is a round number of dollars: up to the next $5


@dataclass(frozen=True)
class MerchantFact:
    merchant: Optional[str]
    period_cents: int            # net spend this period, up to today
    charges: int                 # purchases this period
    prior_months: int            # how many of the history months before the period had spending here
    typical_cents: Optional[int] # median monthly spend here over those months, None if never seen
    # named outright because a small model misread a count of 1 as "new". judged over the history months the
    # user has data for: never seen / every one of them / only the latest ones, in a row (it started since the
    # budget's history began) / some earlier month but not the latest ones
    pattern: Literal["new", "regular", "started_recently", "occasional"]


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
    # what the history months would fit (median x 1.05) without the merchants that started recently; None with no
    # history. a merchant already in the median would otherwise be counted twice in the proposal
    baseline_cents: Optional[int] = None


def projected_cents(spent_cents: int, days_elapsed: int, days_in_period: int) -> int:
    return round_cents(Decimal(spent_cents) * days_in_period / days_elapsed)


# fixed: what the bill actually was. flexible: the category without its recently started merchants (the old limit,
# or the baseline when that's lower) plus their typical month (a new gym), since a charge already posted would
# inflate a straight-line projection; with no such merchant, where the month is heading. rounded up to $5. only
# used when the cause is recurring
def proposed_limit_cents(facts: OverspendFacts) -> int:
    if facts.kind == "fixed":
        base = facts.spent_cents
    else:
        new = [m.typical_cents for m in facts.merchants if m.pattern == "started_recently" and m.typical_cents]
        if new:
            without = facts.limit_cents if facts.baseline_cents is None else min(facts.limit_cents, facts.baseline_cents)
            base = without + sum(new)
        else:
            base = facts.projected_cents
    return -(-base // PROPOSAL_STEP) * PROPOSAL_STEP


def overspend_facts(flag: PaceResult, line_fit: Optional[dict], period_txns: Iterable[Txn],
                    history_txns: Iterable[Txn], period_start: date) -> OverspendFacts:
    history_txns = list(history_txns)    # read twice below; a generator would come back empty
    today = period_start + timedelta(days=flag.days_elapsed - 1)
    hist_start = add_months(period_start, -HISTORY_MONTHS)
    period = [t for t in spending(period_txns) if t.category == flag.category and period_start <= t.date <= today]
    history = [t for t in spending(history_txns)
               if t.category == flag.category and hist_start <= t.date < period_start]

    # only the history months the user has data for count: a user who linked in september has one, not three
    months = [add_months(hist_start, i) for i in range(HISTORY_MONTHS)]
    first = min((t.date for t in history_txns), default=None)
    covered = [m for m in months if first is not None and m >= month_start(first)]

    purchases = [t for t in period if t.amount_cents > 0]
    largest = max(purchases, key=lambda t: (t.amount_cents, t.date), default=None)
    share = min(1.0, largest.amount_cents / flag.spent_cents) if largest and flag.spent_cents > 0 else 0.0

    merchants = [_merchant_fact(name, period, history, covered)
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
        baseline_cents=_baseline(history, covered, {m.merchant for m in merchants if m.pattern == "started_recently"}),
    )


# the category's median history month without the recently started merchants, with fit's 5%
def _baseline(history: list[Txn], covered: list[date], new: set) -> Optional[int]:
    if not covered:
        return None
    totals = [sum(t.amount_cents for t in history if m <= t.date <= month_end(m) and t.merchant not in new)
              for m in covered]
    return round_cents(Decimal(str(statistics.median(totals))) * Decimal(str(FLEX_MARKUP)))


# unnamed purchases are grouped as one merchant (None); a refund nets within its merchant
def _merchant_fact(name: Optional[str], period: list[Txn], history: list[Txn], covered: list[date]) -> MerchantFact:
    mine = [t for t in period if t.merchant == name]
    totals = [sum(t.amount_cents for t in history if t.merchant == name and m <= t.date <= month_end(m))
              for m in covered]
    present = [total > 0 for total in totals]
    monthly = [total for total in totals if total > 0]
    typical = round_cents(Decimal(str(statistics.median(monthly)))) if monthly else None
    return MerchantFact(merchant=name, period_cents=sum(t.amount_cents for t in mine),
                        charges=sum(1 for t in mine if t.amount_cents > 0),
                        prior_months=len(monthly), typical_cents=typical, pattern=_pattern(present))


# present: one flag per covered history month, oldest first
def _pattern(present: list[bool]) -> str:
    if not any(present):
        return "new"
    if all(present):
        return "regular"
    first_seen = present.index(True)
    # seen in a run of months that reaches the latest one, after months without it
    return "started_recently" if all(present[first_seen:]) else "occasional"
