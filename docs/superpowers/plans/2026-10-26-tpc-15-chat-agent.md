# TPC-15 Chat Agent Graph and Streaming Chat UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/chat` page where "Which debt should I pay first?" streams back an answer naming the highest-APR card with its numbers and a cited `search_kb` URL, with the agent's tool calls shown under the answer.

**Architecture:**
- **Graph:** a hand-built LangGraph `StateGraph` (`agent` ⇄ `tools`) in `app/agents/chat_graph.py`, with a SQLite checkpointer.
- **API:** `app/api/chat.py` serves `POST /chat` as SSE and `GET /chat/{thread_id}` as history, and holds the per-user lock.
- **Frontend:** `lib/sse.ts` and `lib/api.ts` read the stream; `components/ChatPanel.tsx` renders it. It's tested with Vitest.

**Tech Stack:** Python 3.12 (new venv), FastAPI, LangGraph + `langgraph-checkpoint-sqlite`, `langchain-ollama`, pytest; Next.js 16, React 19, Vitest, React Testing Library, jsdom.

**Spec:** `docs/superpowers/specs/2026-10-26-tpc-15-chat-agent-design.md`

## Global Constraints

- **Python and dependencies:**
  - Python 3.12 venv at `venv/`, run from `backend/` with `../venv/bin/python -m pytest`.
  - New requirements: `langgraph`, `langgraph-checkpoint-sqlite`.
- **Config defaults:**
  - `LLM_NUM_CTX=8192`;
  - `CHAT_CHECKPOINT_PATH=backend/data/checkpoints.db`, resolved from `app/` with the `or` pattern like `PROMPT_AUDIT_PATH`.
- **Graph limits:**
  - `MAX_TOOL_ROUNDS = 6`; after 6 rounds the agent is called once more with **no tools bound**;
  - checkpoint thread id `f"{user.id}:{thread_id}"`;
  - `thread_id` 1–64 chars; `message` 1–4000 chars after strip.
- **SSE format:**
  - events are `event: <type>\ndata: <json>\n\n`, with JSON from `json.dumps(..., ensure_ascii=False)`;
  - types are `tool_call {id, name, args}`, `tool_result {id, name, ok}`, `token {text}`, `done {}`, `error {message}`;
  - response headers `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`.
- **Fixed copy (exact strings):**
  - `409` detail: `"A reply is already in progress."`
  - Ollama down: `"The local model isn't reachable. Is Ollama running?"`
  - Empty answer: `"The model returned an empty answer. Try rephrasing."`
  - Anything else: `"Something went wrong answering that. Try again."`
  - The frontend's message on `409`: `"Still answering the last message."`
  - `SYSTEM_PROMPT`: copied verbatim from the spec's `chat_graph.py` section.
- **What never leaves the server:**
  - tool result content is never sent to the browser;
  - message text is never logged;
  - exception text never reaches the model or the browser (`_run_tool` rules).
- **Frontend:**
  - before writing frontend code, read the matching guides in `frontend/node_modules/next/dist/docs/` (`frontend/AGENTS.md`);
  - imports use `@/`;
  - client components start with `"use client"`.
- **Test docs:** every test function gets an entry in `docs/tests/<file>.md` in the same commit. Frontend docs are named after the test file (`docs/tests/ChatPanel.test.md`).
- **Comments:** short, lowercase, one-line "why" notes, as in existing files.
- **Commits:** no Claude attribution. Prefixes `feat(TPC-15):`, `test(TPC-15):`, `docs(TPC-15):`, `chore(TPC-15):`.

## Review Focus

