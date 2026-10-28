# Tests: `backend/tests/test_eval_cli.py`

These tests cover `eval/run_eval.py`, the eval harness's command line (TPC-16). It:
- checks that Ollama, the three models and the knowledge base are ready;
- loads the scenarios;
- runs each one `--runs` times and writes each run's record and scores as soon as it finishes;
- ends with `summary.json` and `config.json` in `eval/results/<date>/<model>/`.

No test here calls a real model.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_eval_cli.py -v
```

## Shared setup

- `offline` points `SCENARIOS_DIR` at a temporary folder with two scenarios (`debt-highest-apr` and `second`, written with `write` from `test_eval_fixtures.py`), and `RESULTS_DIR` at a temporary folder. It also replaces everything outside the harness:
  - `KnowledgeBase` → `FakeKB`;
  - `get_chat_model` and `get_embeddings` → placeholder objects;
  - `preflight` → no problems;
  - `score_run` → one canned score.
- `fake_run` returns a canned `RunRecord` with answer "ok" and a 1-second latency.

---

## `test_results_dir_sanitizes_and_never_overwrites`

**What it does:** Calls `results_dir` three times for `qwen2.5:14b` on the same day. It gets `2026-10-27/qwen2.5_14b`, then `qwen2.5_14b-2`, then `qwen2.5_14b-3`, and each folder is created. A model named `a/b` becomes `a_b`.

**Why it's there:** Model names contain `:` and sometimes `/`, which aren't safe in folder names everywhere. Running the eval twice in one day is normal while tuning, and it must never overwrite results the writeup might use.

## `test_preflight_reports_each_problem`

**What it does:** Checks the messages `preflight` returns:
- Ollama unreachable → a single message that says how to start it;
- two missing models and an empty knowledge base → three messages, each with its exact fix (`ollama pull <model>`, `ingest_kb.py`);
- everything ready → no messages.

**Why it's there:** A missing model would otherwise surface minutes into a run as a confusing error on every scenario. Each message names its fix so the person running it doesn't have to look anything up.

## `test_main_returns_2_on_preflight_failure_and_runs_nothing`

**What it does:** When the pre-run check reports a problem, `main` prints it, returns 2, and never runs a scenario.

**Why it's there:** Exit code 2 separates "couldn't start" from a completed run with low scores (exit 0). No model time is spent when the setup is broken.

## `test_main_returns_2_on_bad_scenario`

**What it does:** A `broken.json` in the scenarios folder makes `main` print a message naming the file and return 2, before any run.

**Why it's there:** A typo in one scenario has to stop the eval up front, not be discovered after the others have run.

## `test_main_writes_runs_and_summary`

**What it does:** Runs 2 scenarios × 2 runs with model `m:1` and checks:
- the results folder is `<date>/m_1`;
- it holds 4 run files, `summary.json` (4 runs overall) and `config.json`;
- `config.json` has the model, the judge, the run count, the scenario ids and a finish time;
- each run file holds the record and its scores.

**Why it's there:** These files are what TPC-27 and TPC-28 chart and the writeup cites, so their names and contents are pinned.

## `test_run_written_before_next_starts`

**What it does:** The second run raises `KeyboardInterrupt`. The first run's file is already on disk when the interruption escapes.

**Why it's there:** A full eval takes minutes on local models. Stopping it, or a crash, must keep every run that finished.
