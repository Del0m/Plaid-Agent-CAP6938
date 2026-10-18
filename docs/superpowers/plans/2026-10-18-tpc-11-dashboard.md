# TPC-11 Dashboard and Plaid Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A user links a Plaid Sandbox bank from a Next.js page and sees balances, net worth, a Recharts chart, and debts sorted by APR.

**Architecture:** `POST /plaid/exchange` syncs the new item inline and reports `synced`. A new DB-only `GET /dashboard` computes totals and the APR-sorted debt list. A client-rendered Next.js `/dashboard` page talks to FastAPI through a `/api/*` rewrite proxy.

**Tech Stack:** FastAPI, SQLAlchemy 2, pytest (backend); Next.js App Router, TypeScript, Tailwind, `react-plaid-link`, `recharts` (frontend).

**Spec:** `docs/superpowers/specs/2026-10-18-tpc-11-dashboard-design.md`

## Global Constraints

- Money is integer cents everywhere (`*_cents: int | None`); never floats. APR is a float percentage.
- Asset types: `{"depository", "investment"}`. Debt types: `{"credit", "loan"}`. Balances are summed as stored, with no `abs()`.
- `/dashboard` never calls Plaid.
- Never log or return access tokens; log Plaid failures by `error_code` or exception type only (match `_plaid_failure` in `app/api/plaid.py`).
- Every test function gets an entry in `docs/tests/<module>.md` in the same commit (architecture section 12). Run backend tests from `backend/` with `../venv/bin/python -m pytest`.
- Comments are short, lowercase, one-line "why" notes, matching existing backend files.
- Frontend calls only same-origin `/api/...` paths; the backend URL lives only in `next.config.ts` (`process.env.BACKEND_URL ?? "http://localhost:8000"`).
- No Claude attribution in commits or PRs (no `Co-Authored-By` trailer).

## Review Focus

1. **Overdrawn or negative balances.** A depository account at -5000 lowers `assets_cents`, and a credit card with a credit balance of -2000 lowers `debts_cents`. Values are summed as-is. Pinned by `test_negative_balances_are_summed_as_is` (Task 2).
2. **A loan or credit account with no liability row.** It counts toward `debts_cents` and appears in `accounts`, but not in `debts`. Pinned by `test_debt_type_without_liability_counts_in_totals_only` (Task 2).
3. **Backend down or returning non-JSON through the proxy.** `ApiError` carries the status and the text body instead of `res.json()` throwing a `SyntaxError`, and the page shows the error banner with a working Retry. Pinned by the manual check in Task 4, Step 6.
4. **The link token request fails (no Plaid creds, backend down).** The button shows "Couldn't start Link" with the error instead of staying disabled forever. Pinned by the manual check in Task 4, Step 6.
5. **React StrictMode double effects and double clicks.** One Link success produces exactly one `/api/plaid/exchange`, and "Retry sync" is disabled while in flight. Pinned by the manual check in Task 4, Step 6.

---

### Task 1: Exchange stores institution name and syncs inline

**Files:**
- Modify: `backend/app/api/plaid.py` (`ExchangeRequest`, `ExchangeResponse`, `exchange_public_token`)
- Test: `backend/tests/test_plaid.py`
- Docs: `docs/tests/test_plaid.md`

**Interfaces:**
- Produces: `POST /plaid/exchange` with body `{ public_token: str, institution_name?: str | null }` returns `200 { item_id: int, synced: bool }`. The frontend (Task 3) relies on this shape.

- [ ] **Step 1: Stub sync by default and update existing tests**

The existing exchange tests would now reach the network through `sync_item`. Add a fixture to `test_plaid.py` that records calls and is opted into by every exchange test except `test_sandbox_link_end_to_end`:

```python
from app.api import plaid as plaid_api

# stand-in for sync_item so exchange tests stay offline; returns the items it was called with
@pytest.fixture
def synced(monkeypatch):
    calls = []
    monkeypatch.setattr(plaid_api, "sync_item", lambda session, item: calls.append(item.plaid_item_id))
    return calls
```

