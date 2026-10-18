# TPC-11: Next.js Plaid Link button and dashboard page

Jira: TPC-11 (epic TPC-2, M1). Branch: `TPC-11`, cut from `TPC-10` (the sync work is not on `main` yet; rebase onto `main` once TPC-10 merges).

## Goal

Finish Milestone 1: a user links a Plaid Sandbox bank from a Next.js page and sees their account balances, net worth, and debts sorted by APR, with a Recharts chart.

**Done when:** link `user_good` / `pass_good` in Sandbox, refresh the page, and see balances plus a credit card with its APR.

**Out of scope:** budget pace (M3), real auth (the dev-user stub in `api/deps.py` stays), frontend automated tests, live balance refresh on page load.

## Decisions

| Question | Decision |
|---|---|
| How does data arrive after linking? | `POST /plaid/exchange` runs `sync_item` inline before returning |
| Inline sync fails | Keep the item, return 200 with `synced: false`; the page offers a Retry sync button that calls the existing `POST /plaid/items/{id}/sync` |
| What is a debt? | An account with a `liabilities` row (credit, student, mortgage) |
| Net worth | `depository` + `investment` current balances minus `credit` + `loan` current balances; accounts with a null balance are skipped and counted |
| Does `/dashboard` call Plaid? | No. It reads the DB only |
| Institution name | Frontend sends `metadata.institution.name` from Link's `onSuccess` in the exchange body |
| Frontend → backend | Next.js `rewrites` proxy `/api/*` to FastAPI, so the browser stays same-origin and no CORS middleware is needed |
| Rendering | Client-rendered dashboard page (`"use client"`) |
| Pages | Single `/dashboard`; `/` redirects there |
| Chart | Horizontal bar per account, assets and debts in two colors |
| Tests | Backend pytest only; frontend verified by `npm run build`, `npm run lint`, and the manual Sandbox demo |

## Backend

### `POST /plaid/exchange` (changed, `app/api/plaid.py`)

Request:

```json
{ "public_token": "public-sandbox-...", "institution_name": "First Platypus Bank" }
```

`institution_name` is optional. When present it is written to `PlaidItem.institution_name` on both the new-item and re-link paths; when absent an existing value is left alone.

After the item is committed (new item or re-link), call `sync_item(session, item)`. Catch `PlaidError`, `httpx.HTTPError`, and the `RuntimeError` that `_fetch_sync_pages` raises when pagination keeps restarting; log the failure (error code or exception type only, never the token) and continue.

Response, always 200 once the exchange itself succeeded:

```json
{ "item_id": 1, "synced": true }
```

Errors from the exchange itself (bad `public_token`, Plaid down) and the 409 for another user's item behave exactly as today, and no sync is attempted.

### `GET /dashboard` (new, `app/api/dashboard.py`, router registered in `app/main.py`)

Scoped to `get_current_user`. Reads `Account`, `Liability`, and `PlaidItem` (for `institution_name`) only.

```json
{
  "net_worth_cents": 123400,
  "assets_cents": 150000,
  "debts_cents": 26600,
  "accounts_missing_balance": 0,
  "accounts": [
    {
      "id": 3, "name": "Plaid Checking", "mask": "0000",
      "type": "depository", "subtype": "checking",
      "current_balance_cents": 11000, "available_balance_cents": 10000,
      "institution_name": "First Platypus Bank"
    }
  ],
  "debts": [
    {
      "account_id": 7, "name": "Plaid Credit Card", "mask": "3333",
      "kind": "credit", "apr": 15.24, "balance_cents": 41000,
      "min_payment_cents": 2000, "next_due_date": "2026-11-15"
    }
  ]
}
```

Rules:
- `assets_cents` = sum of `current_balance_cents` over accounts with `type` in `{depository, investment}`.
- `debts_cents` = sum of `current_balance_cents` over accounts with `type` in `{credit, loan}`. Plaid reports these as positive amounts owed.
- `net_worth_cents = assets_cents - debts_cents`.
- Accounts of any other type are listed in `accounts` but excluded from the totals.
- An account in the asset or debt types whose `current_balance_cents` is null is excluded from the totals and counted in `accounts_missing_balance`.
- `accounts` is ordered by type (assets first, then debts, then other), then name.
- `debts` comes from `Liability` joined to `Account`, sorted by `apr` descending with null APRs last, ties broken by `balance_cents` descending.
- `balance_cents` in `debts` is the liability's stored `balance_cents` (set from the account's current balance at liability refresh).
- A user with no accounts gets zeros and empty lists, not a 404. The frontend uses empty `accounts` to show its empty state.

Response models are Pydantic classes in `dashboard.py`, matching the style of `LinkTokenResponse` and friends.

### Backend tests

`backend/tests/test_dashboard.py`, documented in `docs/tests/test_dashboard.md` (architecture section 12). Rows are inserted directly into the test DB through the `db` fixture; no Plaid calls.

