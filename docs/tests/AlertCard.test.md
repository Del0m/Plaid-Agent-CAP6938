# Tests: `frontend/components/AlertCard.test.tsx`

These tests cover `components/AlertCard.tsx`, one alert on the Alerts page (TPC-20). It shows the category, a severity badge, the cause and the agent's message. A pending alert gets Accept and Dismiss. A budget proposal also shows the limit the user would be accepting, and says so when a paused run is waiting on the answer.

Run them from `frontend/`:

```bash
npm test -- components/AlertCard.test.tsx
```

## Shared setup

- `alert(over)` builds a pending one-time general-merchandise alert.
- `draw(alert, busy)` renders it with mock `onAccept` and `onDismiss` callbacks.

---

## `shows the alert and calls back with its id`

**What it does:** The card shows "General merchandise", the message and "One-time". Accept and Dismiss call back with the alert's id.

**Why it's there:** The page sends that id to `POST /alerts/{id}/accept` or `/dismiss`.

## `a decided alert has no buttons and shows its outcome`

**What it does:** A dismissed alert has no buttons and reads "Dismissed".

**Why it's there:** The API refuses a second decision (`409`), so the history shows outcomes instead of buttons that would only fail.

## `buttons are disabled while busy`

**What it does:** While a decision is in flight, both buttons are disabled.

**Why it's there:** A double click would otherwise send a second request that gets `409`.

## `the proposed limit shows only for a budget adjustment`

**What it does:** An `adjust_next_budget` alert shows "Proposed limit next month: $135.00", and a plain alert shows no proposed limit.

**Why it's there:** The user should see the exact number they're accepting, which is the number period close will apply.

## `the waiting note shows only when a run is waiting on it`

**What it does:** "The agent is waiting for your decision." appears only when `awaiting_approval` is true.

**Why it's there:** It tells the user their click continues a paused run (human-in-the-loop), without claiming that for alerts nothing is waiting on.
