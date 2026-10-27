# Tests: `backend/tests/test_tools.py`

These tests cover `app/agents/tools.py`, the chat agent's four tools. The three data tools, `get_accounts`, `get_debts` and `get_spending`, read through the privacy gateway (TPC-12). `search_kb` (TPC-14) searches the public knowledge base instead. The checks here are about the tool layer itself:
- the model can't choose a user;
- the argument schemas are small and forgiving where they can be;
- bad arguments come back as errors the model can act on.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_tools.py -v
```

## Shared setup

- `add_user`, `add_account` and `add_txn` come from `tests/factories.py`, which is shared with `test_gateway.py`.
- `fixed_today` (autouse) pins the gateway's "today" to 2026-10-21.
- `seeded` creates user `u-1` with:
  - a checking account;
  - a credit card at 22% APR and a student loan at 5.25%;
  - a Starbucks purchase (FOOD_AND_DRINK) and a Delta flight (TRAVEL).

  It also creates user `u-2`, who owns "Their Secret Card".
- `FakeKB` (`tests/fakes.py`, shared with the chat API tests) stands in for `KnowledgeBase`. `count()` returns how many chunks it pretends to hold (1 by default), and `search(query, k)` records each call and returns one canned CFPB result. The `kb` fixture is one `FakeKB`.
- `tools` builds `u-1`'s tools the way a chat session will: `make_tools(PrivacyGateway(...), kb)`, with the audit log under `tmp_path`.

---

## `test_four_tools_with_descriptions`

**What it does:** Checks that exactly `get_accounts`, `get_debts`, `get_spending` and `search_kb` exist, and that each has a description.

**Why it's there:** The model picks a tool from its name and description alone. A tool with no description is one it can't choose well.

## `test_no_tool_accepts_a_user`

**What it does:** Checks that no tool has an argument containing "user", that `get_spending` takes exactly `category`, `start_date` and `end_date`, that `search_kb` takes exactly `query`, and that the other two take no arguments.

**Why it's there:** The tools are closures over a gateway that already knows the user. If any schema accepted a user id, a confused or prompt-injected model could read someone else's data. Pinning the exact argument set also keeps the schemas as small as an 8B model needs them.

## `test_get_accounts`

**What it does:** Calls `get_accounts` and checks that it returns the seeded user's three account names.

**Why it's there:** It's the ticket's "unit test against a seeded DB" for this tool.

## `test_get_debts_highest_apr_first`

**What it does:** Calls `get_debts` and expects the credit card at 22% before the student loan at 5.25%.

**Why it's there:** It's the done-when test for this tool, and it backs the M2 goal ("which debt first?"). Getting the list already in avalanche order means the model doesn't have to sort it.

## `test_other_users_data_hidden`

**What it does:** Checks that "Their Secret Card", which belongs to `u-2`, doesn't appear in `u-1`'s accounts or debts.

**Why it's there:** It confirms that user scoping still holds through the tool layer, not just inside the gateway.

## `test_get_spending_defaults_to_totals`

**What it does:** Calls `get_spending` with no arguments and expects `{"TRAVEL": 120.0, "FOOD_AND_DRINK": 6.5}`.

**Why it's there:** It's the done-when test for the no-argument case, and the call a model makes most often ("what did I spend this month?").

## `test_get_spending_category_and_dates`

**What it does:** Asks for `food_and_drink` in lowercase, from 2026-10-01 to 2026-10-11. It expects FOOD_AND_DRINK with a $6.50 total and the one Starbucks row.

**Why it's there:** It checks that the model's string dates are parsed, that lowercase categories are accepted (models often lowercase enums), and that a category request returns that category's rows.

## `test_unknown_category_lists_valid_ones`

**What it does:** Asks for category "groceries" and expects a `ToolException` that repeats "groceries" and lists the valid names, including FOOD_AND_DRINK.

**Why it's there:** Without this check, an unknown word would reach the gateway and come back as a $0 total. The model would report that as "you spent nothing on groceries". An error that lists the real categories lets it retry with FOOD_AND_DRINK. `call_with_tools` forwards `ToolException` text to the model on purpose (see `test_llm.md`).

## `test_bad_date_explains_format`

**What it does:** Parametrized over `start_date="10/01/2026"` and `end_date="yesterday"`. Each should raise a `ToolException` that mentions `YYYY-MM-DD`.

**Why it's there:** Models write dates in many forms. A message that states the expected format is how the model fixes its next call, whereas a raw parsing error would be hidden from it as a generic failure.

## `test_reversed_range_rejected`

**What it does:** A start date after the end date should raise a `ToolException` saying the start must be on or before the end.

**Why it's there:** A reversed range would silently return nothing, which looks like zero spending.

## `test_category_list_is_plaids`

**What it does:** Checks that `PFC_CATEGORIES` has Plaid's 16 primary categories and that the `get_spending` description includes them.

**Why it's there:** The model can only use a category name it has been shown. The list lives in the description, and the architecture's decision log says these Plaid categories are the budget categories.

## `test_every_call_is_audited`

**What it does:** Calls all three tools and checks that the audit log has one line per call, with the purposes accounts, debts and spending.

**Why it's there:** It shows that the tools reach data only through the gateway. A tool that queried the database directly would leave no audit line.

## `test_search_kb_output_shape`

**What it does:** Searches once and checks that the output is exactly `{"purpose": "kb", "results": [...]}` holding the knowledge base's results, and that the knowledge base was asked for 4.

**Why it's there:** The chat agent cites from this JSON. Pinning its shape, with only title, section, URL and text per result and no scores, keeps it small and predictable for an 8B model.

## `test_search_kb_rejects_empty_query`

**What it does:** Checks that an empty query and a whitespace-only query both raise `ToolException("query must not be empty")`.

**Why it's there:** Small models sometimes call a tool with blank arguments. A clear error lets the model retry with a real question, instead of getting four arbitrary chunks it might then cite.

## `test_search_kb_trims_long_query`

**What it does:** Sends a 5,000-character query and checks that the knowledge base receives exactly 1,000 characters.

**Why it's there:** A model may paste the whole conversation in as the query. Trimming keeps that call working, with no error to recover from, and caps what gets sent to the embedding model.

## `test_search_kb_empty_kb`

**What it does:** With a knowledge base holding 0 chunks, checks that `search_kb` raises `ToolException("knowledge base is empty")` and never searches.

**Why it's there:** If nobody has run `scripts/ingest_kb.py`, the model should hear that there is no guidance, rather than get an empty result list it could mistake for "no rules apply". The local log line tells the developer what to run.

## `test_live_model_picks_get_debts`

**What it does:** Runs the real chat graph (`build_chat_graph`, TPC-15) with all four tools (the knowledge base is a `FakeKB`) and asks "Which of my debts has the highest APR?" without naming a tool. It checks that the audit log has a `debts` call and that the answer mentions the credit card. It's skipped unless Ollama is running with `OLLAMA_MODEL` pulled.

**Why it's there:** It tests the ticket's "Learn" goal: can an 8B model choose the right tool from these names and descriptions alone? It passed 3 out of 3 runs on 2026-10-21 with `llama3.1:8b`. On 2026-10-26 it still passed with `search_kb` added as a fourth choice, and again after the default chat model became `qwen2.5:14b` (TPC-15).

---

## Not covered

- **Choosing between tools in harder questions**, such as questions that need two tools or spending phrased as "dining out". That belongs in the M2 eval harness (TPC-16).
- **Dates given as words** ("last month"). The tool rejects them with the format message; turning them into dates is left to the model.
