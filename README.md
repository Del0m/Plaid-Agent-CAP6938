# Plaid-Agent-CAP6938

Agentic financial dashboard: link bank accounts through Plaid, see balances, net worth and debts, and (in later milestones) chat with and get budgets from a local-LLM agent. Design: [`docs/architecture.md`](docs/architecture.md).

## Run locally

First time only:

```bash
cp .env.example .env                 # fill in PLAID_CLIENT_ID, PLAID_SECRET, FERNET_KEY
python3.12 -m venv venv && venv/bin/pip install -r backend/requirements.txt
cd backend && ../venv/bin/alembic upgrade head && cd ..
cd frontend && npm install && cd ..
```

Then, in two terminals:

```bash
# terminal 1: API on :8000
cd backend && ../venv/bin/uvicorn app.main:app --reload --port 8000

# terminal 2: web app on http://localhost:3000
cd frontend && npm run dev
```

The browser only talks to the Next.js server. It forwards `/api/*` to FastAPI on `:8000` (`BACKEND_URL` overrides that), so no CORS setup is needed.

To try it: open http://localhost:3000, click **Connect a bank**, pick **First Platypus Bank**, and log in with `user_good` / `pass_good`.

## Knowledge base

The chat agent's `search_kb` tool reads public financial guidance from a local Chroma store. One-time setup, from `backend/`:

```bash
ollama pull nomic-embed-text
../venv/bin/python scripts/fetch_kb.py      # fetches only what isn't in kb/sources/ yet (on a fresh clone: the git-ignored copyrighted page)
../venv/bin/python scripts/ingest_kb.py     # rebuilds kb/chroma/ from kb/sources/
../venv/bin/python scripts/search_kb.py "Roth IRA vs 401k"
```

Run `fetch_kb.py --refresh` to update the saved pages (for example, when the IRS publishes new contribution limits), read the changed files, then ingest again. The sources are listed in `kb/sources.json`.

## Chat

`/chat` (linked from the dashboard) answers questions about your linked accounts and cites the public guidance it used. It needs Ollama running with both models pulled, and the knowledge base ingested (see above):

```bash
ollama pull qwen2.5:14b
ollama pull nomic-embed-text
```

Answers stream in as they're written; the chips under each answer show which tools ran, and the links are the guidance the app retrieved. A conversation survives a refresh (one per tab); **New chat** starts a fresh one. One reply runs at a time per user, so run the API with a single uvicorn worker (the default).

## Budget agent and alerts

The budget agent checks spending against this month's budget. It asks the local model (`qwen2.5:14b`) why a category is overspent and what to do about it, and posts alerts to `/alerts`. A proposal to change next month's budget waits for your Accept or Dismiss. The dashboard's **Budget** card shows each category's pace.

To try it on a synthetic user with a planted overspend:

```bash
cd backend
../venv/bin/python scripts/seed_budget_user.py --profile overspend-recurring --replace
DEV_USER_EMAIL=overspend-recurring@eval.local ../venv/bin/uvicorn app.main:app
```

Then start the web app. On `/alerts`, run a budget check for **2026-10-20**, accept the $135 proposal, and run again: the agent remembers and doesn't alert twice. Other planted users are `overspend-one-time`, `overspend-seasonal` and `overspend-known-merchant`. The same run works from the terminal with `../venv/bin/python -m app.agents.budget_graph --user budget-overspend-recurring --date 2026-10-20 --no-sync`.

## Simulate a month

A synthetic user can live on a simulated date. Each advanced day injects that day's generated transactions and runs the real budget agent, so a planted month can be replayed and watched:

```bash
cd backend
../venv/bin/python scripts/seed_budget_user.py --profile overspend-one-time --replace --start default
../venv/bin/python scripts/simulate.py --user budget-overspend-one-time --days 31
```

With Ollama running, each flagged day takes a few seconds of model time, so a month takes several minutes. To drive it from the browser, start the API with `ENABLE_ADMIN=1 DEV_USER_EMAIL=overspend-one-time@eval.local`; the Alerts page then shows the simulated date with **Advance 1 day**, **Advance 7 days** and **To month end**.

## Run the agent every morning

Set `ENABLE_SCHEDULER=1` and the API runs each real user's budget check daily at `DAILY_RUN_TIME` (06:00), plus the month-end slot on the 1st at `CLOSE_RUN_TIME` (05:00), in `SCHEDULER_TZ`. If the server starts after today's slot, it runs it once at startup for anyone not yet checked today. Earlier missed days aren't replayed. Run a single uvicorn worker (the default); the scheduler lives inside it.

## Demo on a phone

1. Build and serve the web app on every network interface:
   ```bash
   cd frontend && npm run build && npm run start:lan
   ```
   (`next dev` blocks its dev resources when opened through a LAN IP, so the demo uses the production server.)
2. Find your Mac's IP: `ipconfig getifaddr en0`. If macOS asks whether `node` may accept incoming connections, allow it.
3. On a phone on the same Wi-Fi, open `http://<that-ip>:3000`. The backend stays on `localhost:8000`, because the Next.js server reaches it from the Mac.
4. If the phone can't connect (campus Wi-Fi often blocks one device from reaching another), use your phone's hotspot or run `ngrok http 3000` and open the HTTPS URL it prints.
5. Use a non-OAuth Sandbox bank such as First Platypus Bank. OAuth banks need a registered `redirect_uri`, which isn't set up.

## Tests

```bash
cd backend && ../venv/bin/python -m pytest
cd frontend && npm test && npm run lint && npm run build
```

Live tests (real Ollama models, real knowledge base) run when those are available and are skipped otherwise. Every test file, backend and frontend, is documented in [`docs/tests/`](docs/tests/).
