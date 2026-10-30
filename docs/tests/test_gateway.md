# Tests: `backend/tests/test_gateway.py`

These tests cover `app/privacy/gateway.py`, the only code path that turns user data into LLM prompt text (architecture section 7). They check the per-purpose field allowlists, the per-session pseudonym, scrubbing of free-text fields, spending minimization, and the JSONL audit log. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_gateway.py -v
```

## Shared setup

The `db` fixture from `conftest.py` provides a fresh in-memory database (see [`test_plaid.md`](test_plaid.md#shared-setup-backendtestsconftestpy)). Every test passes its own `audit_path` under pytest's `tmp_path`, so nothing is written to `backend/logs/`.

- `fixed_today` (autouse) pins `gateway._today()` to 2026-10-21, so the default 30-day spending window is the same on every run.
- `add_user`, `add_account` and `add_txn` (in `tests/factories.py`, shared with `test_tools.py`) write rows straight to the database. `add_account` can attach a liability. `add_txn` takes an optional `detailed` category, which the card-payment test needs.
- `seed_pii_user(db)` creates the spec's "Jane Doe" with PII in every place it could leak from:
  - email `jane.doe@example.com`;
  - item id `item-secret-1`;
  - an account named "Jane Doe's Checking ••4321" with mask `4321` and Plaid id `acc-secret-1`;
  - a credit card with a 22% APR;
  - transactions "Zelle to Jane Doe" (TRANSFER_OUT) and "Starbucks" (FOOD_AND_DRINK).
- `PII` is the list of strings that must never appear in a prompt. It includes the user id `u-jane`.
- `data(prompt)` parses a prompt and returns its `data`. `audit_lines(path)` parses the JSONL log.

---

## `test_no_pii_in_any_prompt`

**What it does:** Builds every kind of context for Jane: accounts, debts, spending totals, and spending for TRANSFER_OUT. It asserts that none of the `PII` strings appear in any of them.

**Why it's there:** This is the ticket's done-when: "a unit test shows no name/email/account number in a gateway-built prompt." It covers the two ways PII leaks:
- an allowlist mistake would leak the email, mask, ids or user id;
- a scrub gap would leak the name in the account label or the Zelle merchant.

## `test_rows_have_only_allowlisted_keys`

**What it does:** Checks that every account, debt and transaction row has exactly the keys in `ACCOUNT_FIELDS`, `DEBT_FIELDS` and `TXN_FIELDS`.

**Why it's there:** It enforces "allowlist, not blocklist" (section 7, step 1). If someone builds a row from a whole model object, or adds a field without updating the allowlist, this test fails before the field reaches a prompt.

## `test_money_in_dollars`

**What it does:** Checks that a balance of 12345 cents shows up as `123.45`.

**Why it's there:** The database stores integer cents, but a model reading `41000` would say forty-one thousand dollars. The conversion happens in one place (`_dollars`) and this test pins it.

## `test_pseudonym_format_and_lifetime`

**What it does:** Replaces `secrets.token_hex` so two gateways get `7f3a` and `b2c1`. It checks that the first gateway uses `user_7f3a` in every prompt it builds and the second uses `user_b2c1`.

**Why it's there:** The model sees a pseudonym, never the internal user id (section 7, step 4). The pseudonym must stay the same within one chat session so the model sees one consistent user, and change between sessions so it can't link sessions together.

## `test_pseudonym_can_be_reused`

**What it does:** Builds a gateway with `pseudonym="user_abcd"` and checks that the gateway and its prompt both use it.

**Why it's there:** A chat session spans many HTTP requests, and each request gets its own database session and so its own gateway. Without a way to pass the pseudonym back in, the model would see a new user on every turn. The final review found this gap before TPC-15 built on the constructor.

## `test_other_users_data_hidden`

**What it does:** Adds a second user with a card named "Theirs" and checks that it doesn't appear in Jane's accounts or debts.

**Why it's there:** Every query needs a `user_id` filter. A missing filter here would send another person's finances to the model, which is worse than a leak to the screen, because it also ends up in the audit log.

## `test_debts_sorted_like_dashboard`

**What it does:** Adds cards with APRs of 15, 24.99 and unknown, and expects the order 24.99, 15, unknown.

**Why it's there:** The sort key is `finance.avalanche_key`, shared with GET /dashboard so the two can't drift apart. The M2 done-when question is "which debt first?". If the model sees debts in the same avalanche order as the dashboard, the answer and the page agree, and a small model doesn't have to sort the list itself.

## `test_spending_totals_positive_only_in_range`

**What it does:**
- Adds a purchase of +650 and a refund of -200 in FOOD_AND_DRINK, a +100 purchase before the start date, and a removed +999.
- Expects `{"FOOD_AND_DRINK": 6.5}`.

**Why it's there:** It checks three rules at once:
- spending means money out, so positive amounts only, per the sign convention in section 5;
- removed (soft-deleted) transactions don't count;
- the date range is respected.

Getting any of them wrong would give the model a wrong total to reason about.

## `test_spending_category_lists_capped_newest_first`

**What it does:** Adds 60 FOOD_AND_DRINK transactions and one TRANSFER_OUT, then asks for FOOD_AND_DRINK. It expects 50 rows, the first dated today, `count: 60` with `truncated: true`, and no TRANSFER_OUT anywhere.

**Why it's there:** This is "minimize per purpose" (section 7, step 2): a question about dining gets dining rows only. The cap keeps the prompt small, which matters for an 8B model's accuracy and speed. `count` and `truncated` were added after the review on #6: without them, 50 listed rows next to a total covering 80 quietly disagree, and a model that sums the rows or counts visits gets it wrong.

## `test_spending_category_case_insensitive_and_zero`

**What it does:** With one FOOD_AND_DRINK purchase, asking for `food_and_drink` returns FOOD_AND_DRINK with its $6.50 total, and asking for TRAVEL returns `{"category": "TRAVEL", "total": 0.0, "direction": "outflow", "count": 0, "truncated": false, "transactions": []}`.

**Why it's there:** Plaid categories are upper-case enums, but models often send them in lowercase. Before this fix, both a lowercase name and a category with no spending returned `{}`, which the model can't tell apart from "no data". It would then tell the user they spent nothing on dining. Now the category is normalized and zero is stated explicitly.

## `test_spending_defaults_to_last_30_days`

**What it does:** With today pinned to 2026-10-21, a transaction on 2026-09-22 is included and one on 2026-09-21 is not.

**Why it's there:** The tools in TPC-13 will often ask for spending without dates. This test pins the default window and its edge. The window is 30 calendar days including today. It used to start 30 days *before* today, which with both ends inclusive was 31 days, so a total the model calls "30-day spending" included an extra day. The review on #6 found it, and this test had locked the off-by-one in.

## `test_empty_user_builds_empty_context`

**What it does:** For a user with no rows, accounts and debts return `[]`, spending returns `{}`, and there are still three audit lines.

**Why it's there:** A new user will chat before linking a bank. The gateway should return empty context rather than crash, and every call should still be logged.

## `test_audit_line_per_call`

**What it does:** Builds two prompts and checks that the log has two lines. Each line's `prompt` matches the returned string exactly, the pseudonym matches, the purposes and params are right, and the user id appears nowhere in the file.

**Why it's there:** The audit log is the record of what left the system (section 7, step 6), and it's evidence for the report. It has to hold the exact text that was sent and must not become a PII leak itself.

## `test_audit_creates_missing_directory`

**What it does:** Points the audit log at a directory that doesn't exist yet and checks that the file is created.

**Why it's there:** `backend/logs/` is git-ignored, so it won't exist on a fresh clone. The first prompt shouldn't fail because of that.

## `test_unknown_purpose_raises`

**What it does:** Asks for purpose `"transactions"` and expects a `ValueError`.

**Why it's there:** Each purpose has its own reviewed allowlist. A typo or a new purpose must fail loudly instead of falling through to some default that sends more than intended.

## `test_spending_leaves_out_transfers_and_card_payments`

**What it does:** Adds a $5 dining purchase, a $5 credit-card payment (`LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`), a $9 student-loan payment, a $100 TRANSFER_OUT and $2,500 of INCOME. The default totals are `{"LOAN_PAYMENTS": 9.0, "FOOD_AND_DRINK": 5.0}`.

**Why it's there:** A card payment pays for purchases already counted, and a transfer moves money between the user's own accounts. Counting either doubles spending: spend $500 on a card, pay it off from checking, and the model reports $1,000. These are the same exclusions as the budget engine's `spending()` (TPC-17). A loan payment that isn't a card payment is real money out, so it stays. The review on #6 found this.

## `test_spending_income_question_reports_inflow`

**What it does:** Adds $2,500 of payroll income (stored as -250000, money in) and asks for INCOME. It expects a total of 2500.0, `direction: "inflow"`, one row with a positive amount, and `merchant: null`.

**Why it's there:** The `get_spending` tool offers INCOME and TRANSFER_IN, but the old query only read positive (money-out) rows, so asking "how much did I earn?" always got $0. An inflow question now reads negative rows and reports them as positive, with `direction` saying which way the money moved. The payer is dropped because a payroll or transfer label is often a person's or employer's name, and the amount and date answer the question.

## `test_spending_reversed_range_raises`

**What it does:** Asks for spending from 2026-11-01 to 2026-10-21 and expects a `ValueError`.

**Why it's there:** A reversed range used to return `{}`, which the model reads as "no spending". Raising lets the tool layer tell the model its dates are backwards.

## `test_bad_pseudonym_rejected`

**What it does:** Passes `pseudonym="u-jane"` (a user id, not a pseudonym) and expects a `ValueError`.

**Why it's there:** The pseudonym is written into every prompt and audit line as the "user". Only the gateway's own `user_xxxx` format is accepted, so a user id, or prompt text, can't be passed back in where a pseudonym belongs.

## `test_debt_balance_from_account`

**What it does:** Stores a card whose liability row has a $410 balance, then updates the account's balance to $450, as a later balance refresh would. The debts context reports 450.0.

**Why it's there:** `liabilities.balance_cents` is a copy taken when liabilities were last refreshed, while the account balance is refreshed on every sync. The dashboard now reads the account balance, so the agent does too, and the two always quote the same number.

## `test_audit_file_private_and_rotated`

**What it does:** Writes one audit line, lowers `PROMPT_AUDIT_MAX_BYTES` to just above the file's size, and writes a second. It checks that the file's mode is `0600`, that the current file holds only the second line, and that `a.jsonl.1` holds the first.

**Why it's there:** The audit log holds the exact text of every prompt, so it's created owner-only and opened with `O_NOFOLLOW` (a symlink planted at its path can't redirect the writes). It used to grow forever. It now rotates to a single `.1` backup once the next line would pass the limit (8 MB by default).

---

## Not covered

- **PII the scrub doesn't recognize** in an account or merchant name, such as "Mom" or "Jane Doe Photography". See `test_scrub.md`. The allowlist limits this to those two fields.
- **Concurrent writes to the audit log.** Writes in one process are serialized by a lock, which also covers rotation. Two processes (the API and a CLI script) writing at once aren't, and nothing tests that.

## Purpose `diagnose` (TPC-19)

The budget graph's `diagnose` step asks the LLM why one category is overspent. The gateway builds that prompt from the facts the budget engine computed (`budget_engine/diagnose.py`) and that category's transactions only. Shared setup: `gm(day, cents, merchant, cat)` builds an October 2026 `Txn`. `diagnose_prompt(db, tmp_path, txns)` computes the facts for a general-merchandise flag on day 20 (limit $157.50, history $150 / $148 / $152), seeds the Jane Doe user and returns the gateway and its prompt.

## `test_diagnose_allowlist`

**What it does:** The prompt's data has exactly the `DIAGNOSE_FIELDS` keys. Money is in dollars ($157.50 limit, $635 spent), and the month is named ("October 2026"). The largest purchase carries its share (0.94) and its ratio to the merchant's usual month (none for a new merchant). Each merchant has its `pattern` (`new` here). History months are named oldest first ("July 2026").

**Why it's there:** It's the allowlist rule (architecture section 7): a field added to the facts later can't leak into a prompt by accident. Dollars and month names are what a small model reads correctly.

## `test_diagnose_scrubs_merchants`

**What it does:** A merchant string containing an email address doesn't reach the prompt.

**Why it's there:** Merchant names are the one free-text field the diagnosis needs. They go through the same scrubber as chat spending.

## `test_diagnose_category_only_capped_newest_first`

**What it does:** Of 25 general-merchandise transactions and 3 food ones, the prompt carries 20 general-merchandise ones, newest first, and no food.

**Why it's there:** Minimization per purpose: a diagnosis of one category sees that category only, and a capped list keeps the prompt small.

## `test_diagnose_audited`

**What it does:** The audit log gets a `diagnose` line with the category as its only parameter and the gateway's pseudonym, and the user id appears nowhere in it.

**Why it's there:** Every prompt that leaves the system is recorded for the privacy audit, under the pseudonym only.

## `test_diagnose_unnamed_merchant`

**What it does:** A purchase with no merchant name appears as `"merchant": null` in both the merchants and the transactions.

**Why it's there:** Plaid often has no merchant name. The diagnosis must still be built, not crash in the scrubber.

## `test_diagnose_memory_keys`

**What it does:** Without memory, `this_month_alerts` and `past_advice` are empty lists. With memory, each alert carries only severity, action and status, and each piece of advice only month, action and outcome. A `message` passed in is dropped, so its text never reaches the prompt.

**Why it's there:** The agent's memory (TPC-20) goes through the same allowlist as the rest of the prompt. The model needs to know it already alerted, not what it wrote. Old messages can quote merchants and amounts.

## `test_diagnose_rows_leave_out_card_payments`

**What it does:** A LOAN_PAYMENTS flag whose transactions include a $2,000 credit-card payment and a $400 student-loan payment. The prompt's transactions list only the student loan.

**Why it's there:** The diagnose rows were filtered by category only, so a card payment that `overspend_facts` left out of "spent" was still quoted as evidence, and the rows didn't add up to the facts. The rows now go through the same `spending()` filter. The review on #12 found it.