1. **Backend restart mid-conversation.** History and pseudonym come back from the SQLite file. Pinned by `test_history_survives_new_checkpointer` (Task 3).
2. **A multi-byte character (`é`, `’`, emoji) split across two network chunks.** It renders intact: decode with `TextDecoder` and `{stream: true}`. Pinned by `test streamChat decodes a character split across chunks` (Task 4).
3. **Model text containing blank lines** (lists, paragraphs). It stays inside one `token` event, because JSON escapes the newlines, so SSE framing never breaks. Pinned by `test_token_with_blank_lines_is_one_event` (Task 3).
4. **`sessionStorage` throws** (Safari private mode, blocked storage). Chat still works with an in-memory thread id. Pinned by `test works when sessionStorage throws` (Task 5).
5. **An odd `thread_id`** (`../x`, spaces, 65 chars). Rejected with `422` before anything touches the checkpointer, using pattern `^[A-Za-z0-9-]{1,64}$`. Pinned by `test_thread_id_must_be_simple` (Task 3).

---

### Task 0: Python 3.12 venv

**Files:** Modify `backend/requirements.txt` only if a pin is needed. Docs come in Task 6.

- [ ] **Step 1:** `/opt/homebrew/bin/python3.12 -m venv venv.new && venv.new/bin/pip install -q --upgrade pip && venv.new/bin/pip install -q -r backend/requirements.txt`. Expected: no resolver errors, and `venv.new/bin/pip check` reports "No broken requirements found."
- [ ] **Step 2:** `cd backend && ../venv.new/bin/python -m pytest -q -rs`. Expected: `148 passed`, no skips, and no LibreSSL warning. If a failure needs more than a small code fix, pin the old version from the 3.9 venv (`venv/bin/pip show <pkg>`) in `requirements.txt` and record a ruling.
- [ ] **Step 3:** `mv venv venv39.bak && mv venv.new venv`, then run the suite again from `venv/`. Expected: `148 passed`. Then `rm -rf venv39.bak`.
- [ ] **Step 4:** If `requirements.txt` changed: `git commit -m "chore(TPC-15): python 3.12 venv; pin <pkg>"`. Otherwise there's nothing to commit; record the result in the ledger.

### Task 1: Proxy streaming check (throwaway)

**Files:** Temporary route only (not committed). The result goes in the ledger, and into the decision log in Task 6.

- [ ] **Step 1:** Add a temporary `GET /dev/stream-check` to `app/main.py`: a `StreamingResponse` over a sync generator that yields `f"event: tick\ndata: {i}\n\n"` five times, with `time.sleep(0.2)` before each, and sends the two SSE headers.
- [ ] **Step 2:** Run uvicorn on `:8000`, and `cd frontend && npm run build && npm run start` on `:3000`. Then run `curl -sN http://localhost:3000/api/dev/stream-check | while IFS= read -r l; do printf '%s %s\n' "$(python3 -c 'import time;print(f"{time.time():.2f}")')" "$l"; done`. Expected: `data:` lines whose timestamps span ≥ 0.8 s. If they all share one timestamp, the proxy is buffering.
- [ ] **Step 3, only if buffered:** add `compress: false` to `next.config.ts` and retry. If it still buffers, **stop and ask** (the spec says so).
- [ ] **Step 4:** Remove the route (`git diff --stat` shows only `next.config.ts`, if step 3 applied). Ledger: `Task 1: proxy streams (span X.XXs)` or the fix used. Commit `next.config.ts` only if it changed.

### Task 2: `num_ctx`, `chat_graph.py`, and retiring `call_with_tools`

**Files:**
- Modify: `backend/requirements.txt` (add `langgraph`, `langgraph-checkpoint-sqlite`), `backend/app/config.py`, `.env.example`, `backend/app/agents/llm.py`
- Create: `backend/app/agents/chat_graph.py`, `backend/tests/fakes.py`, `backend/tests/test_chat_graph.py`
- Modify tests: `backend/tests/test_llm.py` (drop the `call_with_tools` tests and `ScriptedModel`), `backend/tests/test_tools.py` (the live test goes through the graph)
- Docs: `docs/tests/test_chat_graph.md`, `docs/tests/test_llm.md`, `docs/tests/test_tools.md`

