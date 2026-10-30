# Tests: `frontend/components/ChatPanel.test.tsx`

These tests cover `components/ChatPanel.tsx`, the chat on the `/chat` page (TPC-15). It:
- sends a message with `streamChat`;
- folds each server event into the answer as it arrives: tokens append text, `tool_call` and `tool_result` make chips, `sources` makes a list of links;
- keeps one conversation per tab, with its id in `sessionStorage`, and redraws it after a refresh with `getChatHistory`.

`@/lib/api` is mocked: `streamChat` and `getChatHistory` are stubs, and the real `ApiError` is kept.

Run them from `frontend/`:

```bash
npm test -- components/ChatPanel.test.tsx
```

## Shared setup

- `heldReply()` makes `streamChat` stay open so the test can push events one at a time and then finish it. This is how a test can check the panel *while* a reply streams.
- `replyWith(events)` makes `streamChat` deliver a fixed list of events and finish.
- `send(text)` types into the "Message" box and clicks Send.
- Before each test, `sessionStorage` is cleared, history is empty, and `crypto.randomUUID` returns a fixed id.

---

## `renders tokens as they arrive`

**What it does:** Pushes the token "Pay " and checks it's on screen. Then it pushes "the card." and checks the bubble reads "Pay the card.", all before the reply has finished.

**Why it's there:** Streaming is the point of TPC-15: the answer has to show up word by word, not all at once at the end.

## `shows tool chips with ✓ and ✗`

**What it does:** One tool succeeds (`get_debts`) and one fails (`search_kb`). The test checks the chips read `get_debts ✓` and `search_kb ✗`.

**Why it's there:** The ticket asks that the page show which tools were called. A failed tool has to look different, so a person can tell the model stumbled.

## `lists sources as links`

**What it does:** Sends a `sources` event and checks there's a link named after the source's title that points at its URL.

**Why it's there:** The app lists the guidance it retrieved (spec amendment A1). These links are the answer's citations, and the model is told never to write URLs itself.

## `drops text written before a tool call`

**What it does:** Streams `CallCheck_debts()`, then a tool call, then the real answer. The test checks that only the answer is shown.

**Why it's there:** qwen2.5:14b sometimes writes text next to its tool call. That text isn't the answer, and showing it would look broken.

## `disables input while streaming and re-enables after done`

**What it does:** Checks that the message box and the Send button are disabled while a reply is open, and the box is usable again once it finishes.

**Why it's there:** The backend allows one reply at a time per user. Disabling input stops a double send before it turns into a 409.

## `shows the error event message`

**What it does:** Sends an `error` event and checks its message ("The local model isn't reachable. Is Ollama running?") appears in the answer bubble.

**Why it's there:** The backend's error messages are written for the person using the app. The panel has to show them rather than an empty bubble.

## `shows the busy note on 409`

**What it does:** Makes `streamChat` reject with `ApiError(409)` and checks the bubble says "Still answering the last message."

**Why it's there:** A 409 means another tab is mid-reply for this user. It isn't a failure, so it gets its own plain note instead of "Request failed (409)".

## `loads history for the stored thread id`

**What it does:** Stores thread id `abc`, then checks that `getChatHistory` is called with `abc` and the earlier question and answer are shown.

**Why it's there:** A refresh shouldn't lose the conversation. The id survives in `sessionStorage`, and the turns come back from the backend.

## `new chat clears messages and stores a new id`

**What it does:** Starts with a loaded conversation, clicks "New chat", and checks that the messages are gone and a new id is stored.

**Why it's there:** "New chat" must really start a fresh thread. Reusing the old id would bring the old history back on the next refresh.

## `a message sent before history loads is kept`

**What it does:** Holds the history request open, sends a message and gets its answer, then lets the old history arrive. It checks that the new answer is still there and the old history didn't replace it.

**Why it's there:** This was a real bug, found while writing the tests above. The history request from mount resolved after the first send and wiped out the new turns. A person who types fast on a slow connection would have watched their message disappear.

## `history that arrives after New chat is dropped`

**What it does:** Holds the history request for the stored thread `abc`, clicks New chat, then lets the history arrive. It checks that the old question isn't shown.

**Why it's there:** On a slow load (the phone demo over LAN, a cold backend), clicking New chat before history arrived let the old thread's turns fill the new, empty conversation. The next message then went to the new thread, whose server history didn't match the screen. The history callback now checks that the thread it loaded is still the current one. The review on #9 found it.

## `works when crypto.randomUUID is unavailable`

**What it does:** Removes `crypto.randomUUID` and checks that a send still streams an answer, with a thread id that matches the backend's `^[A-Za-z0-9-]{1,64}$`.

**Why it's there:** `randomUUID` only exists on HTTPS and localhost. The README's phone demo opens `http://<lan-ip>:3000`, where the page crashed with "a client-side exception has occurred". It now falls back to `crypto.getRandomValues`. Found in the final review; jsdom has `randomUUID`, so no earlier test could see it.

## `shows a plain note when the connection drops`

**What it does:** Makes `streamChat` reject with `TypeError("terminated")`, as a browser does when a stream is cut. It checks that the bubble says "Lost the connection to the server. Try again." and not the raw "terminated".

**Why it's there:** A dropped stream (a proxy timeout, a backend restart) used to show the browser's own word ("terminated", "Load failed", "network error"), which means nothing to a person.

## `works when sessionStorage throws`

**What it does:** Makes `sessionStorage.getItem` and `setItem` throw, as Safari private mode or blocked site data does. It checks that a send still streams an answer, using an in-memory thread id.

**Why it's there:** Storage is a convenience for surviving a refresh. Chat itself must not depend on it.