- empty user returns zeros and empty lists
- net worth = assets minus debts across depository, investment, credit, loan
- `other`-type accounts are listed but excluded from totals
- null balances are excluded from totals and counted in `accounts_missing_balance`
- debts sort by APR descending, null APR last, ties by balance
- another user's accounts and liabilities never appear
- `institution_name` comes from the account's item

`backend/tests/test_plaid.py` additions, with `docs/tests/test_plaid.md` updated in the same change:

- exchange calls `sync_item` for a new item and returns `synced: true`
- exchange calls `sync_item` on re-link too
- `sync_item` raising `PlaidError` / `httpx.HTTPError` / `RuntimeError` still returns 200, `synced: false`, item saved
- `institution_name` is stored when sent and preserved when omitted on re-link
- a failed exchange does not attempt a sync

## Frontend

`frontend/` created with `create-next-app` (TypeScript, App Router, Tailwind, ESLint, no `src/` dir). Added dependencies: `react-plaid-link`, `recharts`.

### Proxy

`next.config.ts`:

```ts
async rewrites() {
  return [{ source: "/api/:path*", destination: `${process.env.BACKEND_URL ?? "http://localhost:8000"}/:path*` }];
}
```

### Files

| File | Responsibility |
|---|---|
| `lib/api.ts` | `getDashboard()`, `createLinkToken()`, `exchangePublicToken(publicToken, institutionName?)`, `syncItem(itemId)`; TS types mirroring the backend responses; throws `ApiError(status, detail)` on non-2xx |
| `lib/format.ts` | `formatCents(cents)` via `Intl.NumberFormat("en-US", { style: "currency", currency: "USD" })`, `formatApr(apr)` as `"15.24%"`, both return `"—"` for null |
| `app/page.tsx` | `redirect("/dashboard")` |
| `app/dashboard/page.tsx` | Client page: fetch on mount, hold `loading` / `error` / `data`, re-fetch after a link, choose which state to render |
| `components/PlaidLinkButton.tsx` | Fetches a link token on mount; `usePlaidLink({ token, onSuccess, onExit })`; `onSuccess(publicToken, metadata)` posts the exchange with `metadata.institution?.name`, then calls `onLinked({ item_id, synced })`; disabled while the token loads or the exchange is in flight; `onExit` with an error reports it via `onError` |
| `components/NetWorthSummary.tsx` | Three stat tiles: net worth, assets, debts; a note when `accounts_missing_balance > 0` |
| `components/BalanceChart.tsx` | Recharts horizontal `BarChart` in a `ResponsiveContainer`, one bar per account with a non-null balance in an asset or debt type; assets and debts use two distinct colors |
| `components/AccountsTable.tsx` | Name with `••mask`, type / subtype, institution, current balance |
| `components/DebtsTable.tsx` | Rows in the backend's order: name with `••mask`, kind, APR, balance, minimum payment, due date |

### Page states

- **Loading:** a short loading message.
- **Error:** banner with the error and a Retry button that re-runs the fetch.
- **Empty** (`accounts` is empty): centered "Connect a bank" card containing `PlaidLinkButton`.
- **Populated:** header with a "Link another account" `PlaidLinkButton`, then `NetWorthSummary`, `BalanceChart`, `DebtsTable`, `AccountsTable`.
- **After a link:** dashboard re-fetches; a dismissible notice says "Account linked." If `synced` is false the notice says "Linked, but loading its data failed" with a Retry sync button: it calls `syncItem(item_id)`, then re-fetches the dashboard; a second failure shows the error and keeps the button.
- **Link exit with error:** inline message near the button.

### Running locally

README gains a "Run locally" section:

```bash
# terminal 1
cd backend && ../venv/bin/uvicorn app.main:app --reload --port 8000
# terminal 2
cd frontend && npm install && npm run dev   # http://localhost:3000
```

## Docs

- `docs/architecture.md` decision log: add rows for inline sync on exchange (and `synced: false` on failure), the net worth / debt definitions, and the Next.js `/api` proxy instead of CORS.
- `docs/architecture.md` section 9: annotate `GET /dashboard` with "budget pace added in M3".

## Verification

1. `cd backend && ../venv/bin/python -m pytest` passes, including the new tests.
2. `cd frontend && npm run lint && npm run build` pass.
3. Manual demo: start both servers, open `http://localhost:3000`, click Connect, pick a Sandbox bank, log in as `user_good` / `pass_good`, refresh, and see account balances, net worth, the chart, and a credit card in the debts table with its APR.

## Follow-ups (not this ticket)

- Add "budget pace" to `/dashboard` in M3 (architecture section 9 lists it there).
- Replace the dev-user stub with real auth; the proxy keeps cookies same-origin so the frontend shouldn't need changes.