**Interfaces:**
- Produces:
  - `config.LLM_NUM_CTX: int`, `config.CHAT_CHECKPOINT_PATH: Path`
  - `get_chat_model(temperature=None, model=None, num_ctx=None) -> BaseChatModel`
  - `_run_tool(by_name: dict, tc: dict) -> str` (unchanged, still in `llm.py`)
  - `chat_graph.SYSTEM_PROMPT: str`, `chat_graph.MAX_TOOL_ROUNDS = 6`
  - `class ChatState(MessagesState)` with fields `pseudonym: str` and `tool_rounds: int`
  - `build_chat_graph(tools, *, model=None, checkpointer=None)`, which returns the compiled graph. When `model` is None it calls the **module-level** name `get_chat_model()`, imported into `chat_graph`, so tests can monkeypatch `app.agents.chat_graph.get_chat_model`.
  - `tests/fakes.py`: `ScriptedChatModel(replies: list)`, a real `BaseChatModel` subclass, so LangGraph's `messages` stream mode sees its tokens. It has:
    - `bind_tools(tools, **kw)`, which appends `[t.name for t in tools]` to `.bound` and returns self;
    - `.seen`, a list of message lists, one per call;
    - each reply is an `AIMessage` or an `Exception` to raise;
    - text replies stream word by word through `_stream`;
    - an optional `gate: threading.Event` waited on before the first reply, used by the Task 3 lock tests.

- [ ] **Step 1: Install and configure.**
  - Add the two packages to requirements, install them, and confirm with `../venv/bin/python -c "import langgraph, langgraph.checkpoint.sqlite"`.
  - Add both settings to `config.py`, using `int(...)` for `LLM_NUM_CTX`.
  - Mirror them in `.env.example`, each with a one-line comment.

- [ ] **Step 2: Write the failing tests** in `test_chat_graph.py`. Use tools made with `@tool`: `get_lucky_number(name) -> len(name)*7+3`, one raising `ToolException("bad arg")` and one raising `RuntimeError("secret user data 4321")`. Use `MemorySaver` and `config = {"configurable": {"thread_id": "t1"}}`.
  - `test_no_tool_call_returns_answer`: the final message content is the reply text; `model.seen[0][0]` is a `SystemMessage` whose content is `SYSTEM_PROMPT`; no `SystemMessage` is stored in `graph.get_state(config).values["messages"]`.
  - `test_tool_call_round_trips`: the second call's last message is a `ToolMessage` with `tool_call_id == "c1"` and content `"24"`.
  - `test_unknown_tool_gets_error_message`: the `ToolMessage` content starts with `"error: unknown tool"`.
  - `test_tool_exception_text_reaches_model`: content is `"error: bad arg"`.
  - `test_generic_exception_text_not_forwarded`: content is `"error: boom failed (RuntimeError)"`, and `"4321"` appears in no message the model saw.
  - `test_model_error_propagates`: a reply that is `ConnectionError()` raises out of `graph.invoke`.
  - `test_round_cap_then_answer_without_tools`: 6 tool-call replies, then a text reply. `len(model.bound) == 6` (tools bound only on the first 6 calls), the 7th call happens with nothing bound, and the final content is the text reply.
  - `test_tool_rounds_reset_each_turn`: turn 1 uses 2 rounds; turn 2 is invoked with `{"messages": [...], "tool_rounds": 0}` and its first call has tools bound.
  - `test_memory_across_turns`: on turn 2, `model.seen[-1]` contains turn 1's human and AI messages.
  - `test_pseudonym_kept_in_state`: invoke turn 1 with `pseudonym="user_ab12"`, then turn 2 without it; `get_state(...).values["pseudonym"] == "user_ab12"`.
  - Live tests (skipif pattern from the old `test_llm.py`): `test_live_tool_call_round_trip` through `build_chat_graph([get_lucky_number])` asks "What is Ada's lucky number? Use the tool."; `"24"` must be in the final content.
  - In `test_llm.py`: `test_get_chat_model_passes_num_ctx` (default from config, and an override wins). Delete the old `call_with_tools` tests and `ScriptedModel`.
  - In `test_tools.py`: `test_live_model_picks_get_debts` now runs `build_chat_graph(tools)` with `MemorySaver`, with the same assertions.