Add `synced` to the parameters of `test_first_link_stores_item`, `test_relinking_same_item_updates_existing_row` and `test_item_owned_by_another_user_is_409`. In `test_first_link_stores_item`, change the response assertion to `assert resp.json() == {"item_id": item.id, "synced": True}`.

- [ ] **Step 2: Write the failing tests**

```python
# linking runs a full sync right away so the dashboard has data on first load
def test_exchange_syncs_new_item(client, db, monkeypatch, synced):
    fake_exchange(monkeypatch, "item-1", "access-1")
    resp = client.post("/plaid/exchange", json={"public_token": "public-1"})
    assert resp.status_code == 200
    assert resp.json()["synced"] is True
    assert synced == ["item-1"]

# re-linking also re-syncs (new token may unlock data the old one couldn't reach)
def test_exchange_syncs_on_relink(client, db, monkeypatch, synced):
    fake_exchange(monkeypatch, "item-1", "access-1", "access-2")
    client.post("/plaid/exchange", json={"public_token": "public-1"})
    client.post("/plaid/exchange", json={"public_token": "public-2"})
    assert synced == ["item-1", "item-1"]

# a sync failure after a good exchange keeps the item and reports synced: false
@pytest.mark.parametrize("err", [
    PlaidError(500, "INTERNAL_SERVER_ERROR", "boom"),
    httpx.ConnectError("down"),
    RuntimeError("kept changing"),
])
def test_sync_failure_keeps_item_and_reports_unsynced(client, db, monkeypatch, err):
    fake_exchange(monkeypatch, "item-1", "access-1")
    def failing(session, item):
        raise err
    monkeypatch.setattr(plaid_api, "sync_item", failing)
    resp = client.post("/plaid/exchange", json={"public_token": "public-1"})
    assert resp.status_code == 200
    [item] = items(db)
    assert resp.json() == {"item_id": item.id, "synced": False}

# institution name from Link metadata is stored; a re-link without it keeps the old value
def test_institution_name_stored_and_kept(client, db, monkeypatch, synced):
    fake_exchange(monkeypatch, "item-1", "access-1", "access-2")
    client.post("/plaid/exchange", json={"public_token": "public-1", "institution_name": "First Platypus Bank"})
    assert items(db)[0].institution_name == "First Platypus Bank"
    client.post("/plaid/exchange", json={"public_token": "public-2"})
    assert items(db)[0].institution_name == "First Platypus Bank"

# a rejected public_token never gets as far as syncing
def test_failed_exchange_does_not_sync(client, db, monkeypatch, synced):
    def bad(_public_token):
        raise PlaidError(400, "INVALID_PUBLIC_TOKEN", "bad token")
    monkeypatch.setattr(plaid, "exchange_public_token", bad)
    resp = client.post("/plaid/exchange", json={"public_token": "nope"})
    assert resp.status_code == 400
    assert synced == []
    assert items(db) == []
```

Imports to add: `import httpx`, `from app.plaid_service.client import PlaidError`. In `test_sandbox_link_end_to_end`, also assert `resp.json()["synced"] is True`. It does a real sync now.

- [ ] **Step 3: Run tests to verify they fail**

Run: `../venv/bin/python -m pytest tests/test_plaid.py -v`
Expected: the new tests FAIL (no `synced` key, `sync_item` never called, `institution_name` rejected or ignored).

- [ ] **Step 4: Implement in `backend/app/api/plaid.py`**

- `ExchangeRequest` gains `institution_name: Optional[str] = None`. `ExchangeResponse` gains `synced: bool`.
- On both the re-link and new-item paths, set `institution_name` only when the request supplied it.
- After the commit on either path, call a private helper `_sync_after_link(session, item) -> bool`. It returns `True` after `sync_item(session, item)`. On `(PlaidError, httpx.HTTPError, RuntimeError)` it logs (`"sync after link failed for item %s: %s"`, with `item.id` and either `err.error_code` or `type(err).__name__`), calls `session.rollback()`, and returns `False`.
- Read `item.id` into a local before syncing and build the response from it; the rollback expires the ORM object.
- The 409 paths and exchange errors return before any sync, as today.

