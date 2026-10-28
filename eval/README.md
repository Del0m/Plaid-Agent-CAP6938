# Eval harness

Scores the chat agent on golden scenarios. Each scenario is a synthetic user with known numbers, a question, and the answer it should get. The harness runs the same graph and tools as `POST /chat`, against a throwaway in-memory database, and scores every answer three ways:

| Metric | How | Target |
|---|---|---|
| Facts | Every listed fact must appear in the answer (numbers within a tolerance), and no forbidden string may | report pass % |
| Similarity | Cosine similarity of `nomic-embed-text` embeddings of the answer and the golden answer | ≥ 0.85 |
| Judge | `gemma3:12b` scores coherent / consistent with the user's data / safe from 1 to 5; all three must be ≥ 4 | 85% pass |

It also reports whether the expected tools were called, whether the expected sources were retrieved, latency, and errors. Design: `docs/superpowers/specs/2026-10-27-tpc-16-eval-harness-design.md`; architecture section 10.

## Before you run it

- Ollama is running, with three models pulled: the agent model, `nomic-embed-text`, and the judge.

  ```bash
  ollama pull qwen2.5:14b && ollama pull nomic-embed-text && ollama pull gemma3:12b
  ```

- The knowledge base is ingested: `venv/bin/python backend/scripts/ingest_kb.py`.

The harness checks all of this first. If anything is missing, it prints the fix and exits with code 2.

## Running it

From the repo root:

```bash
venv/bin/python eval/run_eval.py                          # every scenario, 3 runs each, on OLLAMA_MODEL
venv/bin/python eval/run_eval.py --model llama3.1:8b      # another agent model
venv/bin/python eval/run_eval.py --only debt-highest-apr --runs 1
```

| Flag | Default | |
|---|---|---|
| `--model` | `OLLAMA_MODEL` (`qwen2.5:14b`) | agent model |
| `--judge` | `gemma3:12b` | judge model; keep it outside the agent models being compared |
| `--runs` | 3 | runs per scenario, all at temperature 0 |
| `--only` | | one scenario id |

The exit code is 0 whenever the eval completes, even with low scores. Low scores are a result, not an error.

## Results

`eval/results/<YYYY-MM-DD>/<model>/` (`:` and `/` become `_`). A second run on the same day goes to `<model>-2`, then `-3`, and so on. Results are committed so progress can be charted.

| File | Contents |
|---|---|
| `<id>.run<N>.json` | `record`: answer, tool calls, tool results, sources, latency, error. `scores`: facts (found, missing, forbidden hits), tools, sources, similarity, judge. Written as soon as the run finishes |
| `summary.json` | For each scenario and overall: pass rates, mean similarity and the share of runs at target, judge means, tool and source accuracy, mean and p95 latency, errors, and the min/max across runs |
| `config.json` | Models, run count, git commit, scenario ids, start and finish times |
| `audit.jsonl` | Every prompt the privacy gateway built during the eval |

## Adding a scenario

Create `eval/scenarios/<id>.json`. The `id` must equal the file name.

```json
{
  "id": "debt-highest-apr",
  "question": "Which of my debts should I pay off first?",
  "user": {
    "accounts": [
      {"name": "Everyday Checking", "type": "depository", "subtype": "checking", "balance": 2150.00},
      {"name": "Platinum Card", "type": "credit", "balance": 3200.00,
       "liability": {"kind": "credit", "apr": 24.99, "min_payment": 95.00, "due_in_days": 12}}
    ],
    "transactions": [
      {"account": "Everyday Checking", "days_ago": 4, "amount": 42.10,
       "category": "FOOD_AND_DRINK", "merchant": "Chipotle"}
    ]
  },
  "golden_answer": "Pay the Platinum Card first: at 24.99% APR it costs the most in interest. ...",
  "expect": {
    "facts":   [{"text": "Platinum Card"}, {"number": 24.99, "tol": 0.01}],
    "forbid":  ["http"],
    "tools":   ["get_debts"],
    "sources": []
  }
}
```

| Field | Rules |
|---|---|
| `accounts[]` | `name`, `type` (a Plaid account type) and `balance` in dollars are required. `subtype` and `liability` are optional |
| `liability` | `kind` (`credit`, `student`, `mortgage`) and `apr`; optional `min_payment` (dollars) and `due_in_days` |
| `transactions[]` | `account` must name an account above. `amount` is in dollars with Plaid's sign: **positive = money out**, negative = money in or a refund. `category` must be a Plaid primary category (`PFC_CATEGORIES` in `backend/app/agents/tools.py`). `days_ago` ≥ 0; `merchant` is optional |
| `expect.facts` | At least one fact. Each is `{"text": ...}` (case-insensitive substring) or `{"number": ..., "tol": ...}` |
| `expect.forbid` | Strings that must not appear, case-insensitive |
| `expect.tools` | Tools that must have been called. Reported, not part of facts pass/fail |
| `expect.sources` | `source_url`s that must be among the retrieved guidance |

Tips:
- Keep a number fact to a figure the tools return directly. The design keeps arithmetic out of the model, so a fact the model would have to compute tests something else.
- Remember the gateway's rules when choosing expected numbers. Spending totals count money out only, so refunds are excluded, and the default window is the last 30 days.
- Run `cd backend && ../venv/bin/python -m pytest tests/test_eval_fixtures.py` after adding a file; `test_committed_scenarios_load` checks every committed scenario parses (update its count).

## Budget accuracy

A second, separate eval: how close is a user's **first budget** to what they actually spend the next month (architecture 6.3 and 10.2)? It makes no LLM calls and needs no Ollama, so it runs in seconds. From the repo root:

```bash
venv/bin/python eval/budget_accuracy.py
```

For each synthetic user in `eval/budget_users.py`, it:
- seeds a throwaway database;
- runs the real `initialize_budget` and prints the stored budget (limits, outlier months, skipped categories), so you can check it looks sensible;
- fits May-July with the **median** and with the **mean** and scores each against August.

The scores per user and pooled are:
- **MAPE** over lines whose category had August spending;
- **zero-actual lines**, which got a limit but had no spending, so a percentage can't score them;
- **unbudgeted** categories, which had spending but no line.

Results go to `eval/results/<YYYY-MM-DD>/budget_accuracy/results.json`, with `-2` for a second run the same day.

### Adding a budget user

Add a `Profile` to `PROFILES` in `eval/budget_users.py`. A profile is a few patterns:
- `Weekly(category, merchant, cents, jitter, weekday, growth)`;
- `Monthly(category, merchant, cents, day, moved)`, where `moved` pays one month on a different date;
- `Once(category, merchant, cents, day)`.

Each profile has its own seed, so its transactions never change between runs. Use Plaid primary categories. Include `BASE` so the user has income and a transfer to filter out. Then add a test to `backend/tests/test_budget_users.py` checking that the planted fact really is in the generated transactions, and document it in `docs/tests/test_budget_users.md`.
