# Tests: `frontend/components/BudgetPaceBar.test.tsx`

These tests cover `components/BudgetPaceBar.tsx`, one budget line on the dashboard (TPC-20). The bar fills with the share of the limit spent. A thin marker shows how much of the month has passed, so a fill past the marker means spending is ahead of pace. Amber means the agent flagged it for pace, and red means it's over the limit.

Run them from `frontend/`:

```bash
npm test -- components/BudgetPaceBar.test.tsx
```

## Shared setup

- `line(over)` builds a food line: a $472 limit with $236 spent, unflagged.
- `draw(line)` renders it on day 20 of 31 and returns the meter, the fill and the today marker.

---

## `fills by spent over limit and marks today`

**What it does:** $236 of $472 gives a meter value of 50 and a 50% fill. The today marker sits at 20/31 of the width. The label reads "Food and drink" and "$236.00 of $472.00".

**Why it's there:** The marker against the fill is how the user reads pace at a glance, and `role="meter"` makes the same number available to screen readers.

## `amber on pace, red over the limit, neutral otherwise`

**What it does:** The fill is amber for a pace flag, red for over the limit, and neither for an unflagged line.

**Why it's there:** The colours match the alert severities ("Ahead of pace", "Over limit") the user sees on the Alerts page.

## `caps the bar at 100% but shows what was really spent`

**What it does:** $705.31 against a $149.96 limit fills 100% (meter 100), and the text still says "$705.31 of $149.96".

**Why it's there:** A bar can't grow past its box, and the user still needs the real amount.

## `a zero limit draws an empty bar`

**What it does:** A limit of $0 gives a 0% fill instead of dividing by zero.

**Why it's there:** A zero limit can happen, and the dashboard mustn't show `NaN%` or crash.

## `fixed lines say so`

**What it does:** A fixed line (rent) is labelled "(fixed)".

**Why it's there:** Fixed lines aren't judged on pace (rent lands on the 1st), so a full bar early in the month is expected.
