# Tests: `backend/tests/test_eval_runner.py`

These tests cover `eval/runner.py`. `run_once` runs one eval scenario (TPC-16) through the same agent `POST /chat` uses: `build_chat_graph(make_tools(PrivacyGateway(...), kb), retrieve=kb.search)`. The agent runs against a throwaway database seeded with the scenario's synthetic user. `run_once` records the answer, the tool calls and results, the retrieved sources and the latency. It never raises: a failed run becomes a result to score.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_eval_runner.py -v -rs
```

## Shared setup

- `write(tmp_path, edit)` from `test_eval_fixtures.py` writes the standard test scenario: a Platinum Card at 24.99%, checking and savings accounts, a purchase and a refund.
- `ScriptedChatModel`, `tool_call`, `FakeKB` and `ollama_has` come from `tests/fakes.py`. The fake model replays scripted replies; `FakeKB` returns one canned CFPB result.
- `TODAY` is 2026-10-27.

---

## `test_run_once_captures_everything`

**What it does:** The fake model calls `get_debts`, then answers. The record has:
- the final answer text;
- the tool call `{"name": "get_debts", "args": {}}`;
- a tool result containing the seeded card and its APR;
- the knowledge base's result as `sources`;
- a positive latency, no error, and the run number passed in.

The audit log has exactly one `debts` entry.

**Why it's there:** These are exactly the inputs the metrics need: answer for facts and similarity, tool results for the judge, tool calls and sources for the accuracy checks. The audit line proves the agent read through the privacy gateway, just as in the app.

## `test_run_once_model_error_recorded`

**What it does:** A model that raises `ConnectionError("down")` gives a record with `error == "ConnectionError: down"`, an empty answer and no tool calls. Nothing is raised.

**Why it's there:** One crashed run (for example Ollama dropping a request) must not end a 15-run evaluation. The CLI scores it as a failure and moves on.

## `test_runs_are_isolated`

**What it does:** Runs two scenarios in turn. The second scenario's `get_accounts` result contains its own "Gold Card" and not the first scenario's "Platinum Card".

**Why it's there:** Each run gets its own in-memory database. If state leaked between runs, a scenario could pass using another scenario's data.

## `test_live_debt_highest_apr`

**What it does:** Runs the committed `debt-highest-apr` scenario on the real `qwen2.5:14b` agent and knowledge base, then scores it with the real embeddings and the `gemma3:12b` judge. It expects no error and a parsed judge result.

**Why it's there:** It's the end-to-end check that the harness works with real models: the graph runs, the judge returns JSON we can parse, and the scorer accepts it.

**Skipped when:** Ollama is missing any of the three models, the knowledge base is empty, or the scenario file doesn't exist yet.
