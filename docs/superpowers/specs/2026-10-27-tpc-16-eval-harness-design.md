# TPC-16: Eval harness skeleton with 5 golden scenarios

Jira: TPC-16 (epic TPC-3, M2). Branch: `TPC-16`, cut from `TPC-15` (rebase onto `main` once PRs #5-#9 merge).

## Goal

The first version of the benchmark that M5 grows to 30-50 scenarios and reports in the writeup (architecture section 10). It has to be easy to extend, not complete. It runs the real chat agent against synthetic users with known right answers, scores every answer three ways, and saves the results so progress can be charted.

**Done when:** `python eval/run_eval.py` scores 5 scenarios end to end and writes `eval/results/<date>/<model>/`.

**Out of scope:** the 30-50 scenarios and model-vs-model runs (TPC-26, TPC-27); charts (TPC-28); budget-agent and forecast metrics (M3); running through the HTTP API.

## Decisions

| Decision | Why |
|---|---|
| Judge model is **`gemma3:12b`** (Ollama), temperature 0 | `qwen2.5:14b` is now the chat model, and `llama3.1:8b` and `mistral` are the comparison agents. A fourth model family grades all three without grading itself. It's about 8 GB, so it fits next to `qwen2.5:14b` in 24 GB, and everything stays local and free |
| Golden match is a **rule check on listed facts**, not an LLM verdict | Deterministic and free, and it fails loudly on a wrong number. The judge covers how the answer reads |
| **In-process harness**: build the same graph `/chat` builds, against a temporary DB per scenario | No server, and synthetic users are possible (the API only knows the one dev user and `dev.db`). It tests the real agent code, and `--model` swaps the model |
| Scores are reported, not asserted: the harness is a script, not part of `pytest` | Evals produce numbers for a report and take minutes on real models. The harness's own code is unit-tested with fakes |
| Amounts in dollars and dates as `days_ago` in scenario files | Readable by hand, and a scenario never goes stale, so nothing fakes today's date |
| Results are committed | The writeup charts runs across dates and model versions |

## Layout

```
eval/
├── README.md             how to run it and how to add a scenario
├── scenarios/*.json      one file per scenario
├── fixtures.py           scenario "user" block → rows in a temporary DB
├── runner.py             one scenario × one run → a RunRecord
├── metrics.py            facts, similarity, coherence judge, summary
├── run_eval.py           CLI
└── results/<YYYY-MM-DD>/<model>/
```

`eval/` sits at the repo root (architecture section 13). `run_eval.py` puts `backend/` on `sys.path`, the same way `backend/scripts/*.py` do, so it runs from any directory. It also sets a throwaway `FERNET_KEY` if none is set, the same as `tests/conftest.py`, because `app.security` fails at import without one and the eval never decrypts anything.

## Scenario format

```json
{
  "id": "debt-highest-apr",
  "question": "Which of my debts should I pay off first?",
  "user": {
    "accounts": [
      {"name": "Everyday Checking", "type": "depository", "subtype": "checking", "balance": 2150.00},
      {"name": "Platinum Card", "type": "credit", "balance": 3200.00,
       "liability": {"kind": "credit", "apr": 24.99, "min_payment": 95.00, "due_in_days": 12}},
      {"name": "Student Loan", "type": "loan", "balance": 18400.00,
       "liability": {"kind": "student", "apr": 5.5, "min_payment": 210.00}}
    ],
    "transactions": [
      {"account": "Everyday Checking", "days_ago": 4, "amount": 42.10,
       "category": "FOOD_AND_DRINK", "merchant": "Chipotle"}
    ]
  },
  "golden_answer": "Pay the Platinum Card first: at 24.99% APR it costs the most in interest. ...",
  "expect": {
    "facts":  [{"text": "Platinum Card"}, {"number": 24.99, "tol": 0.01}, {"number": 3200, "tol": 1}],
    "forbid": ["http", "nerdwallet"],
    "tools":  ["get_debts"],
    "sources": []
  }
}
```

- `accounts[].type` is a Plaid account type. `subtype`, `liability` and `liability.due_in_days` are optional.
- Transaction `amount` uses Plaid's sign: positive means money out (architecture section 5).
- `category` must be a Plaid primary category (`PFC_CATEGORIES` in `agents/tools.py`). `account` must name an account in the same file.
- `expect.facts` is required and must not be empty; text facts are case-insensitive substrings. `expect.forbid`, `expect.tools` and `expect.sources` are optional. `sources` lists `source_url`s that must be among the retrieved sources.

## `eval/fixtures.py`

- `load_scenario(path) -> Scenario` parses and validates a file into a dataclass. Anything wrong raises `ScenarioError("<file>: <field>: <problem>")`: bad JSON, a missing required field, an unknown category, an unknown account name, `days_ago < 0`, or an empty `facts` list. A typo can never become a silent 0.
- `seed(session, scenario, today) -> user_id` creates one user (id `eval-<scenario id>`) with one Plaid item. It then adds the accounts, liabilities and transactions, using the row builders in `backend/tests/factories.py` (`add_user`, `add_account`, `add_txn`). `add_account` gains optional `subtype` and liability `next_due_date` arguments; existing tests are unaffected. Dollars become integer cents with `round(x * 100)`, and dates are `today - days_ago`.
- `temp_db()` is a context manager that yields a sessionmaker over a fresh in-memory SQLite database with the app's tables. It's the same setup as the `db` fixture in `tests/conftest.py`.

## `eval/runner.py`

`run_once(scenario, *, model, kb, audit_path) -> RunRecord` runs one scenario once:

1. Create a temporary DB and `seed()` it.
2. Build `PrivacyGateway(session, user_id, audit_path=audit_path)` and `make_tools(gateway, kb)`. Then build `build_chat_graph(tools, retrieve=kb.search, model=model)`; no checkpointer is needed for a single question.
3. `invoke` the graph with `{"messages": [HumanMessage(question)], "tool_rounds": 0}` and time it.
4. Collect the final AI message's text, the tool calls in order (name and args), the tool results' text, and the retrieved `sources`.

`RunRecord` holds `scenario_id`, `run`, `answer`, `tool_calls`, `tool_results`, `sources`, `latency_s` and `error`. If anything raises, the record has `error = "<ExceptionType>: <message>"`, empty outputs, and the elapsed time. The exception is not re-raised.

## `eval/metrics.py`

- **Facts.** `check_facts(answer, expect) -> FactResult(found, total, missing, forbidden_hits, passed)`.
  - Numbers are pulled from the answer with a regex: `$`, `%` and thousands commas are stripped, and a decimal point is allowed. `3.2k` isn't treated as 3200.
  - A number fact matches if any extracted number is within `tol`. A text fact matches as a case-insensitive substring.
  - `passed` means every fact was found and no forbidden string appears.
- **Tools.** `tools_ok = set(expect.tools) ⊆ {names actually called}`. Reported separately, not part of `passed`.
- **Sources.** `sources_ok = set(expect.sources) ⊆ {source_url of retrieved sources}`. Reported separately.
- **Similarity.** `similarity(answer, golden, embeddings) -> float`: the cosine of the two vectors. Both texts get the same nomic task prefix, `clustering: `, because this is a symmetric comparison and not a search. Uses `get_embeddings()`. Target ≥ 0.85.
- **Coherence.** `judge(question, tool_results, answer, judge_model) -> JudgeResult | None`.
  - The prompt asks for JSON only: `{"coherent": 1-5, "consistent_with_data": 1-5, "safe": 1-5, "reason": "..."}`. "Consistent" is judged against the tool results the agent got, so the judge knows the user's real numbers.
  - The first `{...}` block in the reply is parsed with a Pydantic model, because local models often wrap JSON in a code fence. If parsing fails, it retries once; if that fails too, it returns `None`.
  - `passed` means all three scores are ≥ 4. A `None` result or an errored run counts as not passing. The proposal's target is 85% passing.
- **Summary.** `summarize(scored_runs) -> dict`, per scenario and overall:
  - fact pass rate and mean fact score;
  - mean similarity and the share of runs ≥ 0.85;
  - judge pass rate and the mean of each judge score;
  - tool and source accuracy;
  - mean and p95 latency;
  - error count.
  Spread is the min and max across runs for each scenario.

## `eval/run_eval.py`

```
python eval/run_eval.py [--model qwen2.5:14b] [--runs 3] [--only debt-highest-apr] [--judge gemma3:12b]
```

1. **Preflight.**
   - Ollama must answer `/api/tags`, and the agent model, `nomic-embed-text` and the judge must all be pulled. A bare name also matches `<name>:latest`.
   - The knowledge base must not be empty.
   - On failure, exit with code 2 and the fix (`ollama pull gemma3:12b`, `../venv/bin/python backend/scripts/ingest_kb.py`).
2. **Load scenarios.** Load every `eval/scenarios/*.json`, or the one named by `--only`. A `ScenarioError` stops the run before any model call.
3. **Run and score.** For each scenario and each run (temperature 0): `run_once`, then score. Write `<scenario_id>.run<N>.json` (the run record plus all scores) as soon as each finishes, so a crash keeps earlier runs.
4. **Summarize.**
   - Write `summary.json` and `config.json` to the results folder. `config.json` records the models, the run count, the git commit, the scenario ids and the start and end times.
   - Each run's prompt audit log goes to `audit.jsonl` in the same folder.
   - Print a short table to the terminal.
   - Exit with code 0 even when scores are low: low scores are a result, not a failure.

## The five scenarios

| id | Question | What it tests |
|---|---|---|
| `debt-highest-apr` | Which of my debts should I pay off first? | M2's done-when question: `get_debts`, the right card, its APR and balance |
| `spending-food-30d` | How much did I spend on food and drink in the last 30 days? | `get_spending` with a category, and the exact total. One refund row and one 40-days-ago row must not change it |
| `savings-balance` | How much is in my savings account? | `get_accounts` and one exact balance. Kept to a single number on purpose: a sum would test the model's arithmetic, which the design avoids |
| `kb-roth-vs-401k` | What's the difference between a Roth IRA and a 401(k)? | Guidance only: facts like "after-tax", no URLs in the answer, and `sources` includes the IRS Roth comparison chart |
| `mixed-card-vs-emergency` | Should I pay down my credit card or build an emergency fund first? | User data plus guidance. One account is called "Jane Doe's Savings"; `forbid` includes "Jane", which checks the privacy scrub end to end |

## Tests (pytest, no live models unless noted)

Tests go in `backend/tests/` and import `eval` with the repo root on `sys.path`, added in `tests/conftest.py`. Each module gets a `docs/tests/<module>.md`.

- **`test_eval_metrics.py`:**
  - Number extraction: `$3,200.00`, `24.99%`, negatives; `3.2k` is not 3200.
  - Tolerance edges, case-insensitive text facts, and forbidden hits failing an otherwise complete answer.
  - Tools and sources subset checks.
  - Cosine similarity with a fake embeddings object.
  - Judge parsing: valid JSON, JSON wrapped in a code fence or extra text (the first `{...}` block is used), a retry that succeeds, two failures returning `None`, and the pass threshold.
  - `summarize` with errored runs.
- **`test_eval_fixtures.py`:**
  - A scenario becomes the expected rows: cents, `days_ago` dates and liabilities.
  - Each kind of `ScenarioError` names the file and field.
  - All five committed scenario files load.
- **`test_eval_runner.py`:**
  - One end-to-end run with `ScriptedChatModel` (`tests/fakes.py`) and a fake knowledge base. It checks the captured answer, tool calls, tool results and sources, and that the audit log was written.
  - A model that raises gives `error`, not an exception.
- **Live test** (skipped unless Ollama has `qwen2.5:14b`, `nomic-embed-text` and `gemma3:12b`): one run of `debt-highest-apr` produces a scored record with no `error`.

## Docs

- `eval/README.md`: preflight requirements, the CLI, the results layout and how to add a scenario.
- Architecture section 10: the scenario format, the metric definitions as built, and the judge model.
- Section 16 (decision log): `gemma3:12b` as judge; facts checked by rule; the in-process harness.
- Section 3 LLM row: the judge is `gemma3:12b`.
- The TPC-16 Jira text still names `qwen2.5:14b` as judge; the PR says it changed.

## Verification

- `cd backend && ../venv/bin/python -m pytest -q -rs` passes, with the live eval test run, not skipped.
- `python eval/run_eval.py` scores all 5 scenarios × 3 runs on `qwen2.5:14b` and writes `eval/results/2026-10-27/qwen2.5_14b/`. The PR description includes the summary table.

## Risks

| Risk | Mitigation |
|---|---|
| `gemma3:12b` and `qwen2.5:14b` together are tight in 24 GB | Ollama unloads idle models. If both don't fit, judge after all agent runs (one model loaded at a time) |
| The judge is noisy | Temperature 0, 3 runs, scores reported with spread, and facts checked by rule independently |
| The package name `eval` reads like the builtin | It's never imported as a bare `import eval` into code that uses the builtin. Imports are `from eval.metrics import ...` |
| Five scenarios are too few to draw conclusions | Expected. This ticket is the skeleton; TPC-26 grows the set |
