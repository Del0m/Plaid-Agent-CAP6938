# Tests: `frontend/lib/sse.test.ts`

These tests cover `lib/sse.ts`, the parser for the server-sent events that `POST /api/chat` streams (TPC-15). The browser's `EventSource` can only make GET requests, so the chat client reads the response stream itself and passes the text through `parseSSE`. The parser returns the complete events plus any partial event left at the end (`rest`).

Run them from `frontend/`:

```bash
npm test -- lib/sse.test.ts
```

---

## `parses one event`

**What it does:** Checks that `event: token` / `data: {"text":"hi"}` followed by a blank line becomes `{event: "token", data: {text: "hi"}}`, with nothing left over.

**Why it's there:** It's the basic shape of every chat event.

## `parses several events in one buffer`

**What it does:** Checks that two events in one buffer come out as two events, in order.

**Why it's there:** A single network read often carries several tokens at once. Dropping any after the first would lose words from the answer.

## `keeps a partial event in rest`

**What it does:** Checks that an event cut off mid-JSON produces no event and comes back whole in `rest`. Also, when a complete event is followed by the start of another, the complete one is parsed and the start is kept.

**Why it's there:** Network reads end anywhere. If the client parsed a half-event, it would throw on the JSON or show a broken token. The remainder has to wait for the next read.

## `ignores comments and unknown fields`

**What it does:** Checks that a `: keep-alive` comment and an `id:` line don't produce events or break the one that follows.

**Why it's there:** Both are legal SSE. A proxy or a future backend change could add them, and the chat stream shouldn't care.

## `keeps bad JSON as a string`

**What it does:** Checks that `data: not json` gives an event whose data is the raw string, instead of throwing.

**Why it's there:** One malformed event shouldn't kill the rest of the stream.
