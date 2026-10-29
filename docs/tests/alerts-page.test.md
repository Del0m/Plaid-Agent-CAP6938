# Tests: `frontend/app/alerts/page.test.tsx`

These tests cover the Alerts page, `app/alerts/page.tsx` (TPC-20):
- **The run button.** "Run budget check" starts a budget run with an optional date (blank means today), then asks `GET /budget/runs/{id}` every 2 seconds until it's done. It says what happened: alerts written, waiting for approval, no budget, or failed.
- **The alert lists.** Pending alerts have Accept and Dismiss; decided alerts are listed under History.
- **Reloading.** The list reloads after every run and decision.
- **Simulation (TPC-21).** For a simulated user, with admin on, a panel shows the simulated date. It has **Advance 1 day**, **Advance 7 days** and **To month end** buttons, polls the job, and shows progress and then the result.

Run them from `frontend/`:

```bash
npm test -- app/alerts/page.test.tsx
```

## Shared setup

- `@/lib/api` is mocked with `vi.mock`, as in `ChatPanel.test.tsx`. `getAlerts`, `runBudget`, `getRun`, `acceptAlert` and `dismissAlert` are scripted per test, and `errorMessage` and `ApiError` stay real.
- **Fake timers** (`shouldAdvanceTime`) let the 2-second poll run instantly.
- **`alert(id, over)`** builds an alert.
- **`run(over)`** builds a run status.
- **`runThrough(...statuses)`** clicks Run and advances 2 seconds per scripted status.
- The default alert list is alert 2 (pending) and alert 1 (accepted).
- `getSim` answers `404` by default (admin off). Simulation tests set it to `SIMULATED` (the one-time user at Oct 3), and `job(over)` builds a job status.

---

## `loads pending and history`

**What it does:** The page shows "Pending" with alert 2, and "History" with alert 1. There is exactly one Accept button, the pending alert's.

**Why it's there:** It's what the user sees first. Decided alerts mustn't offer buttons that would only get `409`.

## `run polls until done then shows result and reloads`

**What it does:** The run reads `running`, then `ok` with one alert. The page polls run 7 until it's done, says "1 alert written.", and reloads the list.

**Why it's there:** A run outlasts the Next.js proxy's 30-second limit, so the page polls instead of waiting on one request. The reload shows the new alert.

## `run awaiting shows approval line`

**What it does:** A run that ends `awaiting_user` with one proposal says "Waiting for your approval on 1 proposal.".

**Why it's there:** It tells the user the agent paused for them (human-in-the-loop) and where to act.

## `run with no budget says so`

**What it does:** A run that ends `no_budget` says "No budget to check for that date.".

**Why it's there:** "No new alerts" would wrongly suggest the budget was checked and found fine (plan Review Focus 4).

## `run error shows failure`

**What it does:** A run that ends `error` says "The run failed (ConnectionError).".

**Why it's there:** An Ollama outage should be visible and named, not look like a quiet day.

## `a run that never finishes stops polling after 10 minutes`

**What it does:** If the run stays `running`, the page polls 300 times (10 minutes), then says "Still running. Check back later." and frees the button.

**Why it's there:** It was found in the TPC-20 review: a run whose server stopped never finishes, and the page used to spin forever.

## `leaving the page stops polling`

**What it does:** After the page is closed mid-run, it stops asking about the run.

**Why it's there:** A loop left running after the user navigates away would keep hitting the API, and would try to update a page that's gone.

## `409 shows message`

**What it does:** When a run is already in progress, the page shows the server's message.

**Why it's there:** A second tab or a double click gets a clear reason instead of a generic failure.

## `accept reloads`

**What it does:** Accepting alert 2 calls `acceptAlert(2)`, then reloads the list.

**Why it's there:** The decided alert moves to History, and a paused run's alert stops saying the agent is waiting.

## Not covered

- **The dashboard's Budget card and Alerts link** have no component test. They reuse `BudgetPaceBar` (tested) and the API calls (tested), and `npm run build` checks the page compiles and prerenders.

## `no panel when admin is off`

**What it does:** When `GET /admin/sandbox` answers `404`, no simulation panel appears.

**Why it's there:** A normal deployment has admin off, and the page must look the same as before TPC-21.

## `no panel for a real user`

**What it does:** For a user who isn't simulated, no panel appears.

**Why it's there:** Real users live on real time, and can't be advanced.

## `advance 7 days polls and reloads`

**What it does:** The panel shows "Simulated date: Oct 3, 2026". **Advance 7 days** posts 7, shows "Day 3 of 7…" while the job runs, then "Advanced 7 days to Oct 10, 2026: 7 runs, 1 alert written.", and reloads the alerts and the simulated date.

**Why it's there:** It's the dashboard way to drive a simulation and watch the agent's alerts appear day by day.

## `simulation error shows where it stopped`

**What it does:** A job that ends in error shows "The simulation stopped on Oct 5, 2026 (ConnectionError).".

**Why it's there:** The user can fix the cause (start Ollama) and advance again from that day.

## `simulation 409 shows message`

**What it does:** When a run or simulation is already going, the panel shows the server's message.

**Why it's there:** A double click, or the morning job, explains itself instead of failing silently.

## `run button disabled while simulating`

**What it does:** While a simulation runs, the run button is disabled.

**Why it's there:** The server would refuse it anyway (one lock per user), and the page shouldn't offer it.

## `month end on the last day advances one`

**What it does:** On Oct 31, **To month end** advances 1 day.

**Why it's there:** It never sends 0, which the API rejects (plan Review Focus 3).
