# TPC-15: Chat agent graph with streaming chat UI

Jira: TPC-15 (epic TPC-3, M2). Branch: `TPC-15`, cut from `TPC-14` (rebase onto `main` once PRs #5-#8 merge).

## Goal

Ship the M2 demo. On a `/chat` page you ask a question, the answer streams in, and the page shows which tools the agent called. Behind it:
- a hand-built LangGraph `StateGraph` over the four tools (`get_accounts`, `get_debts`, `get_spending`, `search_kb`);
- a SQLite checkpointer, so a conversation remembers earlier turns and survives a backend restart;
- a streamed `POST /chat` endpoint.

**Done when:** "Which debt should I pay first?" names the highest-APR card with the right numbers and cites a source URL from `search_kb`.

**Out of scope:** auth (still the one dev user); a UI for listing or switching conversations; the eval harness (TPC-16).

## Amendment A1 (2026-10-26, after live probes): the app retrieves and cites

This amendment **supersedes** the parts of this spec it contradicts: the `SYSTEM_PROMPT` text, the event list, the history shape, and the done-when test.

**Why.** With the original design, the live done-when test failed 0/3:
- `llama3.1:8b` called `get_debts` but never `search_kb`, then invented a source (NerdWallet, bankrate.com).
- A stricter prompt didn't help. `qwen2.5:14b` also never called `search_kb`, and it invented CFPB URLs that aren't in the knowledge base.
- Neither model makes two *kinds* of tool call in one turn.

Putting the retrieved guidance into the system prompt, and keeping the data tools available, gave the right behavior with no invented URLs: the model called `get_debts`, named the 22% card, and credited the CFPB. Armin chose this design, with **`qwen2.5:14b` as the chat model**.

**Changes**

1. **Chat model.** The `OLLAMA_MODEL` default becomes `qwen2.5:14b`, in `config.py` and `.env.example`. It's also the TPC-16 judge, so TPC-16 needs a different judge model so the model isn't grading itself; that's noted in the decision log.
2. **`retrieve` node.** The graph becomes `START → retrieve → agent ⇄ tools`. `build_chat_graph(tools, *, retrieve=None, model=None, checkpointer=None)`, where `retrieve: Callable[[str], list[dict]]` is `KnowledgeBase.search` in the API, or `None` for no guidance.
   - The node searches on the latest user message (top 4) and stores the results in state.
   - `ChatState` gains `sources: list[dict]` (this turn's results) and `sources_by_turn: dict[str, list[dict]]`. The second is keyed by the user message's id, merged with a dict-merge reducer, and used for history.
   - If the knowledge base is empty, there's no guidance and no sources.
3. **The guidance goes into the system prompt, never into stored messages.** It's appended as `\n\nGuidance retrieved for this question:\n- <title> / <section>: <text>` per result.
4. **`SYSTEM_PROMPT` (exact text, replacing the one below):**

   > You are a personal finance assistant for one user. For anything about the user's own accounts, debts or spending, call get_accounts, get_debts or get_spending and use only the numbers they return; never guess the user's numbers. For general rules and strategies, use the guidance below and refer to it by its title; never write URLs or name other websites, because the app lists the sources for you. If the guidance doesn't cover the question, call search_kb. When a figure applies to a specific year, say which year. Keep answers short and concrete. End with one line: "This is general information, not financial advice."

5. **A new SSE event: `sources {sources: [{title, section, url}]}`.** It's sent once per turn, right after retrieval and before any token, and only when there are results. The UI lists them as clickable "Sources" under the answer.
6. **Text written before a tool call is not the answer.** qwen sometimes writes text alongside a tool call (e.g. `CallCheck_debts()`). Tokens are still streamed as they arrive. When a `tool_call` event follows, the client discards the assistant text received so far in that turn. History uses the text of the turn's last AI message that has no tool calls.
7. **History shape.** Each assistant turn gains `sources: [{title, section, url}]`, from `sources_by_turn` for the user message that started it.
8. **Done-when test (replaces the one below).** On the seeded user, "Which debt should I pay first?" must give:
   - an answer containing "credit card" and "22";
   - a `sources` event including the CFPB `how-reduce-your-debt` URL;
   - no `http` URL in the answer text that isn't among the sources. Fabrication fails the test.

   It's skipped unless Ollama has `OLLAMA_MODEL` and `OLLAMA_EMBED_MODEL`, and the knowledge base is ingested. Run it 3 times and record the score.

## Decisions

| Question | Decision |
|---|---|
| Python | Rebuild the venv on **Python 3.12** (`/opt/homebrew/bin/python3.12`) as the first task. architecture.md already says 3.12; the venv was 3.9 by accident, which capped LangGraph at 0.6.11 and caused the LibreSSL warning |
| Transport | **`POST /chat` returning `text/event-stream`**, not `WS /chat`. Next.js rewrites don't forward WebSocket upgrades. A streamed POST goes through the existing `/api/*` rewrite, so the phone demo and the no-CORS decision (2026-10-18) still hold. The browser reads it with `fetch` plus a stream reader, since `EventSource` can't POST |
| Graph | Hand-built `StateGraph`: `agent` ⇄ `tools`. The tools node reuses `_run_tool`, so the TPC-12 rule holds: only `ToolException` and validation text reach the model, never raw exception text. `create_react_agent` is not used: its `ToolNode` sends raw exception text back to the model by default |
| Tool-round cap | 6 rounds. After that the agent is called once more **without tools bound**, so the user still gets an answer |
| Sync vs async | Everything sync: SQLAlchemy session, `SqliteSaver`, `graph.stream` feeding a sync generator to `StreamingResponse` (Starlette iterates it in its threadpool). Async would add aiosqlite and sync DB calls inside async nodes, for no gain with one local user |
| Memory | `SqliteSaver` at `backend/data/checkpoints.db` (git-ignored by `*.db`). Checkpoint `thread_id` = `"<user_id>:<client thread id>"`, so one user can never load another user's thread |
| Pseudonym | Stored in graph state on the thread's first turn and reused on later turns (`PrivacyGateway(..., pseudonym=...)`) |
| Context | `get_chat_model` gains `num_ctx`, from new config `LLM_NUM_CTX=8192`. `search_kb` adds about 2k tokens per call, and Ollama's smaller default would silently truncate the conversation |
| Concurrency | **One reply in flight per user.** A per-user `threading.Lock` is acquired without waiting before streaming. A busy user gets `409`, and the lock is released in `finally`, including on errors and disconnects. This assumes one uvicorn worker, which is how the app runs |
| Frontend tests | Add **Vitest + React Testing Library + jsdom**, following Next 16's bundled guide (`node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md`): `vitest`, `@vitejs/plugin-react`, `jsdom`, `@testing-library/react`, `@testing-library/dom`, `vite-tsconfig-paths`. Run with `npm test` (`vitest run`). Frontend tests are documented in `docs/tests/` like backend ones |

## Task 0: Python 3.12 venv

1. `/opt/homebrew/bin/python3.12 -m venv venv.new`, then `venv.new/bin/pip install -r backend/requirements.txt`.
2. `cd backend && ../venv.new/bin/python -m pytest -q -rs` must give 148 passed with no skips. Package versions will move: on 3.12, pip resolves current releases (for example langchain-core 1.x). If anything fails, fix the code if the fix is small; otherwise pin the old version in `requirements.txt` and say so in the PR.
3. Swap: `mv venv venv39.bak && mv venv.new venv`. Run the suite again from `venv/`, then delete `venv39.bak`.
4. Commit any `requirements.txt` changes.
5. Decision-log row: the venv is now 3.12, matching section 3.

## Task 1: proxy streaming check (before any chat code)

- Add a throwaway route `GET /dev/stream-check` that yields 5 SSE events 200 ms apart.
- Run `next build && next start` with the rewrite and read it with `curl -N http://localhost:3000/api/dev/stream-check`, timestamping each line as it arrives. The events must arrive spread over at least ~800 ms; if they all arrive together, the proxy is buffering.
- Delete the route and record the result in the decision log.
- If the proxy buffers, try these fixes in order: `X-Accel-Buffering: no` and `Cache-Control: no-cache, no-transform` on the response; then `compress: false` in `next.config.ts`. If it still buffers, stop and ask.

## Configuration (`app/config.py`, `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `LLM_NUM_CTX` | `8192` | Context window passed to Ollama for the chat model |
| `CHAT_CHECKPOINT_PATH` | `backend/data/checkpoints.db` (resolved from `app/`) | LangGraph SQLite checkpointer file |

`requirements.txt` gains `langgraph` and `langgraph-checkpoint-sqlite`. The root `.gitignore` gains `backend/data/`. (This originally said nothing was needed because `*.db` is ignored. That was wrong: `SqliteSaver` uses WAL mode, and the conversation data sits in `checkpoints.db-wal` and `-shm`, which `*.db` doesn't match. Found in the final review.)

## `app/agents/llm.py`

- `get_chat_model(temperature=None, model=None, num_ctx=None)` passes `num_ctx=num_ctx or config.LLM_NUM_CTX` to `ChatOllama`.
- `call_with_tools` is deleted; it was always marked temporary for TPC-15. `_run_tool` stays and is used by the graph. The `call_with_tools` tests in `test_llm.py` and `test_tools.py` move to `test_chat_graph.py` or are dropped: the live lucky-number round trip becomes a graph test, and `test_live_model_picks_get_debts` runs through the graph.

## `app/agents/chat_graph.py`

```python
class ChatState(MessagesState):
    pseudonym: str
    tool_rounds: int

def build_chat_graph(tools: list[BaseTool], *, model: Optional[BaseChatModel] = None,
                     checkpointer: Optional[BaseCheckpointSaver] = None) -> CompiledStateGraph
```

- **`agent` node:** sends `[SystemMessage(SYSTEM_PROMPT)] + state["messages"]` to the model. The model has the tools bound while `tool_rounds < MAX_TOOL_ROUNDS` (6), and is called **without** tools after that. The system message is never stored in state.
- **`tools` node:** for each tool call on the last AI message, appends `ToolMessage(_run_tool(by_name, tc), tool_call_id=tc["id"], name=tc["name"])`, then increments `tool_rounds`.
- **Edges:** `START → agent`. `agent → tools` if the last message has tool calls, else `→ END`. `tools → agent`.
- **`SYSTEM_PROMPT`** (exact text):

  > You are a personal finance assistant for one user. For anything about the user's own accounts, debts or spending, call get_accounts, get_debts or get_spending and use only the numbers they return; never guess the user's numbers. For general rules and strategies, call search_kb and cite the source_url of each passage you rely on. When a figure applies to a specific year, say which year. Keep answers short and concrete. End with one line: "This is general information, not financial advice."

- `tool_rounds` resets to 0 at the start of each user turn: the endpoint passes `{"messages": [HumanMessage], "tool_rounds": 0}` as input.

## `app/api/chat.py`

**`POST /chat`**, body `{thread_id: str (1-64 chars), message: str (1-4000 chars after strip)}`. Invalid input gets `422`.
1. Acquire the user's lock without waiting. If it's taken, return `409 {"detail": "A reply is already in progress."}`.
2. Read the thread's state from the checkpointer: its pseudonym, or a new one.
3. Build `PrivacyGateway(session, user.id, pseudonym=...)`, `KnowledgeBase()`, `make_tools`, `build_chat_graph(tools, checkpointer=saver)`.
4. Return `StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})`. The generator runs `graph.stream(input, config, stream_mode=["messages", "updates"])` and releases the lock in `finally`.

The DB session must stay open for the life of the stream. The generator owns it: open it with `SessionLocal()` inside the generator, not the request-scoped dependency.

**Events** (`event: <type>\ndata: <json>\n\n`):

| Event | Source | Data |
|---|---|---|
| `tool_call` | `updates` from `agent` whose AI message has tool calls | `{id, name, args}` per call |
| `tool_result` | `updates` from `tools` | `{id, name, ok}`, where `ok = not content.startswith("error:")` |
| `token` | `messages` chunks from the `agent` node with text content | `{text}` |
| `done` | the graph finished | `{}` |
| `error` | an exception | `{message}`: fixed text, see below |

Tool result content is never sent to the browser.

**Errors** (detail goes only to `logger.warning(..., exc_info=True)`):

| Case | `error.message` |
|---|---|
| Ollama unreachable (`httpx.ConnectError` or builtin `ConnectionError`) | "The local model isn't reachable. Is Ollama running?" |
| The final AI message has no text and no tool calls | "The model returned an empty answer. Try rephrasing." |
| Any other exception | "Something went wrong answering that. Try again." |

**`GET /chat/{thread_id}`** returns `[{role: "user"|"assistant", text, tools: [{name, args, ok}]}]` from the checkpoint. Tool calls and results are grouped under the assistant turn they belong to. An unknown thread returns `[]`.

**Logging:** event types, tool names and timings only, never message text.

Register the router in `app/main.py`.

## Frontend

- **`lib/sse.ts`:** `parseSSE(buffer: string): { events: {event: string, data: unknown}[], rest: string }`. A pure function that splits on blank lines and keeps a partial trailing event in `rest`.
- **`lib/api.ts`:**
  - `streamChat(threadId, message, onEvent, signal?)` POSTs to `/api/chat` and feeds `parseSSE` from `response.body.getReader()`. A `409` throws `ApiError(409)`; any other non-200 throws `ApiError`.
  - `getChatHistory(threadId)`.
  - Types `ChatEvent` and `ChatTurn`.
- **`components/ChatPanel.tsx`** (client):
  - Messages: user bubbles; assistant bubbles that grow as `token` events arrive; tool chips under each assistant bubble (`get_debts ✓`, `search_kb ✗`).
  - Input and Send are disabled while a reply streams.
  - An `error` event, or a failed request, shows the message in the assistant bubble. A `409` shows "Still answering the last message."
  - The thread id is a UUID in `sessionStorage["chatThreadId"]`. On mount it loads `getChatHistory`. "New chat" makes a new UUID and clears the list.
- **`app/chat/page.tsx`:** the page wrapper with a link back to the dashboard. `app/dashboard/page.tsx` gets a "Chat" link.
- **Test runner:** `vitest.config.mts` with `tsconfigPaths()`, `react()` and `environment: "jsdom"`, as in the Next guide. Add `"test": "vitest run"` to `package.json`. Tests live next to their code (`lib/sse.test.ts`, `lib/api.test.ts`, `components/ChatPanel.test.tsx`).

Before writing any frontend code, read the relevant guides in `node_modules/next/dist/docs/` (the frontend's `AGENTS.md` requires it, since Next 16 differs from older versions).

## Tests

Every test file gets `docs/tests/<file>.md`. Frontend ones are named after the test file, e.g. `docs/tests/ChatPanel.test.md`.

**`backend/tests/test_chat_graph.py`** (scripted fake model with `bind_tools`, `invoke`, `stream` support as needed; `MemorySaver`):
- no tool call: the answer is returned, and the system prompt is the first message the model sees;
- a tool call: the tool runs and the model sees its result as a `ToolMessage` with the matching `tool_call_id`;
- a tool raising a plain exception: the model sees only `error: <name> failed (<Type>)`;
- the cap: a model that always calls tools gets 6 rounds, then one call with no tools bound, whose answer is returned;
- memory: on turn 2 of the same thread, the model sees turn 1's messages;
- the pseudonym is stored on turn 1 and unchanged on turn 2;
- `get_chat_model` passes `num_ctx` (in `test_llm.py`);
- live, skipped without Ollama: the lucky-number round trip through the graph (replaces the `call_with_tools` live test), and `test_live_model_picks_get_debts` through the graph.

**`backend/tests/test_chat_api.py`** (`TestClient`, the fake model injected by monkeypatching the graph's model factory, a temp checkpoint DB):
- the stream order for one turn: `tool_call`, `tool_result`, `token`… then `done`;
- `422` for an empty message, a whitespace-only message, and 4,001 characters;
- Ollama down gives the fixed message; another exception gives the generic message, and its text appears nowhere in the stream;
- the empty answer gives its fixed message;
- `GET` history after a turn is grouped correctly; an unknown thread gives `[]`;
- two users with the same `thread_id` get separate histories;
- lock: a second POST while the first stream is open gets `409`; after the first finishes, a POST succeeds; the lock is released after the generator raises, and after it's closed early;
- no message text in the captured logs (`caplog`);
- `test_live_chat_answers_which_debt_first` (the done-when). Skipped unless Ollama has `llama3.1:8b` and `nomic-embed-text` and the KB is ingested. It uses the `test_tools.py` seeded user (credit card at 22% APR and $410, student loan at 5.25%). The answer must contain "credit card" (case-insensitive) and "22", plus an `https://` URL that was one of the `search_kb` results. Run it 3 times by hand and record the score in the test doc.

**Frontend (Vitest):**
- `lib/sse.test.ts`: one event; several events in one chunk; an event split across two chunks (`rest` carries the remainder); a comment line and an unknown field are ignored; bad JSON in `data` is kept as a raw string.
- `lib/api.test.ts` (stubbed `fetch` with a `ReadableStream` body): events are delivered in order across chunk boundaries; `409` throws `ApiError` with status 409; `500` throws `ApiError`.
- `components/ChatPanel.test.tsx` (`vi.mock("@/lib/api")`):
  - tokens render as they arrive;
  - tool chips show ✓ and ✗;
  - input is disabled while streaming and enabled after `done`;
  - an `error` event shows its message;
  - `409` shows "Still answering the last message.";
  - history loads for the stored thread id;
  - "New chat" clears the list and stores a new id.

## Docs

- `architecture.md`:
  - section 9: `WS /chat` becomes `POST /chat` (SSE) and `GET /chat/{thread_id}`;
  - section 3: Python 3.12 is now true.
- Decision-log rows: the 3.12 venv; SSE over the rewrite instead of WebSocket (with the task 1 result); the hand-built graph and the 6-round cap; the per-user lock (single worker); the frontend test runner.
- README: a "Chat" note (Ollama models needed, ingest first) and `npm test` under "Tests".

## Verification

1. `cd backend && ../venv/bin/python -m pytest -q -rs`: all pass, live tests not skipped.
2. `cd frontend && npm test && npm run lint && npm run build`: all pass.
3. A manual pass on `next start`, with the result recorded in the PR:
   - ask "Which debt should I pay first?" and check the streamed answer and the chips;
   - refresh, and the history comes back;
   - "New chat" starts a fresh conversation;
   - send twice quickly, and the second send is refused with the "still answering" note;
   - stop Ollama, and the friendly error shows.

## Risks

- **The 3.12 re-resolve pulls major-version bumps** (langchain-core 1.x, chromadb). Task 0 runs the full suite before anything else. If a fix isn't small, pin the old version.
- **The Next rewrite might buffer SSE.** Task 1 settles this before any chat code, and has fallbacks.
- **`llama3.1:8b` may skip `search_kb` or forget to cite.** The system prompt asks for a citation, and the live test measures how often it happens. Record the 3-run score honestly; TPC-16 evals track it.
- **Stale figures.** The IRS Roth chart shows 2024 limits while the IRA limits page shows 2026. The prompt's "say which year" rule is the mitigation.

## Follow-ups (not this ticket)

- TPC-16: eval scenarios, including citation correctness and year mentions.
- The auth ticket: the per-user lock and the `user_id:` thread prefix are ready for real users.
