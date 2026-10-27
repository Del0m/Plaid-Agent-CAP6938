# Tests: `backend/tests/test_eval_metrics.py`

These tests cover `eval/metrics.py`, which scores one eval run (TPC-16) three ways:
- a **rule check** on the facts the scenario lists (numbers within a tolerance, or case-insensitive text), plus forbidden strings;
- **semantic similarity** between the answer and the golden answer;
- a **coherence judge** (`gemma3:12b`).

It also checks whether the expected tools were called and the expected sources retrieved, and it summarizes many runs. The facts check is deterministic on purpose, so a wrong number fails loudly however fluent the answer is.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_eval_metrics.py -v
```

## Shared setup

- `expect(...)`, `scenario(...)` and `record(...)` build an `Expect`, a `Scenario` and a stand-in for a runner `RunRecord`. `score_run` only reads the record's attributes, so a `SimpleNamespace` is enough.
- `FakeEmbeddings(vectors)` returns preset vectors and records the texts it was asked to embed.
- `Explodes` fails the test if anything on it is used. It stands in for a model that must not be called.
- `scored(...)` builds one `{"record", "scores"}` item in the shape `run_eval.py` writes, for `summarize`.

---

## `test_extract_numbers_formats`

**What it does:** Pulls `3200`, `24.99` and `-12.5` out of "$3,200.00 at 24.99% and 24.99 percent, refund -12.50". It also checks that "about 3.2k" yields neither 3200 nor 3.2.

**Why it's there:** Models write money and rates many ways. The fact check has to see through `$`, `%` and thousands commas, and it must not count an abbreviated number as matching, so a vague "3.2k" never passes for "$3,200".

## `test_401k_does_not_match_401_fact`

**What it does:** A number fact of 401 is *not* found in "put it in a 401(k) plan".

**Why it's there:** "401(k)" shows up in every retirement answer. If it counted as the number 401, a scenario with a 401 fact would pass by accident.

## `test_number_fact_tolerance`

**What it does:** A fact of 3200 ± 1 matches "$3,200.99" but not "$3,201.50".

**Why it's there:** It pins the tolerance edge, so a scenario author knows exactly what a `tol` allows.

## `test_text_fact_case_insensitive`

**What it does:** "platinum card" is found in "Pay the PLATINUM CARD first".

**Why it's there:** Capitalization isn't something the benchmark should punish.

## `test_forbidden_fails_complete_answer`

**What it does:** An answer with every fact but also a URL, when `http` is forbidden, has 2/2 facts found but `passed` false, with `forbidden_hits == ["http"]`.

**Why it's there:** The chat prompt forbids URLs because local models invent them (spec amendment A1 in TPC-15). The benchmark has to catch that even when the numbers are right.

## `test_missing_lists_labels`

**What it does:** When nothing matches, `missing` lists the facts' labels ("Platinum Card", "24.99±0.01") and the score is 0.

**Why it's there:** The per-run result file has to say *which* fact was missed, not just how many, so a failing scenario can be diagnosed from the results alone.

## `test_tools_and_sources_subset`

**What it does:** Checks that:
- an extra tool call doesn't fail `tools_ok`;
- a missing expected tool does;
- an empty expectation passes.

The same three rules apply to `sources_ok` with `source_url`s.

**Why it's there:** Tool choice and retrieval are reported next to the facts, so it's clear whether a wrong answer came from the wrong tool, the wrong guidance, or the model's own wording.

## `test_similarity_cosine`

**What it does:** With vectors [1,0] and [1,1], similarity is 1/√2. Both texts are sent with the `clustering: ` prefix.

**Why it's there:** It pins the cosine maths and the nomic task prefix. The search prefixes are asymmetric and would skew a symmetric comparison.

## `test_similarity_empty_answer_is_zero`

**What it does:** A blank answer scores 0.0 without calling the embedding model.

**Why it's there:** An empty answer has nothing to compare, and some embedding servers error on empty input. Scoring it 0 keeps one bad run from crashing the eval.

## `test_score_errored_run_skips_models`

**What it does:** Scoring a run whose `error` is set gives similarity 0.0, no judge result, `judge_passed` false and 0 facts found. Neither model is called.

**Why it's there:** An errored run should cost nothing extra and count as a failure everywhere, not be skipped. Skipping it would quietly inflate the pass rates.

## `test_summarize_counts_errors_and_rates`

**What it does:** Summarizes three runs (one errored, one at the similarity target, one below) and checks:
- the run and error counts;
- the fact, judge, similarity-at-target and tool rates;
- the judge score means over non-null judges;
- mean latency and p95 latency (nearest rank, so the max for 3 runs);
- the per-scenario min and max of fact score and similarity.

**Why it's there:** The summary is what goes in the writeup, so its arithmetic is pinned here rather than trusted.

---

## Coherence judge

`judge()` sends `gemma3:12b` the question, the tool results the agent got and the answer. The judge replies with JSON scoring `coherent`, `consistent_with_data` and `safe` from 1 to 5. A run passes if all three are at least `JUDGE_PASS` (4). These tests use `ScriptedChatModel` from `tests/fakes.py` in place of the judge. `judge_with(*replies)` runs one judgement and returns the result and the fake model.

## `test_judge_parses_json`

**What it does:** A valid reply gives scores 4/5/4, which pass. The prompt the judge saw contains the question, the tool result and the answer.

**Why it's there:** "Consistent with data" only means something if the judge sees the same numbers the agent saw.

## `test_judge_extracts_from_code_fence`

**What it does:** A reply that wraps the JSON in a code fence with chatty text around it still parses.

**Why it's there:** Local models often add a fence or a preamble even when told not to. Rejecting those would score many good answers as "no verdict".

## `test_judge_retries_once`

**What it does:** "not json" followed by valid JSON gives a result after exactly 2 calls.

**Why it's there:** One malformed reply is common and cheap to retry.

## `test_judge_two_failures_returns_none`

**What it does:** Two unparseable replies give `None` after exactly 2 calls.

**Why it's there:** It caps the cost. A judge that never produces JSON can't stall the eval, and the run counts as not passing.

## `test_judge_out_of_range_rejected`

**What it does:** `"coherent": 7`, twice, gives `None`.

**Why it's there:** A score outside 1-5 means the judge misread the rubric. Accepting it would distort the means.

## `test_judge_model_exception_returns_none`

**What it does:** A judge model that raises `TimeoutError` twice gives `None`, with no exception escaping.

**Why it's there:** A slow or crashed judge model must not abort a 15-run evaluation partway through.

## `test_judge_pass_threshold`

**What it does:** Scores of 3/5/5 parse but don't pass.

**Why it's there:** It pins the rule that every one of the three scores must be at least 4. A high average can't hide one weak dimension.

## `test_score_run_uses_judge`

**What it does:** Scoring a normal run calls the judge and stores its scores, with `judge_passed` true.

**Why it's there:** It connects the judge to `score_run`, the function the CLI calls.