- [ ] **Step 5: Run tests to verify they pass**

Run: `../venv/bin/python -m pytest -v`
Expected: all PASS. Sandbox tests are skipped without creds.

- [ ] **Step 6: Document tests in `docs/tests/test_plaid.md`**

Describe the `synced` fixture under the helpers. Add a "What it does / Why it's there" entry for each new test. Update `test_first_link_stores_item` (response now includes `synced`) and `test_sandbox_link_end_to_end` (asserts a real sync). Intro: linking now also runs the first sync.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/plaid.py backend/tests/test_plaid.py docs/tests/test_plaid.md
git commit -m "feat(TPC-11): sync item inline on exchange and store institution name"
```

---

### Task 2: `GET /dashboard`

**Files:**
- Create: `backend/app/api/dashboard.py`
- Modify: `backend/app/main.py` (add `app.include_router(dashboard.router)`)
- Test: `backend/tests/test_dashboard.py`
- Docs: `docs/tests/test_dashboard.md`

**Interfaces:**
- Produces: `GET /dashboard` returning the following (field names exact, consumed by Task 3's TS types):
  - `DashboardResponse { net_worth_cents: int, assets_cents: int, debts_cents: int, accounts_missing_balance: int, accounts: list[AccountOut], debts: list[DebtOut] }`
  - `AccountOut { id: int, name: str, mask: str|None, type: str, subtype: str|None, current_balance_cents: int|None, available_balance_cents: int|None, institution_name: str|None }`
  - `DebtOut { account_id: int, name: str, mask: str|None, kind: str, apr: float|None, balance_cents: int|None, min_payment_cents: int|None, next_due_date: date|None }`

- [ ] **Step 1: Write the failing tests**

Helper at the top of `test_dashboard.py`. It writes rows directly, like `seed_item` in `test_sync.py`:

```python
# one item for user_id with the given accounts, written straight to the db. each account is a dict:
# {"name", "type", "balance": cents|None, "mask"?, "liability"?: {"kind", "apr", "balance", "min"?, "due"?}}.
# creates the User row when email is given (for "another user" tests). returns account ids in order
def seed(db, user_id, accounts, institution="Test Bank", email=None) -> list[int]: ...
```

The dev user comes from `get_current_user`. Call `client.get("/dashboard")` once before seeding, read `User.id` for `dev@localhost` from the db, and seed against that id. Name that helper `dev_user_id(client, db) -> str`.

Tests and their key assertions:

| Test | Setup | Assert |
|---|---|---|
| `test_empty_user_gets_zeros` | none | `== {"net_worth_cents": 0, "assets_cents": 0, "debts_cents": 0, "accounts_missing_balance": 0, "accounts": [], "debts": []}` |
| `test_net_worth_is_assets_minus_debts` | checking 10000 (depository), brokerage 50000 (investment), card 3000 (credit), loan 20000 (loan) | assets 60000, debts 23000, net worth 37000 |
| `test_other_types_listed_not_totaled` | checking 10000, `other` 999 | totals ignore 999; `len(accounts) == 2` |
| `test_null_balance_skipped_and_counted` | checking 10000, savings `None` (depository), card `None` (credit) | assets 10000, debts 0, `accounts_missing_balance == 2` |
| `test_negative_balances_are_summed_as_is` | checking -5000, savings 8000, card -2000 | assets 3000, debts -2000, net worth 5000 |
| `test_debt_type_without_liability_counts_in_totals_only` | loan 20000 with no liability; card 3000 with liability ("credit", 20.0, 3000) | debts 23000; `[d["name"] for d in debts] == ["card"]` |
| `test_debts_sorted_by_apr_then_balance` | liabilities: A apr 15.0 bal 100, B apr 24.99 bal 100, C apr None bal 900, D apr 15.0 bal 500 | names in order `["B", "D", "A", "C"]` |
| `test_accounts_ordered_assets_debts_other` | "zeta" depository, "alpha" credit, "mid" other, "beta" investment | names `["beta", "zeta", "alpha", "mid"]` |
| `test_other_users_data_hidden` | seed the dev user with checking 100; seed `"someone-else"` with checking 999 and a liability | only the dev user's account; debts empty; assets 100 |
| `test_institution_name_from_item` | seed with `institution="First Platypus Bank"` | `accounts[0]["institution_name"] == "First Platypus Bank"` |
| `test_debt_fields` | "Plaid Credit Card" mask "3333" with liability ("credit", 15.24, 41000), min 2000, due 2026-11-15 | `debts == [{"account_id": <id>, "name": "Plaid Credit Card", "mask": "3333", "kind": "credit", "apr": 15.24, "balance_cents": 41000, "min_payment_cents": 2000, "next_due_date": "2026-11-15"}]` |

- [ ] **Step 2: Run tests to verify they fail**

Run: `../venv/bin/python -m pytest tests/test_dashboard.py -v`
Expected: FAIL with 404 (route does not exist).

- [ ] **Step 3: Implement `get_dashboard(user, session) -> DashboardResponse` in `backend/app/api/dashboard.py`**

`router = APIRouter(tags=["dashboard"])` with `@router.get("/dashboard", response_model=DashboardResponse)`. Module constants are `ASSET_TYPES` and `DEBT_TYPES`. Query the user's accounts joined to `PlaidItem` for `institution_name`, then the user's liabilities joined to `Account`. Do the totals and both orderings in Python:
- accounts key: `(0 if asset, 1 if debt, else 2, name)`
- debts key: `(apr is None, -(apr or 0), -(balance_cents or 0))`

Register the router in `main.py` next to `plaid.router`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `../venv/bin/python -m pytest -v`
Expected: all PASS.

- [ ] **Step 5: Write `docs/tests/test_dashboard.md`**

Same structure as `test_plaid.md`: run command, shared setup (fixtures plus the `seed` and `dev_user_id` helpers), a What/Why entry per test, and a "Not covered" section. Under "Not covered", note that totals use account balances while debt rows use the liability's stored balance, which can differ between a balance refresh and a liability refresh.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/dashboard.py backend/app/main.py backend/tests/test_dashboard.py docs/tests/test_dashboard.md
git commit -m "feat(TPC-11): add GET /dashboard with net worth and APR-sorted debts"
```

