# Plaid-Agent-CAP6938

Agentic financial dashboard: link bank accounts through Plaid, see balances, net worth and debts, and (in later milestones) chat with and get budgets from a local-LLM agent. Design: [`docs/architecture.md`](docs/architecture.md).

## Run locally

First time only:

```bash
cp .env.example .env                 # fill in PLAID_CLIENT_ID, PLAID_SECRET, FERNET_KEY
python3 -m venv venv && venv/bin/pip install -r backend/requirements.txt
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
cd frontend && npm run lint && npm run build
```

Each backend test module is documented in [`docs/tests/`](docs/tests/).
