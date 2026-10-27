# Tests: `backend/tests/test_llm.py`

These tests cover `app/agents/llm.py`, the only place the app creates an LLM client. They check that `get_chat_model` builds `ChatOllama` from config, and that `call_with_tools` runs a tool call round trip: the model asks for a tool, gets the result back, and answers with it. Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_llm.py -v
```

## Helpers

- `ScriptedModel(*replies)` is a fake chat model. Each `invoke` returns the next canned `AIMessage`, or raises it if it's an exception, and every call records the messages it was sent in `.seen`. `call_with_tools` only uses `bind_tools` and `invoke`, so this fake is enough to drive the loop without Ollama.
- `get_lucky_number(name)` is the toy tool. It returns `len(name) * 7 + 3`, so "Ada" gives 24. It's a number the model can't guess, so a right answer proves the tool was really called.
- `call(name, args, id)` builds an `AIMessage` that requests one tool call.

---

## `test_get_chat_model_uses_config`

**What it does:** Sets `OLLAMA_MODEL`, `OLLAMA_BASE_URL` and `LLM_TEMPERATURE` in `config` and checks that the model `get_chat_model()` returns uses all three.

**Why it's there:** The whole point of the wrapper is that config decides which model runs, including the eval runs that swap in `mistral`. Settings that were read once at import time, or hard-coded, would make that swap quietly do nothing.

## `test_get_chat_model_overrides`

**What it does:** Calls `get_chat_model(temperature=0.7, model="mistral")` and checks that both arguments win over the config defaults.

**Why it's there:** The default temperature is 0 so evals are repeatable, but chat may want some variety, and benchmarks need to pick the model per run. Both paths need to work.

## `test_get_embeddings_uses_config`

**What it does:** Sets `OLLAMA_EMBED_MODEL` and `OLLAMA_BASE_URL` with monkeypatch, then checks that `get_embeddings()` builds an Ollama embeddings client with exactly that model and server.

**Why it's there:** The knowledge base (TPC-14) embeds with whatever this returns. If it ignored config, an M5 embedding-model comparison would quietly keep using nomic-embed-text.

## `test_tool_call_round_trips`

**What it does:** The fake model first asks for `get_lucky_number(name="Ada")`, then answers. The test checks that the second model call received a `ToolMessage` with content `"24"` and the matching `tool_call_id`, and that the final answer is returned.

**Why it's there:** This is the ticket's "toy tool call round-trips", tested without a real model. The `tool_call_id` matters because the model uses it to pair a result with its request. Without it, Ollama can ignore or mismatch the result.

## `test_no_tool_call_returns_first_reply`

**What it does:** The fake model answers straight away, and the test checks that the answer comes back after a single model call.

**Why it's there:** Most chat turns won't need a tool. The loop shouldn't call the model again when there's nothing to run.

## `test_unknown_tool_gets_error_message`

**What it does:** The fake model asks for a tool called `nope`. The test checks that the next model call gets a `ToolMessage` saying "unknown tool" and that the loop finishes normally.

**Why it's there:** Small local models sometimes invent tool names. Crashing on that would end the whole chat turn, while telling the model lets it try again with a real tool.

## `test_tool_exception_becomes_error_message`

**What it does:** A tool that raises `ToolException("boom")` is called. The test checks that the model is told `error: boom` and that the loop finishes normally.

**Why it's there:** `ToolException` (and pydantic's argument `ValidationError`) is how a tool tells the model what went wrong, for example "unknown category". The model should get that message and a chance to fix its call, rather than the user getting a server error.

## `test_generic_exception_message_not_forwarded`

**What it does:** A tool raises a `RuntimeError` whose message looks like a SQLAlchemy error with bound parameters (`'u-jane'`). The test checks that the model is told the tool failed and the exception type, but sees neither `u-jane` nor the SQL.

**Why it's there:** This loop is a way for text to reach the model without going through the privacy gateway. Database errors print their parameters, which can include user ids, emails or names. So only exceptions written for the model are forwarded, and every other error is reduced to its type, with the full detail kept in local logs. The final code review found this leak.

## `test_model_error_propagates`

**What it does:** The fake model raises `ConnectionError`, and the test expects that error to come out of `call_with_tools`.

**Why it's there:** If Ollama is down, the caller has to know. Swallowing the error and returning an empty or made-up answer would look like the assistant replied.

## `test_loop_stops_after_max_steps`

**What it does:** The fake model asks for a tool on every turn. With `max_steps=3`, the test expects a `RuntimeError` saying the loop "did not finish in 3 steps".

**Why it's there:** A model stuck calling tools forever would hang the request and keep the GPU busy. The cap turns that into a clear error.

## `test_live_tool_call_round_trip`

**What it does:** Sends the real local model (`OLLAMA_MODEL`, by default `llama3.1:8b`) the message "What is Ada's lucky number? Use the tool." with a recording copy of the toy tool. It checks that the tool was called exactly once with `"Ada"` and that the final answer contains `24`. It's skipped unless `GET {OLLAMA_BASE_URL}/api/tags` responds and lists the model.

**Why it's there:** It's the ticket's done-when on real hardware. The fake-model tests prove the loop is wired correctly, but only a real run shows that this 8B model actually emits tool calls through Ollama. It passed 3 out of 3 runs on 2026-10-21 at temperature 0.

---

## Not covered

- **Tool-calling reliability in general.** The live test proves one model, one tool and one prompt. Longer conversations, several tools, and tools with complex arguments are where small models fail. That belongs in the M2 eval harness (TPC-16), not here.
- **Streaming.** `call_with_tools` returns whole messages. Streaming tokens to the chat UI is TPC-15.