- [ ] **Step 3: Run them.** `../venv/bin/python -m pytest tests/test_chat_graph.py tests/test_llm.py tests/test_tools.py -q`. Expected: ImportError for `app.agents.chat_graph`.

- [ ] **Step 4: Implement.**
  - Delete `call_with_tools` from `llm.py` and add `num_ctx` to `get_chat_model`.
  - Write `chat_graph.py` per the spec:
    - The agent node binds tools only while `state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS`.
    - The tools node returns `{"messages": [...ToolMessage...], "tool_rounds": state["tool_rounds"] + 1}`.
    - The routing function checks `getattr(last, "tool_calls", None)`.
    - `pseudonym` is a plain state key (last value wins), so a turn that omits it keeps the stored value.

- [ ] **Step 5: Run.** Run the Step 3 command, then the full suite. Expected: all pass, and the live tests run.

- [ ] **Step 6: Docs and commit.** Write `docs/tests/test_chat_graph.md`. In `test_llm.md`, remove the deleted tests and add the `num_ctx` one; in `test_tools.md`, update the live test entry. Then `git commit -m "feat(TPC-15): hand-built langgraph chat graph with tool-round cap; retire call_with_tools"`.

### Task 3: `api/chat.py`: streamed `POST /chat`, history, per-user lock

**Files:**
- Create: `backend/app/api/chat.py`, `backend/tests/test_chat_api.py`
- Modify: `backend/app/main.py` (include the router)
- Docs: `docs/tests/test_chat_api.md`

**Interfaces:**
- Consumes: `build_chat_graph`, `ChatState` (Task 2); `PrivacyGateway(session, user_id, pseudonym=...)`; `make_tools(gateway, kb)`; `KnowledgeBase()`; `get_current_user`.
- Produces:
  - `router` with `POST /chat` and `GET /chat/{thread_id}`;
  - module-level hooks the tests monkeypatch: `session_factory = SessionLocal`, `get_checkpointer() -> BaseCheckpointSaver` (a cached `SqliteSaver(sqlite3.connect(path, check_same_thread=False))`, creating the parent dir), `make_kb = KnowledgeBase`;
  - `format_event(event: str, data: dict) -> str`.

