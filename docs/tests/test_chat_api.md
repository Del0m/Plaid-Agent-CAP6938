# Tests: `backend/tests/test_chat_api.py`

These tests cover `app/api/chat.py`, the chat agent over HTTP (TPC-15):
- **`POST /chat`** runs one turn of the chat graph and streams it as server-sent events (`sources`, `tool_call`, `tool_result`, `token`, then `done` or `error`).
- **`GET /chat/{thread_id}`** returns the conversation so far, grouped into turns.
- **A per-user lock** allows one reply at a time.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_chat_api.py -v
```

## Shared setup

- `users` seeds `u-1` (a credit card at 22% APR with $410, a student loan at 5.25%) and `u-2`, who exists only to prove that threads are per user.
- `api` is the conftest `client`, with these hooks swapped out:
  - `chat.session_factory` uses the test database;
  - `chat.get_checkpointer` is an in-memory `MemorySaver`;
  - `chat.make_kb` returns a `FakeKB`, which finds one CFPB result;
  - the graph's `get_chat_model` returns a `ScriptedChatModel`;
  - the privacy audit log goes to `tmp_path`;
  - `u-1` is the signed-in user.
- `read_events` parses the SSE body into `(event, data)` pairs. `SOURCE` is how the `FakeKB` result appears in a `sources` event.

---

## `test_turn_streams_tools_then_tokens_then_done`

**What it does:** The model calls `get_debts`, then answers. The test checks the event order: `sources`, `tool_call`, `tool_result`, several `token`s, then `done`. It also checks the contents of the first three events, and that the joined tokens equal the answer.

**Why it's there:** This is the contract the chat page is built on. The order matters too: sources and tool chips appear before the answer starts streaming.

## `test_tool_result_content_not_sent`

**What it does:** Checks that `Plaid Credit Card`, which appears in the `get_debts` output, appears nowhere in the stream.

**Why it's there:** The page shows *which* tools ran, not what they returned. Raw tool output is gateway JSON meant for the model, and the answer already carries what the user needs.

## `test_failed_tool_reports_not_ok`

**What it does:** Checks that a call to a tool that doesn't exist produces `tool_result` with `ok: false`.

**Why it's there:** The chip shows ✗ for a failed tool. That's how a person sees that the model tripped over its tools instead of getting a silent wrong answer.

## `test_message_validation`

**What it does:** Checks that an empty message, a whitespace-only message and a 4,001-character message each get `422`.

**Why it's there:** Each of these would cost a model run. An empty turn would also be stored in the thread forever.

## `test_thread_id_must_be_simple` and `test_history_thread_id_must_be_simple`

**What they do:** Check that `POST /chat` rejects `../x`, `a b`, 65 characters and an empty id with `422`. They also check that `GET /chat/{id}` rejects a space and 65 characters with `422`. A slash never reaches that route as part of the id, so the GET test leaves it out.

**Why they're there:** The thread id becomes part of the checkpoint key. Only `A-Z a-z 0-9 -`, up to 64 characters, ever reaches the checkpointer; the page uses UUIDs.

## `test_ollama_down_message`

**What it does:** Checks that a model call raising `ConnectionError` gives a `sources` event, then exactly one `error` saying "The local model isn't reachable. Is Ollama running?"

**Why it's there:** This is the most likely failure in a local demo, and the message says exactly what to fix.

## `test_other_error_is_generic`

**What it does:** The model raises `RuntimeError("acct 4321")`. The test checks that the last event is the generic "Something went wrong…" message, and that `4321` appears nowhere in the response.

**Why it's there:** Exception text can carry user data. It goes to the local log, never to the browser. This is the same rule as TPC-12.

## `test_empty_answer_message`

**What it does:** Checks that a model reply with no text and no tool call ends with "The model returned an empty answer. Try rephrasing." and sends no tokens.

**Why it's there:** Otherwise the page would show an empty bubble with no explanation.

## `test_token_with_blank_lines_is_one_event`

**What it does:** The answer `a\n\nb` arrives as one chunk. The test checks that the joined tokens are exactly `a\n\nb` and the stream still ends with `done`.

**Why it's there:** A blank line ends an SSE event. The text is JSON-encoded, so newlines in an answer (lists, paragraphs) can't break the framing.

## `test_history_grouped_after_turn`

**What it does:** After one turn, checks that `GET /chat/t1` returns the user turn, then the assistant turn with its text, its tool (`get_debts`, ok) and its sources.

**Why it's there:** This is what the page redraws after a refresh. Tools and sources have to stay attached to the answer they belong to.

## `test_no_sources_event_when_kb_empty`

**What it does:** With an empty knowledge base, checks that no `sources` event is sent and that the history's assistant turn has `sources: []`.

**Why it's there:** If `ingest_kb.py` was never run, chat should still work. It just has no guidance to list, rather than listing nothing as if it were a source.

## `test_text_before_tool_call_not_in_history`

**What it does:** The model writes `CallCheck_debts()` next to its tool call, then answers. The test checks that this text streams before the `tool_call` event (so the page can drop it), and that history keeps only the real answer.

**Why it's there:** qwen2.5:14b did exactly this in live runs. Showing "CallCheck_debts()" as part of the answer would look broken.

## `test_unknown_thread_history_empty`

**What it does:** Checks that a thread that was never used returns `[]`.

**Why it's there:** A new tab's thread id doesn't exist until its first message. That's normal, not a 404.

## `test_threads_are_per_user`

**What it does:** `u-1` chats on thread `t1`. Then, as `u-2`, the test checks that `GET /chat/t1` returns `[]`.

**Why it's there:** Checkpoint keys are `<user_id>:<thread_id>`, so guessing someone's thread id shows nothing. This is ready for when real auth replaces the single dev user.

## `test_history_survives_new_checkpointer`

**What it does:** Runs a turn with a SQLite checkpointer on a temp file, then swaps in a fresh saver on the same file, as a backend restart would. It checks that history is unchanged, then runs a second turn and checks that both turns used the same pseudonym in the audit log.

**Why it's there:** Conversations are meant to survive a restart. The pseudonym must survive it too, or the model would see a different "user" halfway through a chat.

## `test_second_request_while_streaming_gets_409`

**What it does:** Starts a turn on a background thread with the model held on a gate. While it's running, a second POST must get `409` with "A reply is already in progress." Once the first turn finishes, a POST must succeed.

**Why it's there:** This is the per-user lock. Each turn holds the local model for seconds, and a second tab or a refresh loop would otherwise stack model runs.

## `test_lock_released_after_error`

**What it does:** Checks that after a turn that raises, the next POST succeeds.

**Why it's there:** A failed turn must not leave the user locked out.

## `test_lock_released_when_client_disconnects`

**What it does:** Calls the route directly, reads the first event (`sources`) from its body, then closes the body, which is what Starlette does when the browser goes away. It checks that the lock is freed within a few seconds and that the next POST succeeds.

**Why it's there:** Closing a tab mid-answer is normal. Closing the body tells the turn's worker thread to stop; the worker cleans up and frees the lock on its own thread. If the lock leaked, chat would show "already in progress" until a restart.

## `test_disconnect_does_not_block_event_loop`

**What it does:** The fake model writes slowly (0.3 s per word). The test reads `sources` and the first token, then closes the body *while the model is still writing*. It checks that closing takes under 0.5 s, that a ticker task on the event loop never stalls for 0.5 s, and that the lock is freed afterwards.

**Why it's there:** LangGraph's stream waits for the in-flight model call when it's closed. When that close ran on the event loop, a refresh mid-answer froze the whole backend (`/health`, the dashboard, history) until the model finished: 1.48 s in this test, tens of seconds with the real 14B model. Found in the final review. The turn now runs entirely on a worker thread.

## `test_keepalive_sent_while_model_silent`

**What it does:** With the keep-alive interval set to 0.05 s and the model held silent for 0.4 s, it checks that the body contains `: keep-alive` comments and still ends with `done`.

**Why it's there:** The Next.js rewrite proxy drops a response after 30 s with no bytes. A cold `qwen2.5:14b` load, or a busy shared Ollama, can be silent that long before the first token, and the browser would see the answer cut off. A keep-alive goes out after 10 s of quiet; it's an SSE comment, so `parseSSE` ignores it. Found in the final review.

## `test_no_message_text_in_logs`

**What it does:** Captures every log record at DEBUG and checks that neither the question nor the answer appears.

**Why it's there:** Chat text can contain anything a person types about their money. The logs record event types and tool names only.

## `test_live_chat_answers_which_debt_first`

**What it does:** The M2 done-when (spec amendment A1). With the real chat model, the real knowledge base and the seeded user, it asks "Which debt should I pay first?" and checks:
- the turn ends with `done`;
- the answer mentions the credit card and `22`;
- the `sources` event includes the CFPB "How to reduce your debt" page;
- every URL written in the answer is one of those sources.

It's skipped unless Ollama has `OLLAMA_MODEL` and `OLLAMA_EMBED_MODEL` and the knowledge base is ingested.

**Why it's there:** This is the demo the milestone exists for. The last check fails the test if the model makes up a citation, which both local models did before the app took over retrieval. Score on 2026-10-26 with `qwen2.5:14b`: **3/3**. Under the original design it was 0/3: no `search_kb` call, and invented sources.
