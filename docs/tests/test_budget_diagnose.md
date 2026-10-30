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

**What it does:** With no recently started merchant, a flexible projection of 15502¢ proposes $160, and one of exactly $155 stays at $155. A fixed line proposes what was actually spent.

**Why it's there:** The code sets the number the user accepts, not the LLM. A round $5 figure is easy to read, and a fixed bill should follow the actual amount, as at period close.

## `test_largest_vs_typical`

**What it does:** A $520 purchase at a shop whose usual month is $120 is 4.33 times typical. A purchase at a merchant never seen before has no ratio.

**Why it's there:** It was found in the TPC-19 review. A big one-off at a regular shop was called `recurring`, because "seen before" was the only signal. This ratio lets the model see that the purchase is far above that merchant's norm.

## `test_recurring_proposal_adds_new_merchants_typical`

**What it does:** For the gym user (a $42 limit; a $90 gym that started last month), the proposal is $135: the category without the gym ($42), plus the gym's typical month, rounded up to $5.

**Why it's there:** It was found in the TPC-19 review. A straight-line projection proposed $205 for what is a steady $130 a month, because both charges had already posted by day 20. The proposal is what close applies if the user accepts it.

## `test_merchant_pattern`

**What it does:** Merchants are labelled `started_recently` (the gym, seen in the latest of the 3 history months), `regular` (the barber, seen in all 3) or `new` (a spa, never seen).

**Why it's there:** In a live run, the model read "months_seen_before: 1" as "seen for the first time" and called the gym `one_time`. Naming the pattern in code removes that reading error, and the model still decides the cause.

## `test_short_history_merchant_is_regular`

**What it does:** History holds only September. A grocer seen in September is `regular`, and since no merchant started recently, a recurring proposal is the projection rounded up to $5.

**Why it's there:** The pattern used to assume three months of history. For a user who linked in September, every merchant had 1 prior month and was labelled `started_recently`. The prompt's first rule then pushed the cause to recurring, and the proposal added every merchant's typical month on top of a limit already fit from them, about twice the category's real spend. Patterns are now judged over the history months the user has data for. The review on #12 found it.

## `test_recurring_merchant_already_in_the_median_not_added_twice`

**What it does:** A $90 gym appears in August and September, and a $40 barber in all three months, so the limit is $136.50 (the $130 median × 1.05). The gym is `started_recently`, and the proposal is $135: the barber-only baseline ($42) plus one gym, rounded up to $5.

**Why it's there:** The old rule added the gym's month to the old limit, but the $130 median already held the gym, so it proposed $227. The proposal now starts from the lower of the old limit and the category's median without the new merchants. The review on #12 found it.

## `test_merchant_seen_long_ago_is_occasional`

**What it does:** A camera shop seen in July, but not in August or September, is `occasional` when it shows up again in October.

**Why it's there:** It used to count as `started_recently` (seen in 1 of 3 months), which the prompt reads as "began charging since the budget was set and now charges every month". A $500 one-off at a shop visited once months ago was then called recurring, and next month's limit went up by $500. `occasional` is treated like `new` by the prompt's one-time rule. The review on #12 found it.

## `test_fixed_proposal_rounded_up`

**What it does:** A fixed line that spent $1,523.37 proposes $1,525.

**Why it's there:** The comment said every proposal is rounded up to $5, but a fixed line's proposal was the raw amount. The review on #12 found it. Next month's bill paid early no longer counts toward this month (TPC-17's early-bill rule in pace), so it can't double a fixed proposal either.