- [ ] **Step 1: Write the failing tests.**

  **Fixtures:**
  - `chat_client`: builds on the conftest `client` and `db`;
  - monkeypatch `chat.session_factory = db`, `chat.get_checkpointer = lambda: saver` (a `MemorySaver`, or `SqliteSaver` under `tmp_path` where noted), `chat.make_kb = lambda: FakeKB()` (move `FakeKB` from `test_tools.py` into `tests/fakes.py`), and `app.agents.chat_graph.get_chat_model = lambda **kw: model`;
  - a `seeded` user `u-1` like `test_tools.py`, made current with `app.dependency_overrides[get_current_user] = lambda: <u-1 User>`; `test_threads_are_per_user` swaps the override to a second seeded user.

  **Helper:** `read_events(resp) -> list[tuple[str, dict]]` parses the SSE body.

  **Tests:**
  - `test_turn_streams_tools_then_tokens_then_done`: replies are a `get_debts` call, then the text `"Pay the credit card first."`. Event types are `["tool_call", "tool_result", "token", ..., "done"]`; the tool events carry `name == "get_debts"` and `ok is True`; the joined `token` texts equal the answer.
  - `test_tool_result_content_not_sent`: no event contains `"Plaid Credit Card"` (it's in the `get_debts` output).
  - `test_failed_tool_reports_not_ok`: a call to an unknown tool gives `ok is False`.
  - `test_message_validation` (parametrized `""`, `"   "`, `"x"*4001`): `422`.
  - `test_thread_id_must_be_simple` (parametrized `"../x"`, `"a b"`, `"a"*65`, `""`): `422` for both POST and GET.
  - `test_ollama_down_message`: a `ConnectionError` reply gives exactly one `error` event with the fixed Ollama text.
  - `test_other_error_is_generic`: `RuntimeError("acct 4321")` gives the generic text, and `"4321"` is not in the raw body.
  - `test_empty_answer_message`: `AIMessage(content="")` gives the empty-answer text.
  - `test_token_with_blank_lines_is_one_event`: the reply `"a\n\nb"` (sent as a single chunk) parses into `token` events whose joined text is `"a\n\nb"`.
  - `test_history_grouped_after_turn`: `GET /chat/t1` returns `[{"role":"user","text":Q,"tools":[]},{"role":"assistant","text":A,"tools":[{"name":"get_debts","args":{},"ok":True}]}]`.
  - `test_unknown_thread_history_empty`: `[]`.
  - `test_threads_are_per_user`: with `get_current_user` overridden to user B, `GET /chat/t1` is `[]` after user A's turn on `t1`.
  - `test_history_survives_new_checkpointer`: using `SqliteSaver` on `tmp_path / "cp.db"`, run a turn, swap in a new saver on the same file, and the history equals the pre-swap history. A second turn reuses the first turn's pseudonym, checked via the audit log's `pseudonym`.
  - `test_second_request_while_streaming_gets_409`: the model has `gate` unset. Open `client.stream("POST", ...)` and, while it's open, `client.post(...)` gets `409` with the exact detail. Set the gate, drain the first stream, and the next POST is `200`.
  - `test_lock_released_after_error`: an exception reply, then the next POST is `200`.
  - `test_lock_released_when_client_disconnects`: open a stream, read one event, close it early, and the next POST is `200` within 2 s (poll).
  - `test_no_message_text_in_logs`: with `caplog` at DEBUG, neither the question nor the answer appears in `caplog.text`.
  - `test_live_chat_answers_which_debt_first` (done-when, skipped unless Ollama lists `llama3.1:8b` and `nomic-embed-text`, and `KnowledgeBase().count() > 0`):
    - real model and real KB, seeded user;
    - the joined tokens' lowercase text contains `"credit card"` and `"22"`;
    - it contains an `https://` URL that appears among the `source_url`s of the `search_kb` results for this turn. Get them by wrapping the real KB's `search` to record its results.

- [ ] **Step 2: Run them.** `../venv/bin/python -m pytest tests/test_chat_api.py -q`. Expected: ImportError, or 404s.

- [ ] **Step 3: Implement `chat.py`.** The request model uses pydantic `Field(pattern=r"^[A-Za-z0-9-]{1,64}$")` for `thread_id`, and a stripped `message` with length 1–4000; the GET path parameter uses the same pattern. Locks live in `_locks: dict[str, threading.Lock]`, guarded by `_locks_guard = threading.Lock()`.

  The response body is an **async** generator that drives the sync `graph.stream` iterator through `starlette.concurrency.iterate_in_threadpool`. Its `try/finally` closes the sync iterator, closes the DB session and releases the lock. This is the one place the code refines the spec's "sync generator": a sync generator handed straight to `StreamingResponse` isn't closed when the client disconnects, so the lock would leak. Record it as a ruling.

  Event mapping from `stream_mode=["messages","updates"]`:
  - `("updates", {"agent": {...}})` with tool calls → `tool_call` per call;
  - `("updates", {"tools": {...}})` → `tool_result` per `ToolMessage`;
  - `("messages", (chunk, meta))` with `meta["langgraph_node"] == "agent"` and string content → `token`.

  After the stream, if no `token` was sent and the last state message has neither text nor tool calls, send the empty-answer `error`. Otherwise send `done`. Exceptions are mapped to the fixed copy, with `logger.warning("chat turn failed", exc_info=True)`.

  History reads `graph.get_state(...).values.get("messages", [])` and groups it:
  - a `HumanMessage` starts a user turn;
  - AI tool calls and `ToolMessage`s attach to the following assistant turn;
  - an AI message with text closes that assistant turn.

- [ ] **Step 4: Run.** Run the Step 2 command, then the full suite with `-rs`. Expected: all pass, and `test_live_chat_answers_which_debt_first` is not skipped. Run the live test 3 times (`for i in 1 2 3; do ../venv/bin/python -m pytest tests/test_chat_api.py -k which_debt_first -q | tail -1; done`) and record the score, e.g. `3/3`, in the ledger and the test doc. A miss is a recorded result, not a reason to loosen the assertion.

- [ ] **Step 5: Docs and commit.** Write `docs/tests/test_chat_api.md`, then `git commit -m "feat(TPC-15): streamed POST /chat with history and a per-user lock"`.

### Task 4: Frontend test runner, `lib/sse.ts`, `streamChat` and `getChatHistory`

**Files:**
- Modify: `frontend/package.json` (devDeps and the `"test": "vitest run"` script), `frontend/lib/api.ts`
- Create: `frontend/vitest.config.mts`, `frontend/lib/sse.ts`, `frontend/lib/sse.test.ts`, `frontend/lib/api.test.ts`
- Docs: `docs/tests/sse.test.md`, `docs/tests/api.test.md`

**Interfaces:**
- Produces:
  - `parseSSE(buffer: string): { events: { event: string; data: unknown }[]; rest: string }`
  - `type ChatEvent = { event: "token"; data: { text: string } } | { event: "tool_call"; data: { id: string; name: string; args: Record<string, unknown> } } | { event: "tool_result"; data: { id: string; name: string; ok: boolean } } | { event: "done"; data: Record<string, never> } | { event: "error"; data: { message: string } }`
  - `type ChatTurn = { role: "user" | "assistant"; text: string; tools: { name: string; args: Record<string, unknown>; ok: boolean }[] }`
  - `streamChat(threadId: string, message: string, onEvent: (e: ChatEvent) => void, signal?: AbortSignal): Promise<void>`
  - `getChatHistory(threadId: string): Promise<ChatTurn[]>`

- [ ] **Step 1: Read and install.** Read `node_modules/next/dist/docs/01-app/02-guides/testing/vitest.md`. Then `npm install -D vitest @vitejs/plugin-react jsdom @testing-library/react @testing-library/dom vite-tsconfig-paths`, and write `vitest.config.mts` exactly as the guide gives it. Add the script. A trivial `expect(1).toBe(1)` in `lib/sse.test.ts` passing proves the setup; replace it in Step 2.
- [ ] **Step 2: Write the failing tests.**
  - `sse.test.ts`:
    - `parses one event`;
    - `parses several events in one buffer`;
    - `keeps a partial event in rest`: `"event: token\ndata: {\"te"` gives no events and the whole input as `rest`;
    - `ignores comments and unknown fields`;
    - `keeps bad JSON as a string`.
  - `api.test.ts` (stub `fetch` with `vi.stubGlobal` returning `new Response(new ReadableStream(...))`, enqueuing `Uint8Array` chunks):
    - `streamChat delivers events in order across chunk boundaries`;
    - `streamChat decodes a character split across chunks`: split the UTF-8 bytes of `é` across two chunks, and the token text is `"café"`;
    - `streamChat throws ApiError 409 when busy`;
    - `streamChat throws ApiError on 500`;
    - `getChatHistory returns the turns`;
    - `posts thread_id and message as JSON to /api/chat`.
- [ ] **Step 3: Run them.** `npm test`. Expected: failures for the missing exports.
- [ ] **Step 4: Implement.** Write `parseSSE` (split on `\n\n`; per block, `event:` and `data:` lines; `JSON.parse` with a fallback to the raw string), and `streamChat` (`TextDecoder` with `{stream: true}`, accumulating a buffer and calling `parseSSE` per read). Reuse `ApiError` and the existing error-body handling from `request<T>`.
- [ ] **Step 5: Run.** `npm test && npm run lint`. Expected: all pass, and lint is clean.
- [ ] **Step 6: Docs and commit.** Write both test docs, then `git commit -m "feat(TPC-15): vitest setup, SSE parser and streaming chat client"`.

### Task 5: `ChatPanel`, `/chat` page, dashboard link

**Files:**
- Create: `frontend/components/ChatPanel.tsx`, `frontend/components/ChatPanel.test.tsx`, `frontend/app/chat/page.tsx`
- Modify: `frontend/app/dashboard/page.tsx` (a "Chat" link)
- Docs: `docs/tests/ChatPanel.test.md`

**Interfaces:**
- Consumes: `streamChat`, `getChatHistory`, `ApiError`, and the types (Task 4).
- Produces: `export default function ChatPanel()`, plus `THREAD_KEY = "chatThreadId"`.

- [ ] **Step 1: Read.** Read the Next docs on client components and `Link` in `node_modules/next/dist/docs/01-app/`, and copy `app/dashboard/page.tsx`'s Tailwind card styling.
- [ ] **Step 2: Write the failing tests** (`vi.mock("@/lib/api")`; the `streamChat` mock replays a scripted list of events and can be held open with a deferred promise; `crypto.randomUUID` is stubbed). Use `@testing-library/react` with `screen`, `fireEvent`/`userEvent` and `findBy*`. Tests:
  - `renders tokens as they arrive`;
  - `shows tool chips with ✓ and ✗`: chip text `get_debts ✓` and `search_kb ✗`;
  - `disables input while streaming and re-enables after done`;
  - `shows the error event message`;
  - `shows the busy note on 409`: the text is `Still answering the last message.`;
  - `loads history for the stored thread id`: `sessionStorage.setItem(THREAD_KEY, "abc")`, then `getChatHistory` is called with `"abc"` and the turns render;
  - `new chat clears messages and stores a new id`;
  - `works when sessionStorage throws`: stub `Storage.prototype.getItem`/`setItem` to throw, and a send still renders the answer.
- [ ] **Step 3: Run them.** `npm test`. Expected: failures for the missing `ChatPanel`.
- [ ] **Step 4: Implement.** `ChatPanel` keeps `turns: ChatTurn[]` and `streaming: boolean`. A send appends a user turn and an empty assistant turn, then folds each event into the last turn:
  - `token` appends text;
  - `tool_call` adds a chip with `ok` pending;
  - `tool_result` sets `ok` by id;
  - `error` sets the text to the message.

  Wrap every `sessionStorage` access in `try/catch`, falling back to an in-memory id. `app/chat/page.tsx` renders a title, a link back to `/dashboard`, and `<ChatPanel />`.
- [ ] **Step 5: Run.** `npm test && npm run lint && npm run build`. Expected: all pass, and the build lists `/chat`.
- [ ] **Step 6: Docs and commit.** Write the test doc, then `git commit -m "feat(TPC-15): chat page with streamed answers and tool chips"`.

### Task 6: Docs, decision log and manual verification

**Files:** Modify `docs/architecture.md` (sections 3, 9 and 16), `README.md`.

- [ ] **Step 1: Architecture.**
  - Section 9: replace `WS /chat` with `POST /chat (SSE)` and `GET /chat/{thread_id}`.
  - Section 3: Python 3.12 now holds.
  - Section 16: add five rows dated 2026-10-26:
    - the 3.12 venv;
    - SSE through the rewrite instead of WebSocket, with the Task 1 result;
    - the hand-built graph and the 6-round cap with a final no-tools call;
    - the per-user lock (`409`, single worker);
    - Vitest + RTL for the frontend.
- [ ] **Step 2: README.** Add a "Chat" section: needs `llama3.1:8b` and `nomic-embed-text` pulled and `ingest_kb.py` run; open `/chat`. Under "Tests", add `cd frontend && npm test`.
- [ ] **Step 3: Manual pass.** On `next build && next start` plus uvicorn, run the spec's Verification step 3 list and record each result in the ledger for the PR:
  - the debt question streams, with chips;
  - refresh restores the history;
  - "New chat" starts fresh;
  - a quick double send shows the busy note;
  - with Ollama stopped, the friendly error shows.
- [ ] **Step 4: Commit.** `git commit -m "docs(TPC-15): chat endpoint, streaming and test runner in architecture, decision log and README"`.