---

### Task 3: Next.js scaffold, proxy and API client

**Files:**
- Create: `frontend/` via `create-next-app`
- Create/Modify: `frontend/next.config.ts`, `frontend/lib/api.ts`, `frontend/lib/format.ts`, `frontend/app/page.tsx`

**Interfaces:**
- Consumes: the Task 1 exchange shape, the Task 2 dashboard shape, and the existing `POST /plaid/link-token` `{ link_token }` and `POST /plaid/items/{id}/sync`.
- Produces (in `lib/api.ts`):
  - types `Account`, `Debt`, `Dashboard`, `ExchangeResult = { item_id: number; synced: boolean }`, mirroring Task 2 field names exactly (`number | null` for optional ints and floats, `string | null` for strings and dates)
  - `class ApiError extends Error { status: number; detail: unknown }`
  - `getDashboard(): Promise<Dashboard>`
  - `createLinkToken(): Promise<string>`
  - `exchangePublicToken(publicToken: string, institutionName?: string | null): Promise<ExchangeResult>`
  - `syncItem(itemId: number): Promise<void>`
- Produces (in `lib/format.ts`): `formatCents(cents: number | null): string` and `formatApr(apr: number | null): string`.

- [ ] **Step 1: Scaffold**

From the repo root: `npx create-next-app@latest frontend --ts --app --tailwind --eslint --no-src-dir --import-alias "@/*" --use-npm` (accept defaults for anything else it asks; if a flag is rejected by the installed version, answer the matching prompt instead). Then `cd frontend && npm install react-plaid-link recharts`. Delete the starter content in `app/page.tsx`.

