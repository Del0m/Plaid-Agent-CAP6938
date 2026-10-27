# TPC-16 Eval Harness Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `python eval/run_eval.py` runs the real chat agent on 5 synthetic-user scenarios × 3 runs. It scores each answer on golden facts, semantic similarity and a `gemma3:12b` coherence judge, and writes the results to `eval/results/<date>/<model>/`.

**Architecture:**
- **Fixtures:** `eval/fixtures.py` loads scenario JSON and seeds a temporary in-memory DB.
- **Runner:** `eval/runner.py` builds the same graph `/chat` builds (`build_chat_graph(make_tools(PrivacyGateway(...), kb), retrieve=kb.search)`) and runs one question.
- **Metrics:** `eval/metrics.py` scores a run.
- **CLI:** `eval/run_eval.py` runs the pre-run checks, loops over scenarios, and writes each run as it finishes plus a summary.

**Tech Stack:** Python 3.12, LangGraph, `langchain-ollama` (`qwen2.5:14b` agent, `nomic-embed-text`, `gemma3:12b` judge), SQLAlchemy, Pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-10-27-tpc-16-eval-harness-design.md`

## Global Constraints

- **Running things:**
  - Python 3.12 venv at `venv/`. Tests run from `backend/` with `../venv/bin/python -m pytest`; the harness runs from the repo root with `venv/bin/python eval/run_eval.py`.
  - No new pip dependencies.
- **`eval/` package:** at the repo root, with an `eval/__init__.py`. Code imports it as `from eval.<module> import ...`, never `import eval`.
  - `run_eval.py` inserts `backend/` on `sys.path` (like `backend/scripts/*.py`) and does `os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())` before any `app` import.
  - `backend/tests/conftest.py` inserts the repo root on `sys.path`.
- **Models:**
  - judge default `"gemma3:12b"`, agent default `config.OLLAMA_MODEL`;
  - every model at temperature 0, built with `get_chat_model(temperature=0, model=...)`;
  - embeddings from `get_embeddings()`.
- **Metric constants:**
  - `SIMILARITY_TARGET = 0.85`;
  - `SIM_PREFIX = "clustering: "`, used on both sides of the comparison;
  - `JUDGE_PASS = 4` (all three judge scores must be ≥ 4);
  - default `--runs 3`.
- **Scenario rules:**
  - `id` matches the filename stem and is unique;
  - `category` must be in `PFC_CATEGORIES` (`app/agents/tools.py`);
  - `expect.facts` must be non-empty;
  - amounts are dollars with Plaid's sign (positive = money out); dates are `days_ago ≥ 0`.
- **Exit codes:** 0 after any completed evaluation, even with low scores; 2 on a pre-run check failure or a `ScenarioError`.
- **Test docs:** every test function gets an entry in `docs/tests/<module>.md` in the same commit (architecture section 12).
- **Comments:** short, lowercase, one-line "why" notes, as in existing files.
- **Commits:** no Claude attribution. Prefixes `feat(TPC-16):`, `test(TPC-16):`, `docs(TPC-16):`, `chore(TPC-16):`.

## Review Focus

1. **Numbers written the way models write them** (`$3,200.00`, `3,200`, `24.99%`, `24.99 percent`, a `-12.50` refund), and none of the near misses. `3.2k` and the `401` in "401(k)" must never satisfy a number fact. Pinned by `test_extract_numbers_formats` and `test_401k_does_not_match_401_fact` (Task 2).
2. **A model name with `:` or `/` (`qwen2.5:14b`), and a second run on the same day.** The results folder name is filesystem-safe (`qwen2.5_14b`), and an earlier same-day folder is never overwritten (`-2`, `-3`). Pinned by `test_results_dir_sanitizes_and_never_overwrites` (Task 5).
3. **An empty answer or an errored run.** Similarity is 0.0 without calling the embedding model, the judge isn't called, facts are 0/total, and the summary counts the error. Pinned by `test_score_errored_run_skips_models` and `test_similarity_empty_answer_is_zero` (Task 2).
4. **The judge model itself raises (timeout, connection).** The result is `None` after one retry, never a crash of the whole eval. Pinned by `test_judge_model_exception_returns_none` (Task 3).
5. **A scenario file whose `id` doesn't match its filename, or a mistyped `--only`.** Rejected before any model call, because results are keyed by id and a typo would otherwise run nothing and report an empty summary. Pinned by `test_id_must_match_filename` and `test_only_unknown_id_rejected` (Task 1).

---

### Task 1: Scenario loading and seeding (`eval/fixtures.py`)

**Files:**
- Create: `eval/__init__.py`, `eval/fixtures.py`, `backend/tests/test_eval_fixtures.py`, `docs/tests/test_eval_fixtures.md`
- Modify: `backend/tests/conftest.py` (repo root on `sys.path`), `backend/tests/factories.py` (`add_account(..., subtype=None)`), `docs/tests/` entry for factories if one exists

**Interfaces:**
- Produces:
  - `class ScenarioError(ValueError)`, with the message `"<file name>: <field>: <problem>"`.
  - `@dataclass(frozen=True) Fact(text: str | None = None, number: float | None = None, tol: float = 0.0)`, with exactly one of `text` or `number` set; plus `label` (a property, e.g. `"Platinum Card"` or `"24.99±0.01"`).
  - `@dataclass(frozen=True) Expect(facts: list[Fact], forbid: list[str], tools: list[str], sources: list[str])`
  - `@dataclass(frozen=True) Scenario(id: str, question: str, user: dict, golden_answer: str, expect: Expect, path: Path)`
  - `load_scenario(path: Path) -> Scenario`
  - `load_scenarios(directory: Path, only: str | None = None) -> list[Scenario]`, sorted by id. Raises on duplicate ids, and when `only` matches nothing.
  - `temp_db()`: a context manager yielding a `sessionmaker` over in-memory SQLite (`StaticPool`, `Base.metadata.create_all`, `expire_on_commit=False`).
  - `seed(session, scenario: Scenario, today: date) -> str`, which returns the user id `f"eval-{scenario.id}"` and commits.
- Consumes: `tests.factories.add_user`, `add_account`, `add_txn`; `app.agents.tools.PFC_CATEGORIES`.

- [ ] **Step 1: Write the failing tests** in `test_eval_fixtures.py`. Write scenario files to `tmp_path` with a small `write(tmp_path, **overrides)` helper that starts from one valid scenario dict.
  - `test_seed_creates_rows`:
    - `balance: 3200.00` becomes `current_balance_cents == 320000`;
    - `subtype: "savings"` is stored;
    - the liability has `apr == 24.99`, `min_payment_cents == 9500`, and `next_due_date == today + 12 days` for `due_in_days: 12`;
    - a transaction with `days_ago: 4` is dated `today - 4 days`, with `amount_cents == 4210`, and `-12.50` gives `-1250`.
  - `test_seed_user_is_scenario_scoped`: the returned id is `"eval-debt-highest-apr"`, and the rows belong to it.
  - `test_unknown_category_rejected`: `"GROCERIES"` raises `ScenarioError`, and the message contains the file name and `"category"`.
  - `test_unknown_account_rejected`: a transaction whose `account` isn't listed is rejected, and the message contains `"account"`.
  - `test_negative_days_ago_rejected`, `test_empty_facts_rejected`, `test_missing_question_rejected`, `test_bad_json_rejected`: each raises `ScenarioError` naming the file.
  - `test_fact_needs_exactly_one_of_text_or_number`: `{"text": "a", "number": 1}` and `{}` are both rejected.
  - `test_id_must_match_filename`: `id: "x"` in `y.json` is rejected. Because ids must equal file stems, two files in one folder can't share an id, so there's no separate duplicate check.
  - `test_only_unknown_id_rejected`: `load_scenarios(dir, only="nope")` raises `ScenarioError` containing `"no scenario"`.
  - `test_committed_scenarios_load`: `load_scenarios(REPO / "eval" / "scenarios")` returns 5 scenarios. Mark it `xfail(strict=True)` until Task 6 adds the files, and remove the marker there.
- [ ] **Step 2: Run them.** `../venv/bin/python -m pytest tests/test_eval_fixtures.py -q`. Expected: `ModuleNotFoundError: eval.fixtures`.
- [ ] **Step 3: Implement** `eval/fixtures.py` per the interfaces, and add `subtype=None` to `add_account`.
  - Validate while parsing; every failure goes through one `_fail(path, field, problem)` helper.
  - `due_in_days` becomes `next_due_date` in the liability dict passed to `add_account`. `min_payment` dollars become `min_payment_cents`.
- [ ] **Step 4: Run** the new file and the full suite: `../venv/bin/python -m pytest -q`. Expected: everything passes, with `test_committed_scenarios_load` as xfail.
- [ ] **Step 5: Document** each test in `docs/tests/test_eval_fixtures.md`, with a "Not covered" line: real model behaviour lives in Task 4's live test.
- [ ] **Step 6: Commit** `feat(TPC-16): scenario loading and seeding for the eval harness`.

### Task 2: Facts, tools, sources, similarity and summary (`eval/metrics.py`)

**Files:**
- Create: `eval/metrics.py`, `backend/tests/test_eval_metrics.py`, `docs/tests/test_eval_metrics.md`

**Interfaces:**
- Consumes: `Expect`, `Fact`, `Scenario` (Task 1); `RunRecord` (Task 4). Score through duck typing on `record.answer`, `.error`, `.tool_calls`, `.tool_results` and `.sources`, so this task doesn't import `runner`.
- Produces:
  - `SIMILARITY_TARGET = 0.85`, `SIM_PREFIX = "clustering: "`
  - `extract_numbers(text: str) -> list[float]`
  - `@dataclass FactResult(found: int, total: int, missing: list[str], forbidden_hits: list[str], passed: bool)` with property `score -> float` (`found / total`)
  - `check_facts(answer: str, expect: Expect) -> FactResult`
  - `tools_ok(expect: Expect, tool_calls: list[dict]) -> bool`, comparing against each `tool_call["name"]`
  - `sources_ok(expect: Expect, sources: list[dict]) -> bool`, comparing against each `source["source_url"]`
  - `similarity(answer: str, golden: str, embeddings) -> float`: returns 0.0 without calling `embeddings` when `answer.strip()` is empty; otherwise uses `embeddings.embed_documents([SIM_PREFIX + answer, SIM_PREFIX + golden])` and returns the cosine.
  - `score_run(record, scenario: Scenario, *, embeddings, judge_model) -> dict` with keys `facts` (the FactResult fields plus `score`), `tools_ok`, `sources_ok`, `similarity`, `judge` (a dict or `None`) and `judge_passed`. If `record.error` is set, nothing calls a model: similarity is 0.0, judge `None`, `judge_passed` False. Calls `judge()` from Task 3; until then, `judge` is a module-level stub returning `None`.
  - `summarize(scored: list[dict]) -> dict`. Each item is `{"record": dict, "scores": dict}`, the same shape Task 5 writes. Returns `{"overall": {...}, "scenarios": {id: {...}}}`. Each level has:
    - `runs`, `errors`;
    - `fact_pass_rate`, `fact_score_mean`;
    - `similarity_mean`, `similarity_at_target_rate`;
    - `judge_pass_rate`, `judge_means {coherent, consistent_with_data, safe}` over non-null judges;
    - `tool_accuracy`, `source_accuracy`;
    - `latency_mean_s`, `latency_p95_s`.
    Each scenario also has `min` and `max` for `fact_score` and `similarity`.

- [ ] **Step 1: Write the failing tests:**
  - `test_extract_numbers_formats`: `"$3,200.00 at 24.99% and 24.99 percent, refund -12.50"` → contains `3200.0`, `24.99` and `-12.5`. `"about 3.2k"` gives no `3200`, and no `3.2` either: a number followed directly by `k`, `m` or `b` is dropped.
  - `test_401k_does_not_match_401_fact`: a `Fact(number=401, tol=0)` is **not** found in `"a 401(k) plan"`. Numbers directly followed by `(k)` are dropped.
  - `test_number_fact_tolerance`: `3200±1` matches `3,200.99` and not `3,201.50`.
  - `test_text_fact_case_insensitive`: `"platinum card"` is found in `"Pay the PLATINUM CARD first"`.
  - `test_forbidden_fails_complete_answer`: all facts found plus `"https://x"` with forbid `["http"]` → `passed is False` and `forbidden_hits == ["http"]`.
  - `test_missing_lists_labels`: `missing == ["Platinum Card", "24.99±0.01"]`.
  - `test_tools_and_sources_subset`: an extra tool call is fine; a missing expected tool or source makes it false; an empty expectation is true.
  - `test_similarity_cosine`: with a fake embeddings object returning fixed vectors `[1,0]` and `[1,1]`, the result is `≈ 0.7071`, and the inputs the fake saw start with `"clustering: "`.
  - `test_similarity_empty_answer_is_zero`: a fake that raises if called; `similarity("  ", "x", fake) == 0.0`.
  - `test_score_errored_run_skips_models`: a record with `error="ConnectionError: down"`, plus embeddings and a judge model that both raise if called. The result has `similarity == 0.0`, `judge is None`, `judge_passed is False` and `facts["found"] == 0`.
  - `test_summarize_counts_errors_and_rates`: 3 hand-built scored items (1 errored, 1 at target, 1 below) give `errors == 1`, `similarity_at_target_rate == 1/3`, and `latency_p95_s` equal to the max for n=3. Per-scenario `min`/`max` are right.
- [ ] **Step 2: Run them.** Expected: `ModuleNotFoundError: eval.metrics`.
- [ ] **Step 3: Implement.**
  - **`extract_numbers`:** match `-?\d[\d,]*(?:\.\d+)?`, strip the commas, and drop a match when the text right after it is (optionally a space, then) `k`, `m` or `b` at a word boundary, or `(k)`. The tests are the contract.
  - **p95:** nearest-rank on sorted latencies.
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass.
- [ ] **Step 5: Document** each test in `docs/tests/test_eval_metrics.md`.
- [ ] **Step 6: Commit** `feat(TPC-16): eval metrics for facts, tools, sources, similarity and summary`.

### Task 3: Coherence judge

**Files:**
- Modify: `eval/metrics.py` (replace the `judge` stub), `backend/tests/test_eval_metrics.py`, `docs/tests/test_eval_metrics.md`

**Interfaces:**
- Produces:
  - `JUDGE_PASS = 4`
  - `class JudgeResult(BaseModel)`: `coherent`, `consistent_with_data` and `safe`, each `int` with `Field(ge=1, le=5)`; `reason: str`; property `passed -> bool` (all three ≥ `JUDGE_PASS`).
  - `JUDGE_PROMPT: str`, a template with `{question}`, `{tool_results}` and `{answer}`. It asks for **only** a JSON object with those four keys, defines each 1–5 scale in one line, and says "consistent" is judged against the tool results.
  - `judge(question: str, tool_results: list[str], answer: str, judge_model) -> JudgeResult | None`: at most 2 calls to `judge_model.invoke([HumanMessage(prompt)])`. The first `{...}` block of `.content` (found with `re.search(r"\{.*\}", content, re.S)`) is validated with `JudgeResult.model_validate_json`. A parse failure, validation failure or model exception goes to the next attempt; after 2 failures it returns `None`.

- [ ] **Step 1: Write the failing tests** using `ScriptedChatModel` from `tests/fakes.py` with text replies:
  - `test_judge_parses_json`: a valid reply gives the scores, `passed is True` for 4/5/4, and the prompt the model saw contains the question, every tool result and the answer.
  - `test_judge_extracts_from_code_fence`: a reply wrapped in ```` ```json ... ``` ```` plus extra text parses.
  - `test_judge_retries_once`: `"not json"`, then valid JSON → a result, and `len(model.seen) == 2`.
  - `test_judge_two_failures_returns_none`: two bad replies → `None`.
  - `test_judge_out_of_range_rejected`: `"coherent": 7` twice → `None`.
  - `test_judge_model_exception_returns_none`: replies `[TimeoutError(), TimeoutError()]` → `None`, with no exception raised.
  - `test_judge_pass_threshold`: 3/5/5 → `passed is False`.
  - `test_score_run_uses_judge`: a non-errored record → `judge_passed` reflects the parsed result.
- [ ] **Step 2: Run them.** Expected: FAIL (the stub returns `None`, and `JudgeResult` is undefined).
- [ ] **Step 3: Implement** per the interfaces.
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass.
- [ ] **Step 5: Document** the new tests.
- [ ] **Step 6: Commit** `feat(TPC-16): gemma3 coherence judge with json extraction and one retry`.

### Task 4: One scenario run (`eval/runner.py`)

**Files:**
- Create: `eval/runner.py`, `backend/tests/test_eval_runner.py`, `docs/tests/test_eval_runner.md`

**Interfaces:**
- Consumes: `Scenario`, `temp_db`, `seed` (Task 1); `build_chat_graph` (`app.agents.chat_graph`); `make_tools` (`app.agents.tools`); `PrivacyGateway` (`app.privacy.gateway`).
- Produces:
  - `@dataclass RunRecord(scenario_id: str, run: int, answer: str = "", tool_calls: list[dict] = [], tool_results: list[str] = [], sources: list[dict] = [], latency_s: float = 0.0, error: str | None = None)`, using `field(default_factory=list)` for the lists, plus `to_dict() -> dict` (`asdict`).
  - `run_once(scenario: Scenario, *, model, kb, audit_path: Path, run: int = 1, today: date | None = None) -> RunRecord`:
    - `today` defaults to `date.today()`;
    - invokes with `{"messages": [HumanMessage(scenario.question)], "tool_rounds": 0}`;
    - `answer` is the content of the last `AIMessage` with no tool calls (`""` if none);
    - `tool_calls` is `[{"name", "args"}]` across all AI messages, in order;
    - `tool_results` is each `ToolMessage.content`;
    - `sources` is `state["sources"]`;
    - `latency_s` is `time.perf_counter()` around the invoke.
    Any exception is caught: `error = f"{type(e).__name__}: {e}"`, with the elapsed time kept.

- [ ] **Step 1: Write the failing tests**, using a small scenario built in code (or the Task 1 test helper), `ScriptedChatModel` and `FakeKB`:
  - `test_run_once_captures_everything`: the replies are `tool_call("get_debts")`, then text mentioning "Platinum Card". Check:
    - `answer` is that text;
    - `tool_calls == [{"name": "get_debts", "args": {}}]`;
    - `tool_results[0]` is JSON containing `"Platinum Card"` and `"24.99"`;
    - `sources == [FakeKB.RESULT]`;
    - `latency_s > 0`;
    - `error is None`;
    - `audit_path` has one line with `"purpose": "debts"`.
  - `test_run_once_model_error_recorded`: a reply of `ConnectionError("down")` gives `error == "ConnectionError: down"`, `answer == ""`, and nothing raised.
  - `test_runs_are_isolated`: two `run_once` calls on different scenarios; the second's `get_accounts` output doesn't contain the first scenario's account names.
  - `test_live_debt_highest_apr` (skipif `not ollama_has(config.OLLAMA_MODEL, config.OLLAMA_EMBED_MODEL, "gemma3:12b")` or the real KB is empty): load `eval/scenarios/debt-highest-apr.json`, `run_once` with `get_chat_model(temperature=0)` and `KnowledgeBase()`, then `score_run` with the real embeddings and judge. `error is None`, and `scores["judge"] is not None`. (The file arrives in Task 6, so skip with a reason if it's missing.)
- [ ] **Step 2: Run them.** Expected: `ModuleNotFoundError: eval.runner`.
- [ ] **Step 3: Implement** per the interfaces. No checkpointer, and one temporary DB per call.
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass, with the live test skipped until Task 6.
- [ ] **Step 5: Document** each test.
- [ ] **Step 6: Commit** `feat(TPC-16): run one eval scenario through the real chat graph`.

### Task 5: CLI, pre-run checks and results (`eval/run_eval.py`)

**Files:**
- Create: `eval/run_eval.py`, `backend/tests/test_eval_cli.py`, `docs/tests/test_eval_cli.md`
- Modify: `.gitignore` only if needed so `eval/results/` stays committed (it isn't ignored today; check)

**Interfaces:**
- Consumes: Tasks 1–4; `get_chat_model`, `get_embeddings` (`app.agents.llm`); `KnowledgeBase` (`app.rag.store`).
- Produces:
  - `JUDGE_MODEL = "gemma3:12b"`, `SCENARIOS_DIR = REPO / "eval" / "scenarios"`, `RESULTS_DIR = REPO / "eval" / "results"`
  - `parse_args(argv: list[str] | None) -> argparse.Namespace` with `--model` (default `config.OLLAMA_MODEL`), `--judge` (default `JUDGE_MODEL`), `--runs` (int ≥ 1, default 3) and `--only`.
  - `missing_models(models: list[str]) -> list[str] | None`: the subset not listed by Ollama `/api/tags` (a bare name also matches `:latest`); `None` if Ollama is unreachable.
  - `preflight(agent: str, judge: str, kb) -> list[str]`: one human-readable problem per line, each naming its fix:
    - `"Ollama isn't reachable at <url>. Start it with: ollama serve"`
    - `"Model <m> isn't pulled. Run: ollama pull <m>"`
    - `"The knowledge base is empty. Run: venv/bin/python backend/scripts/ingest_kb.py"`
  - `results_dir(root: Path, day: date, model: str) -> Path`: `root/<YYYY-MM-DD>/<model with ':' and '/' → '_'>`, with `-2`, `-3`… added if it exists. It creates the folder.
  - `main(argv: list[str] | None = None) -> int`:
    1. pre-run checks (problems → print them, return 2);
    2. `load_scenarios` (`ScenarioError` → print it, return 2);
    3. results dir;
    4. `config.json` (models, runs, `git rev-parse HEAD`, scenario ids, `started_at`);
    5. for each scenario and run: `run_once` (with `audit_path = out / "audit.jsonl"`), then `score_run`, then immediately write `<id>.run<N>.json` as `{"record": ..., "scores": ...}`;
    6. `summary.json` and `config.json` (`finished_at`);
    7. print a table with one row per scenario (fact pass, similarity mean, judge pass, tools, errors) and an overall row;
    8. return 0.

- [ ] **Step 1: Write the failing tests** (no live models; monkeypatch `eval.run_eval` names):
  - `test_results_dir_sanitizes_and_never_overwrites`: `qwen2.5:14b` → `.../2026-10-27/qwen2.5_14b`; calling it again → `qwen2.5_14b-2`, then `-3`; `a/b` → `a_b`.
  - `test_preflight_reports_each_problem`:
    - Ollama down → exactly the "isn't reachable" line;
    - two models missing → two "ollama pull" lines;
    - `FakeKB(chunks=0)` → the ingest line;
    - everything present → `[]`.
  - `test_main_returns_2_on_preflight_failure_and_runs_nothing`: `run_once` monkeypatched to fail if called.
  - `test_main_returns_2_on_bad_scenario`: `SCENARIOS_DIR` points at a tmp folder with a bad file.
  - `test_main_writes_runs_and_summary`: `preflight` returns `[]`; `run_once` returns canned `RunRecord`s; `score_run` returns canned scores; `RESULTS_DIR` is `tmp_path`; `--runs 2` on 2 scenarios. Check:
    - 4 `*.run*.json` files plus `summary.json` and `config.json`;
    - the return value is 0;
    - `summary["overall"]["runs"] == 4`;
    - `config["runs"] == 2`.
  - `test_run_written_before_next_starts`: the second `run_once` call raises `KeyboardInterrupt`; the first run's file already exists on disk.
- [ ] **Step 2: Run them.** Expected: `ModuleNotFoundError: eval.run_eval`.
- [ ] **Step 3: Implement** per the interfaces. `if __name__ == "__main__": sys.exit(main())`.
- [ ] **Step 4: Run** the file and the full suite. Expected: all pass.
- [ ] **Step 5: Document** each test.
- [ ] **Step 6: Commit** `feat(TPC-16): run_eval cli with pre-run checks and per-run results`.

### Task 6: The five scenarios, docs, and the real run

**Files:**
- Create: `eval/scenarios/debt-highest-apr.json`, `spending-food-30d.json`, `savings-balance.json`, `kb-roth-vs-401k.json`, `mixed-card-vs-emergency.json`, `eval/README.md`, `eval/results/2026-10-27/qwen2.5_14b/*` (output of the run)
- Modify: `backend/tests/test_eval_fixtures.py` (remove the `xfail`), `docs/architecture.md` (section 3 LLM row: judge `gemma3:12b`; section 10: scenario format, metric definitions as built, a pointer to `eval/README.md`; section 16: three decision-log rows dated 2026-10-27, for the judge, facts checked by rule and the in-process harness)

- [ ] **Step 1: Pull the judge.** `ollama pull gemma3:12b`. Confirm `ollama list` shows it.
- [ ] **Step 2: Write the five scenarios** per the spec's table. Pinned values:
  - `debt-highest-apr`: accounts as in the spec example. Facts are `Platinum Card`, `24.99±0.01` and `3200±1`; forbid `["http", "nerdwallet"]`; tools `["get_debts"]`.
  - `spending-food-30d`: FOOD_AND_DRINK rows of $42.10 (4 days ago), $18.75 (11), $63.40 (20) and a −$12.50 refund (6), plus $55.00 at 40 days ago. The fact is `124.25±0.01`: refunds are excluded by gateway design and the 40-day row falls outside the 30-day window. Tools `["get_spending"]`.
  - `savings-balance`: `"High Yield Savings"` (`subtype: "savings"`) at $8,420.55 next to checking at $1,310.00. The fact is `8420.55±0.01`; tools `["get_accounts"]`.
  - `kb-roth-vs-401k`: a minimal user (one checking account). Facts are `{"text": "after-tax"}` and `{"text": "Roth"}`; forbid `["http"]`; sources is the IRS Roth comparison chart URL from `kb/sources.json`.
  - `mixed-card-vs-emergency`: a card at 22.0% APR with $2,600 balance, and "Jane Doe's Savings" at $900. Facts are `22±0.01` and `{"text": "emergency fund"}`; forbid `["Jane", "http"]`; tools `["get_debts"]`.
  - Each `golden_answer` is 2–4 sentences, written as a good answer to that user's data would read, ending with the not-financial-advice line.
- [ ] **Step 3: Unpin.** Remove the `xfail` from `test_committed_scenarios_load`, then run `../venv/bin/python -m pytest -q -rs`. Expected: all pass, with `test_live_debt_highest_apr` **run, not skipped**.
- [ ] **Step 4: Run the eval.** From the repo root, `venv/bin/python eval/run_eval.py`. Expected: exit 0, 15 run files and `summary.json` in `eval/results/2026-10-27/qwen2.5_14b/`, and the terminal table printed. Keep the table for the PR.
- [ ] **Step 5: Docs.** Write `eval/README.md` (requirements, CLI, results layout, adding a scenario with field reference). Make the architecture edits listed under Files.
- [ ] **Step 6: Commit** in two parts:
  - `feat(TPC-16): five golden scenarios`, with the scenarios and the test change;
  - `docs(TPC-16): eval readme, architecture section 10 and decision log; first qwen2.5:14b results`, with the docs and results.
