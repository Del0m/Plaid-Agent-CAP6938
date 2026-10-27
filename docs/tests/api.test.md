# Tests: `frontend/lib/api.test.ts`

These tests cover the chat calls in `lib/api.ts` (TPC-15):
- `streamChat(threadId, message, onEvent)` posts one message to `/api/chat` and calls `onEvent` for each server-sent event as it arrives.
- `getChatHistory(threadId)` loads a conversation.

`fetch` is replaced with a stub whose response body is a `ReadableStream` that delivers chosen byte chunks, so no server is needed.

Run them from `frontend/`:

```bash
npm test -- lib/api.test.ts
```

---

## `streamChat delivers events in order across chunk boundaries`

**What it does:** Sends a `sources` event, two tokens and `done`, split into three chunks at arbitrary points. It checks that all four events arrive in order and that the tokens join to "Pay it".

**Why it's there:** Real reads split events anywhere. This proves the client carries the partial tail from one read into the next.

## `streamChat decodes a character split across chunks`

**What it does:** Splits the two UTF-8 bytes of `é` across two chunks, and checks that the token text is `café`.

**Why it's there:** Decoding each chunk on its own turns a split character into `�`. Answers contain curly quotes and accented names, so the decoder has to run with `{stream: true}`.

## `streamChat throws ApiError 409 when busy`

**What it does:** Checks that a `409` response throws an `ApiError` with status 409.

**Why it's there:** The backend allows one reply at a time per user. The chat panel catches this status to show "Still answering the last message." instead of a generic error.

## `streamChat throws ApiError on 500`

**What it does:** Checks that a `500` response throws an `ApiError`.

**Why it's there:** A failed request must reach the panel as an error, not as a silent empty answer.

## `streamChat posts thread_id and message as JSON to /api/chat`

**What it does:** Checks the URL, the method, and that the body is exactly `{thread_id, message}`.

**Why it's there:** The request has to go through the `/api` rewrite, and its fields must match the backend's `ChatRequest` model. Otherwise every send gets a 422.

## `getChatHistory returns the turns`

**What it does:** Checks that it fetches `/api/chat/t1` and returns the turns unchanged, including each assistant turn's tools and sources.

**Why it's there:** The panel redraws a conversation after a refresh from exactly this data.
