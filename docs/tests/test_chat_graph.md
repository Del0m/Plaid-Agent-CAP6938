# Tests: `backend/tests/test_chat_graph.py`

These tests cover `app/agents/chat_graph.py`, the M2 chat agent (TPC-15). It's a hand-built LangGraph `StateGraph` with three nodes:
- `retrieve` searches the knowledge base for the user's question;
- `agent` calls the model with the system prompt, the retrieved guidance and the tools bound;
- `tools` runs the requested tool calls through `_run_tool`.

The graph loops between `agent` and `tools` until the model answers, with at most 6 tool rounds per question. The `retrieve` step was added by spec amendment A1: in live runs, neither local model called `search_kb` on its own, and both invented citations. It replaces TPC-12's temporary `call_with_tools` loop, so the tool-error rules first tested there are now tested here.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_chat_graph.py -v
```

## Shared setup

- `ScriptedChatModel` (`tests/fakes.py`) is a real LangChain chat model that replays scripted replies: an `AIMessage`, or an exception to raise. It records two things on every call:
  - the messages it was sent, in `.seen`;
  - the tools bound, in `.tools_seen` (`None` when no tools were bound).

  Text replies stream word by word, so LangGraph's token streaming treats it like Ollama.
- `tool_call(name, args, id)` builds an `AIMessage` that requests one tool call.
- The tools:
  - `get_lucky_number` returns `len(name) * 7 + 3`, so "Ada" gives 24, a number the model can't guess;
  - `bad` raises `ToolException("bad arg")`;
  - `boom` raises `RuntimeError("secret user data 4321")`, standing in for a database error that carries user data.
- `run(model, ..., retrieve=None)` builds the graph with a `MemorySaver` and runs one question on thread `t1`. `final(graph)` is the last stored message.
- `GUIDE` is one knowledge-base result (CFPB, "Highest interest rate method"), used as the retriever's output.

---

## `test_no_tool_call_returns_answer`

**What it does:** With no retriever, checks that a plain answer is returned, that the first message the model sees is exactly the system prompt, and that the system prompt is never stored in the thread's messages.

**Why it's there:** The system prompt carries the rules the M2 demo depends on: use the tools for the user's numbers, and cite `source_url`. Sending it on every call without storing it means a prompt change applies to existing conversations too, rather than piling up old copies in the history.

## `test_tool_call_round_trips`

**What it does:** The model asks for `get_lucky_number("Ada")`. The test checks that the next model call ends with a `ToolMessage` whose `tool_call_id` is `c1` and whose content is `24`, and that the model's answer is the final message.

**Why it's there:** This is the ReAct loop. The tool result has to come back tagged with the id of the call that asked for it, or the model can't match answers to questions.

## `test_unknown_tool_gets_error_message`

**What it does:** The model calls a tool that doesn't exist. The test checks that it gets back a message starting with `error: unknown tool` and the graph carries on.

**Why it's there:** Small models invent tool names. That should be a recoverable mistake, not a crash in the middle of a chat.

## `test_tool_exception_text_reaches_model`

**What it does:** Checks that a tool raising `ToolException("bad arg")` reaches the model as exactly `error: bad arg`.

**Why it's there:** A `ToolException` is the tool talking to the model, for example "unknown category … use one of …". Its text is what lets the model fix its call.

## `test_generic_exception_text_not_forwarded`

**What it does:** Checks that a tool raising `RuntimeError("secret user data 4321")` reaches the model only as `error: boom failed (RuntimeError)`, and that `4321` appears in nothing the model was sent.

**Why it's there:** Any other exception's text can carry user data. SQLAlchemy errors print bound parameters, for example, and this path skips the privacy gateway. This is the TPC-12 rule, now enforced by the graph.

## `test_validation_error_input_not_forwarded`

**What it does:** The `strict` tool builds a pydantic model from a value that looks like database data ("Jane Doe's Checking") and fails validation. The test checks that the model gets an `error:` message that doesn't contain "Jane".

**Why it's there:** A pydantic `ValidationError` prints `input_value=...`. When the error comes from a tool's own code building models from database rows, that value is user data, and `_run_tool` doesn't go through the privacy gateway. The review on #6 found that these errors were forwarded word for word. Now only a generic "check the tool's argument schema" message reaches the model. (This test started in `test_llm.py` on TPC-12 and moved here with the tool loop.)

## `test_model_error_propagates`

**What it does:** Checks that a model call raising `ConnectionError` raises out of the graph.

**Why it's there:** If Ollama goes away, the chat endpoint has to know, so it can show "Is Ollama running?". Swallowing the error would produce an empty or made-up answer instead.

## `test_round_cap_then_answer_without_tools`

**What it does:** The model asks for a tool 6 times in a row. The test checks that the first 6 calls have tools bound, that a 7th call happens with **no** tools bound, and that its answer is the final message.

**Why it's there:** A small model can loop on tool calls forever. Removing the tools forces it to answer from what it already has, so the user gets an answer instead of an error. The cap is 6 rounds per question.

## `test_tool_rounds_reset_each_turn`

**What it does:** Turn 1 uses 2 tool rounds. Turn 2 starts with `tool_rounds: 0`, and the test checks that its model call has tools bound again.

**Why it's there:** The cap is per question. If the count carried over, a long conversation would eventually lose its tools for good.

## `test_tool_rounds_reset_without_the_caller`

**What it does:** Turn 1 uses all 6 tool rounds. Turn 2 is invoked with only its message, no `tool_rounds`, and the test checks that its model call has tools bound.

**Why it's there:** The reset used to depend on every caller passing `tool_rounds: 0`, and the count is stored in the checkpoint. A caller that forgot (an eval harness, a later endpoint) inherited the old count and lost its tools, so the model answered balance questions without looking them up. The retrieve step now resets it. The review on #9 found it.

## `test_dangling_tool_calls_answered_before_the_model_sees_them`

**What it does:** Runs a turn whose history holds a tool call with no result, as a turn cut off mid-tool leaves it. It checks that the model is sent an `error:` ToolMessage for that call right after it.

**Why it's there:** A closed tab or an Ollama timeout after a tool call leaves an unanswered call in the checkpoint, and every later turn was built on it. The stored history is left as it is (GET /chat shows the call as failed, which is true), but the model now gets a stand-in result for each unanswered call. The review on #9 found it.

## `test_memory_across_turns`

**What it does:** Runs two turns on one thread and checks that the second model call sees the first question and the first answer.

**Why it's there:** This is the checkpointer's job. A follow-up like "and the second one?" only makes sense if the model can see the earlier turn.

## `test_pseudonym_kept_in_state`

**What it does:** Turn 1 stores `pseudonym="user_ab12"`. Turn 2 leaves the pseudonym out, and the test checks the stored one is unchanged.

**Why it's there:** The privacy gateway's pseudonym has to stay the same for the whole conversation, or the model sees a different "user" every turn. The chat endpoint reads it from this state.

## `test_retrieve_puts_guidance_in_system_prompt_not_messages`

**What it does:** With a retriever that returns `GUIDE`, checks that:
- the system prompt starts with `SYSTEM_PROMPT` and then lists the guidance as `- <title> / <section>: <text>`;
- the guidance has no URL;
- the guidance is never stored in the thread's messages;
- `sources` in state holds what was retrieved.

**Why it's there:** The model gets the guidance as context and refers to it by title. The app, not the model, shows the URLs, so a citation can't be invented. Keeping the guidance out of stored messages means old threads never carry stale guidance.

## `test_retrieve_searches_each_new_question`

**What it does:** Runs two turns and checks that the retriever was called with each turn's question, in order.

**Why it's there:** Guidance has to match the question being asked now, not the first question of the conversation.

## `test_no_guidance_when_nothing_retrieved`

**What it does:** Checks that when nothing is retrieved, the system prompt is exactly `SYSTEM_PROMPT`.

**Why it's there:** An empty knowledge base shouldn't put an empty "Guidance" heading in front of the model, which would invite it to make guidance up.

## `test_sources_by_turn_keyed_by_question_id`

**What it does:** Runs two turns with different results and checks that `sources_by_turn` maps each user message's id to that turn's sources: title, section and URL only, without the passage text.

**Why it's there:** `GET /chat/{id}` uses this to show each answer's own sources after a refresh. `sources` alone only holds the latest turn's. History only needs the citation, and the checkpointer rewrites every channel on every step, so storing about four full passages per question grew long threads for nothing. The review on #9 found it.

## `test_live_tool_call_round_trip`

**What it does:** Asks the real local model "What is Ada's lucky number? Use the tool." through the graph, and checks that the answer contains 24. It's skipped unless Ollama is running with `OLLAMA_MODEL` pulled.

**Why it's there:** It proves the graph works with real tool calling on `llama3.1:8b`, not just with the fake. It replaces the `call_with_tools` live test from TPC-12. It passed on 2026-10-26 on `llama3.1:8b`, and again after the chat model became `qwen2.5:14b`.
