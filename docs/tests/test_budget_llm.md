# Tests: `backend/tests/test_budget_llm.py`

These tests cover `app/agents/budget_llm.py`, the budget graph's two LLM calls (TPC-19):
- **`Diagnosis`:** the cause of one overspend (`one_time`, `seasonal` or `recurring`) with a one-sentence piece of evidence.
- **`Action`:** what to do about it (`alert`, `suggest_cuts`, `adjust_next_budget` or `no_op`) with a short message for the user.

`invoke_structured` binds Ollama's `format` to the model's JSON schema, which constrains the output. Pydantic still checks the reply, and a bad reply gets exactly one retry before the call gives up and the graph falls back to a plain alert.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_budget_llm.py -v
```

## Shared setup

- `model(*replies)` builds a `ScriptedChatModel` (`tests/fakes.py`) that replies with the given strings in order, or raises a given exception. `kwargs_seen` (added for TPC-19) records each call's keyword arguments, so a test can see the bound `format`.
- `GOOD` is a valid `one_time` diagnosis as JSON.

---

## `test_valid_reply_parsed`

**What it does:** A valid reply comes back as a `Diagnosis`, and the call bound `format` to `Diagnosis`'s JSON schema.

**Why it's there:** The schema is what makes a local model's output dependable, so the test checks it's actually sent, not just that parsing works.

## `test_invalid_then_valid_retries`

**What it does:** A first reply that isn't JSON is retried, and the second reply is used.

**Why it's there:** Small models occasionally produce a bad answer. One retry usually recovers without the user noticing.

## `test_two_invalid_gives_none`

**What it does:** Two replies that don't match the schema (`{}`) return `None` after exactly 2 calls.

**Why it's there:** The retry is bounded, so a model stuck on bad output can't loop. `None` tells the graph to use its fallback.

## `test_fenced_json_parses`

**What it does:** A valid object wrapped in a ```` ```json ```` fence parses on the first call.

**Why it's there:** Models often fence their JSON even when asked not to. Treating that as invalid would waste the retry.

## `test_empty_text_is_invalid`

**What it does:** A diagnosis with empty evidence counts as invalid, and the retry's reply is used.

**Why it's there:** An empty message or evidence would put a blank alert in front of the user.

## `test_unreachable_model_returns_none`

**What it does:** A connection error from the model returns `None` at once, and the valid reply queued after it is never asked for.

**Why it's there:** The run used to fail on an unreachable model, which hid every overspend until Ollama came back (review on #12). `None` makes the graph write its code-written alert. There's no retry, because a model that's down won't answer a second call either. The errors that count as unreachable are httpx errors, `ConnectionError`, `TimeoutError` and Ollama's `ResponseError`.

## `test_other_model_error_propagates`

**What it does:** A `KeyError` raised by the model call still propagates.

**Why it's there:** Only an unreachable model falls back. Anything else is a bug, and catching it would hide it behind a fallback alert, the way a broad catch briefly hid a test fake running out of replies.

## `test_action_schema`

**What it does:** An `adjust_next_budget` action parses through the same call.

**Why it's there:** Both LLM calls share one path, so a fix to retries or parsing applies to both.

## `test_decide_messages_carry_numbers`

**What it does:** The decide prompt carries the facts, the diagnosis cause, the projected spend ($155.00) and the proposed limit ($160.00) in dollars. Without a diagnosis it says the cause is unknown.

**Why it's there:** The LLM picks the action but never computes a money number. It gets the code's projection and proposal to quote, and still gets a prompt when the diagnosis failed.

## `test_whitespace_text_is_invalid`

**What it does:** Evidence of only spaces counts as invalid, and the retry's reply is used.

**Why it's there:** It was found in the TPC-19 review: `min_length=1` alone let `"   "` through, which would have written a blank alert.

## `test_no_proposal_unless_recurring`

**What it does:** For a `seasonal` diagnosis, or none at all, the decide prompt gives no proposed limit and says `adjust_next_budget` is only for a recurring cause.

**Why it's there:** It was found in the TPC-19 review. In a live run, holiday shopping got `adjust_next_budget` with a $1,600 January limit. A holiday month or a one-off says nothing about next month's budget.

## `test_decide_messages_severity_and_rules`

**What it does:** The decide prompt states the flag's severity. Its instructions open with the memory check, before the list of actions: `no_op` when this month already has an alert of the same or higher severity, and never re-propose an adjustment the user dismissed.

**Why it's there:** Deduplication is the model's call (TPC-20). It can only judge "nothing got worse" if it's told this flag's severity next to the earlier alerts'.

## `test_decide_messages_summarize_earlier_alerts`

**What it does:** With two earlier alerts this month (medium, then high), the decide prompt says "Earlier alerts for this category this month: 2, highest severity high". With none, it says "none".

**Why it's there:** In live runs, `qwen2.5:14b` alerted again on the same overspend twice when the earlier alerts were only inside the JSON facts. With this one-line summary and the memory rule first in the prompt, it chose `no_op` both times. The model still decides; the code only puts the comparison in front of it.