- [ ] **Step 2: Proxy and redirect**

`next.config.ts` adds the `rewrites()` shown in the spec. `app/page.tsx` is a server component that calls `redirect("/dashboard")` from `next/navigation`.

- [ ] **Step 3: `lib/format.ts`**

`formatCents` uses `Intl.NumberFormat("en-US", { style: "currency", currency: "USD" })` on `cents / 100`. `formatApr` gives `"15.24%"` (two decimals). Both return `"—"` for `null`.

- [ ] **Step 4: `lib/api.ts`**

A private `request<T>(path: string, init?: RequestInit): Promise<T>` prefixes `/api`, sends JSON, and on a non-2xx response throws `ApiError`. For the detail, it reads the body as text and `JSON.parse`s it inside try/catch (falling back to the raw text), so an HTML 500 from the proxy doesn't throw a `SyntaxError` (Review Focus 3). All POSTs send `Content-Type: application/json`. Exchange sends `{ public_token, institution_name }`.

- [ ] **Step 5: Verify the proxy end to end**

Run the backend (`cd backend && ../venv/bin/uvicorn app.main:app --port 8000`) and `cd frontend && npm run dev`, then `curl -s localhost:3000/api/dashboard`.
Expected: the empty-user JSON (or real data if `dev.db` has items). `curl -sI localhost:3000/` shows a redirect to `/dashboard`.

- [ ] **Step 6: Lint and build**

Run: `cd frontend && npm run lint && npm run build`
Expected: both succeed with no errors.

- [ ] **Step 7: Commit**

```bash
git add frontend
git commit -m "feat(TPC-11): scaffold Next.js frontend with /api proxy and typed API client"
```

---

### Task 4: Dashboard page and components

**Files:**
- Create: `frontend/app/dashboard/page.tsx`, `frontend/components/PlaidLinkButton.tsx`, `frontend/components/NetWorthSummary.tsx`, `frontend/components/BalanceChart.tsx`, `frontend/components/AccountsTable.tsx`, `frontend/components/DebtsTable.tsx`

**Interfaces:**
- Consumes: everything Task 3 produces.
- Component props:
  - `PlaidLinkButton({ label: string; onLinked: (r: ExchangeResult) => void; onError: (message: string) => void })`
  - `NetWorthSummary({ data: Dashboard })`
  - `BalanceChart({ accounts: Account[] })`
  - `AccountsTable({ accounts: Account[] })`
  - `DebtsTable({ debts: Debt[] })`

- [ ] **Step 1: `PlaidLinkButton`**

`"use client"`. In `useEffect`, call `createLinkToken()`. On failure, set local error text and render "Couldn't start Link: {message}" in place of the button (Review Focus 4). Wire `usePlaidLink({ token, onSuccess, onExit })` and enable the button only when `ready && !exchanging`.

`onSuccess(publicToken, metadata)` does the following:
- guards with a `useRef` in-flight flag so it runs once (Review Focus 5)
- sets `exchanging`
- calls `exchangePublicToken(publicToken, metadata.institution?.name)`
- then calls `onLinked(result)`
- on error, calls `onError`

`onExit(err)` calls `onError(err.display_message ?? err.error_message)` when `err` is non-null.

- [ ] **Step 2: Presentational components**

- `NetWorthSummary`: three tiles (Net worth, Assets, Debts) using `formatCents`, plus the line "N account(s) had no balance and aren't included" when `accounts_missing_balance > 0`.
- `AccountsTable`: columns Account (`name ••mask`), Type (`type / subtype`), Institution, Balance.
- `DebtsTable`: columns Account (`name ••mask`), Kind, APR, Balance, Min payment, Due. Keep the backend's order. Show "No debts found." when empty.
- `BalanceChart`: `"use client"`. `ResponsiveContainer` (height 40px per bar plus 40), `BarChart layout="vertical"`, one `Bar dataKey="value"` with a `Cell` per row.
  - Rows are accounts in an asset or debt type with a non-null balance: `{ name, value: cents / 100, debt: boolean }`.
  - Assets are `#059669` and debts are `#e11d48`.
  - The tooltip uses `formatCents(value * 100)`.
  - Render nothing when there are no rows.

