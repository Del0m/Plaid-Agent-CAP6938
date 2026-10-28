from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from app.budget_engine.pace import BudgetLine
from app.budget_engine.txns import round_cents

# next month's limits at period close (architecture 6.5). smoothing and the clamp keep the
# budget from chasing one bad month.

SMOOTH_OLD, SMOOTH_ACTUAL = 0.7, 0.3
CLAMP = 0.15


@dataclass(frozen=True)
class Adjustment:
    category: str
    old_cents: int
    actual_cents: int
    new_cents: int
    source: Literal["accepted", "smoothed", "fixed"]


def close_period(lines: Sequence[BudgetLine], actuals: dict[str, int], accepted: dict[str, int]) -> list[Adjustment]:
    out = []
    for line in lines:
        old, actual = line.limit_cents, actuals.get(line.category, 0)
        if line.category in accepted:
            # the user agreed to this number; it isn't ours to clamp
            new, source = accepted[line.category], "accepted"
        elif line.kind == "fixed":
            # a rent increase should be adopted, not smoothed toward; a $0 month means it moved, not stopped
            new, source = (actual if actual > 0 else old), "fixed"
        else:
            smoothed = round_cents(Decimal(str(SMOOTH_OLD)) * old + Decimal(str(SMOOTH_ACTUAL)) * actual)
            clamp = Decimal(str(CLAMP))
            low, high = round_cents(old * (1 - clamp)), round_cents(old * (1 + clamp))
            new, source = min(max(smoothed, low), high), "smoothed"
        out.append(Adjustment(line.category, old, actual, new, source))
    return out
