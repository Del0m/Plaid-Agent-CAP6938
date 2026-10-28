# Tests: `backend/tests/test_eval_fixtures.py`

These tests cover `eval/fixtures.py`. It loads a scenario file (TPC-16), validates it, and seeds a throwaway in-memory database with the scenario's synthetic user, so the eval harness can run the real chat agent against known data. A scenario with a typo must fail loudly instead of scoring a silent 0.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_eval_fixtures.py -v
```

## Shared setup

- `tests/conftest.py` puts the repo root on `sys.path`, so `eval` imports from here.
- `scenario_dict()` builds one valid scenario. It has a checking account, a savings account (`subtype: savings`), and a credit card with a liability (24.99% APR, $95 minimum, due in 12 days). It also has a $42.10 purchase and a −$12.50 refund.
- `write(tmp_path, edit=..., name=..., raw=...)` writes it as `<id>.json`. `edit` changes it first, `name` overrides the file name, and `raw` writes text that isn't JSON.
- `rejected(path, *fragments)` asserts that `load_scenario` raises `ScenarioError` and that the message names the file and contains each fragment.
- `TODAY` is fixed at 2026-10-27, so dates from `days_ago` are exact.
- `seed` reuses `add_user`, `add_account` and `add_txn` from `tests/factories.py`. `add_account` gained an optional `subtype` for this.

---

## `test_seed_creates_rows`

**What it does:** Seeds the scenario and checks the rows:
- dollar balances become integer cents ($3,200 → 320000);
- `subtype: savings` is stored;
- the card's liability has its APR, a $95 minimum stored as 9500, and a due date of today + 12 days, and it belongs to the card's account;
- the purchase and refund are dated `today - days_ago`, keep Plaid's sign (4210 and −1250), and keep their category and merchant.

**Why it's there:** Every metric depends on the seeded numbers being exactly the ones the scenario author wrote. A rounding or sign slip here would make every downstream score wrong.

## `test_seed_user_is_scenario_scoped`

**What it does:** Checks that `seed` returns `eval-debt-highest-apr`, that it's the only user, and that every account belongs to it.

**Why it's there:** The agent's tools read through a gateway built for one user id. The id has to be the one the rows were written under, or the agent sees an empty account list.

## `test_unknown_category_rejected`

**What it does:** A transaction with category `GROCERIES` is rejected, and the message names the file and `category`.

**Why it's there:** `get_spending` only accepts Plaid's primary categories. A made-up category would seed rows the agent can never ask about, and the scenario would fail for a reason that has nothing to do with the model.

## `test_unknown_account_rejected`

**What it does:** A transaction pointing at an account name that isn't listed is rejected, naming `account`.

**Why it's there:** Without it, a typo in an account name would crash in the middle of seeding, or attach the transaction to nothing.

## `test_negative_days_ago_rejected`

**What it does:** `days_ago: -1` is rejected, naming `days_ago`.

**Why it's there:** A future-dated transaction falls outside every spending window and silently changes the expected total.

## `test_empty_facts_rejected`

**What it does:** `expect.facts: []` is rejected.

**Why it's there:** With no facts, every answer would "pass" the golden-answer check.

## `test_missing_question_rejected`

**What it does:** A file with no `question` is rejected, naming `question`.

**Why it's there:** It confirms required fields are checked before anything runs.

## `test_bad_json_rejected`

**What it does:** A file containing `{not json` is rejected with a message naming the file and `json`.

**Why it's there:** A raw `JSONDecodeError` wouldn't say which of the scenario files is broken.

## `test_fact_needs_exactly_one_of_text_or_number`

**What it does:** A fact with both `text` and `number`, and an empty fact, are both rejected (parametrized).

**Why it's there:** The checker treats text facts and number facts differently. A fact that's both, or neither, has no single meaning.

## `test_id_must_match_filename`

**What it does:** A file named `other.json` whose `id` is `debt-highest-apr` is rejected, naming `id`.

**Why it's there:** Results are keyed by scenario id. Requiring the id to equal the file name also makes duplicate ids impossible within one folder.

## `test_only_unknown_id_rejected`

**What it does:** `load_scenarios(dir, only="nope")` raises `ScenarioError` with "no scenario".

**Why it's there:** A mistyped `--only` would otherwise run nothing and write an empty summary that looks like a result.

## `test_committed_scenarios_load`

**What it does:** Loads every file in `eval/scenarios/` and expects 5.

**Why it's there:** It catches a broken committed scenario in the normal test run, before anyone spends minutes of model time on an eval.

## Not covered

- How a real model behaves on these rows. That's `test_live_debt_highest_apr` in `test_eval_runner.py`.

---

## Added after the final review

## `test_empty_text_fact_rejected`

**What it does:** `{"text": ""}` is rejected, naming `facts`.

**Why it's there:** An empty string is a substring of every answer, so the fact would always pass.

## `test_forbid_must_be_list_of_strings`

**What it does:** `forbid` given as the bare string `"http"`, as `[""]`, or as `[3]` is rejected (parametrized). `tools` and `sources` follow the same rule.

**Why it's there:** A bare string would be split into single characters, so almost every answer would hit a "forbidden" letter. An empty string would fail every answer.

## `test_non_numeric_number_fact_rejected`

**What it does:** `{"number": "abc"}` raises `ScenarioError` naming `facts`, not a bare `ValueError`.

**Why it's there:** `run_eval.py` reports `ScenarioError`s cleanly and exits 2. Any other exception would surface as a traceback.

## `test_liability_without_kind_rejected`

**What it does:** A liability with no `kind` is rejected at load time, naming `kind`.

**Why it's there:** Otherwise seeding fails inside every run, so each run is recorded as an error instead of the scenario being rejected once, up front.

## `test_text_facts_not_in_question`

**What it does:** For every committed scenario, no text fact appears in the scenario's own question.

**Why it's there:** An answer that just repeats the question would pass such a fact. The final review found two ("Roth" and "emergency fund"). They were replaced with facts only a real answer contains: "tax-free", and the user's $900 savings balance.