- [ ] **Step 3: `app/dashboard/page.tsx`**

`"use client"`. State: `data: Dashboard | null`, `error: string | null`, `loading: boolean`, `notice: { synced: boolean; itemId: number } | null`, `linkError: string | null`, `retrying: boolean`. `load()` calls `getDashboard()`; run it in `useEffect` and from Retry. Render the states exactly as listed in the spec's "Page states":
- `onLinked` sets `notice` and calls `load()`.
- The unsynced notice's "Retry sync" button is disabled while `retrying` (Review Focus 5). It calls `syncItem(itemId)` then `load()`. On success it clears the notice. On failure it keeps the notice and shows the error.

Use plain Tailwind layout: `max-w-5xl mx-auto p-6 space-y-6`, card sections with `rounded-lg border p-4`.

- [ ] **Step 4: Lint and build**

Run: `cd frontend && npm run lint && npm run build`
Expected: both succeed.

- [ ] **Step 5: Sandbox demo (the ticket's done-when)**

With both servers running and a fresh `dev.db` (`cd backend && rm -f dev.db && ../venv/bin/alembic upgrade head`):
1. Open `http://localhost:3000`. It redirects to `/dashboard` and shows the "Connect a bank" card.
2. Click Connect, choose First Platypus Bank, and log in with `user_good` / `pass_good`.
3. The page fills in with the "Account linked." notice.
4. Refresh. The tiles, chart, accounts table, and a credit card row in Debts with its APR are all present.

- [ ] **Step 6: Review Focus manual checks**

1. Stop the backend and reload. The error banner shows a status (no crash in the console). Restart the backend and click Retry, and the data loads.
2. Remove `PLAID_SECRET` from `.env` and restart the backend. The button area shows "Couldn't start Link: …". Then restore the secret.
3. In DevTools Network, link once more. Exactly one `/api/plaid/exchange` request is sent.
4. To simulate a failed sync, temporarily make `_sync_after_link` return `False`. The "Linked, but loading its data failed" notice appears. Double-clicking Retry sync sends one request at a time, and the notice clears after the sync. Then revert the temporary change.

- [ ] **Step 7: Commit**

```bash
git add frontend
git commit -m "feat(TPC-11): dashboard page with Plaid Link, net worth, chart and debts by APR"
```

---

### Task 5: Run docs and architecture decisions

**Files:**
- Modify: `README.md`, `docs/architecture.md` (section 9, section 16)

- [ ] **Step 1: README**

Replace the current two-line content with a "Run locally" section. Use the spec's two-terminal block, plus a note to copy `.env.example` to `.env` and run `../venv/bin/alembic upgrade head` from `backend/` first.

- [ ] **Step 2: Architecture doc**

- Section 9: change the line to `GET  /dashboard                 -> balances, net worth, debts by APR (budget pace added in M3)`.
- Section 16: add three rows dated `2026-10-18`:
  1. "`/plaid/exchange` syncs the item inline; on sync failure the item is kept and `synced: false` is returned". Why: the dashboard has data right after linking, and a valid token is never thrown away.
  2. "Net worth = depository + investment minus credit + loan current balances; debts list = accounts with a liabilities row, APR desc, nulls last". Why: matches what Plaid reports, and the list only shows debts with APR data.
  3. "Frontend reaches FastAPI through a Next.js `/api/*` rewrite, not CORS". Why: same-origin keeps future auth cookies simple and there's no CORS config.

- [ ] **Step 3: Full verification**

Run: `cd backend && ../venv/bin/python -m pytest -q`, then `cd ../frontend && npm run lint && npm run build`
Expected: all tests pass, and lint and build succeed.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/architecture.md
git commit -m "docs(TPC-11): run instructions and dashboard decisions"
```
