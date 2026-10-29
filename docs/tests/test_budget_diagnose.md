# Tests: `backend/tests/test_budget_diagnose.py`

These tests cover `app/budget_engine/diagnose.py`, the facts the budget graph's `diagnose` step gives the LLM (TPC-19). For one flagged category, the facts include:
- how much the biggest purchase explains;
- which merchants are new this month and which repeat from the 3 months before, with their typical amount;
- the category's monthly history;
- the projected month-end spend and the proposed limit for `adjust_next_budget`.

The code works these out, so a small model doesn't have to; the LLM only reads them and names the cause.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_diagnose.py -v
```

## Shared setup

- `OCT` is Oct 1 2026, the period start. History is July-September.
- `flag(cat, spent, limit, reason, kind, pace)` builds a flagged `PaceResult` on day 20 of 31.
- `t(m, d, cents, cat, merchant)` builds a 2026 `Txn`, and `merchant(facts, name)` finds one merchant's facts.

---

## `test_one_time_largest_and_new_merchant`

**What it does:** With a $600 ElectroMart purchase and a $35 corner-shop purchase this month, and the corner shop in each of the last three months, the largest purchase is the $600 one and explains 600/635 of the spending. ElectroMart was seen in 0 earlier months and has no typical amount. The corner shop was seen in 3, at $35.

**Why it's there:** These are the facts that make a purchase `one_time`: one big charge at a merchant never seen before.

## `test_recurring_gym_seen_once_before`

**What it does:** A $90 gym charged this month and once last month (September) has `prior_months == 1` and a typical amount of $90. Merchants are ordered by this month's spend.

**Why it's there:** A charge that repeats at the same amount since the budget was set is the signature of `recurring`.

## `test_history_from_fit_json`

**What it does:** The monthly totals and outlier months come from the budget line's stored `fit_json`, and are empty when there is none (a period opened by close).

**Why it's there:** The history the LLM sees is the history the budget was built from (TPC-18), not a fresh recomputation.

## `test_other_categories_and_income_ignored`

**What it does:** A bigger food purchase and a paycheck in the same month don't appear in a general-merchandise flag's merchants or largest purchase.

**Why it's there:** Facts for one category must only describe that category. Architecture section 7 says a diagnosis of dining sees dining only.

## `test_merchants_capped_and_unnamed_grouped`

**What it does:** Seven named merchants plus two purchases with no merchant give 5 entries. The two unnamed purchases are one entry with 2 charges, and the entries are ordered by spend.

**Why it's there:** It keeps the prompt small for a local model. Plaid often has no merchant name, and those purchases still count.

## `test_largest_share_capped`

**What it does:** A $100 purchase with a $40 refund (net $60) gives a share of 1.0, not 1.67, and the merchant's spend is net of the refund.

**Why it's there:** A share above 100% would confuse the model. Refunds are netted, as everywhere else in the budget.

## `test_projected_cents`

**What it does:** $100 spent by day 20 of 31 projects to $155.00, and 1001¢ by day 2 projects to 15516¢ (15515.5 rounded half up).

**Why it's there:** The projection is the number behind the proposed limit, so it uses the same half-up money rounding as the rest of the budget.

## `test_proposed_limit`

**What it does:** A flexible projection of 15502¢ proposes $160, and one of exactly $155 stays at $155. A fixed line proposes what was actually spent.

**Why it's there:** The code sets the number the user accepts, not the LLM. A round $5 figure is easy to read, and a fixed bill should follow the actual amount, as at period close.
