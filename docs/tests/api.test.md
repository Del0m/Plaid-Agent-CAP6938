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

## Budget and alerts calls (TPC-20)

`stub(body, status)` makes `fetch` answer every call with a fresh JSON response. A response body can only be read once, so reusing one object would fail on the second call.

## `each call hits its route with the right method and body`

**What it does:** It checks every budget and alerts call's URL, method and body:
- `GET /api/budget`;
- `POST /api/budget/run` with `{"date":"2026-10-20"}`, or `{}` with no date;
- `GET /api/budget/runs/7`;
- `GET /api/alerts?status=pending_user`, and `?status=all` by default;
- `POST /api/alerts/3/accept` and `POST /api/alerts/4/dismiss`.

**Why it's there:** These are the field names and routes the backend expects (TPC-20). A typo would only show up as a 404 in the browser.

## `a 409 run reads as its detail`

**What it does:** A `409` from `runBudget` becomes an `ApiError`, and `errorMessage` gives "A budget run is already in progress.".

**Why it's there:** The Alerts page shows the server's message when a run is already going, not "Request failed (409)".

## Simulation calls (TPC-21)

## `hit the admin routes`

**What it does:** `getSim()` reads `GET /api/admin/sandbox`, `advanceSim(7)` posts `{"days":7}` to `/api/admin/sandbox/advance`, and `getSimJob(5)` reads `/api/admin/sandbox/advance/5`.

**Why it's there:** These are the routes and body the admin API expects.

## `days to month end`

**What it does:** Oct 3 is 28 days from October's end. Oct 31 and Feb 28 2028 (a leap year) both give 1, never 0.

**Why it's there:** "To month end" on the last day must still advance one day, because the API rejects 0 (plan Review Focus 3).
