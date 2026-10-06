# Architecture: Agentic Financial Dashboard

Term project, agentic track. Author: Armin Delmo (UCF).
Status: living design doc. Update it as you make decisions; the "Decision log" at the bottom is where those go.

This doc is written so you can build the system one milestone at a time. Each section says what the piece does, why it's shaped that way, and what you'll learn building it. Where there was a real choice, the default is stated and the alternatives are listed so you can swap them deliberately.

---

## 1. What we're building

A web dashboard where a user links bank accounts through Plaid, sees balances and debts at a glance, chats with an assistant grounded in their own data, and gets goals they can realistically hit.

The novel part is the **dynamic budget agent**: a scheduled, stateful loop that builds a per-category budget from history, watches spending every day, figures out *why* a category is running hot, decides what to do about it, and adapts the next period's budget. It is agentic because it has:

| Property | Where it lives |
|---|---|
| Persistent state | App DB (budgets, alerts, advice log) + LangGraph checkpointer |
| Autonomous scheduled runs | Scheduler triggers the Budget Agent daily and the Period-Close Agent at period end |
| Multi-step tool use | LangGraph nodes call tools (sync, query spend, search KB, create alert) |
| Adaptive decisions | LLM classifies the cause of an overspend and picks an action; budget is re-fit each period |

### Non-goals (for now)
- Real money movement (stretch goal only, section 11).
- Multi-tenant production hardening. One instance, a handful of test users.
- Mobile app.

---

## 2. Diagrams

All sources are PlantUML in [`diagrams/`](diagrams/). Rendered PNG and SVG sit next to each source.

| Diagram | What it shows |
|---|---|
| [`system-design.puml`](diagrams/system-design.puml) | Every component and how data flows between them |
| [`plaid-link-sequence.puml`](diagrams/plaid-link-sequence.puml) | Linking an account and the first transaction sync (Milestone 1) |
| [`budget-agent-loop.puml`](diagrams/budget-agent-loop.puml) | One scheduled run of the budget agent as LangGraph nodes (Milestone 3) |

To re-render after editing:

```bash
# needs Java + Graphviz (brew install graphviz / apt install graphviz)
java -jar plantuml.jar -tsvg docs/diagrams/*.puml
# or use the PlantUML extension in VS Code (Alt+D to preview)
```

![System design](diagrams/system-design.png)

---

## 3. Tech stack

| Layer | Default | Why | Alternatives |
|---|---|---|---|
| Frontend | **Next.js (React, TypeScript)** + Tailwind + Recharts | Plaid ships `react-plaid-link`; Recharts covers balance and budget-pace charts | Vite + React, SvelteKit |
| Backend API | **FastAPI (Python 3.12)** (decided) | LangGraph, ChromaDB and the Plaid Python SDK are all Python, so one language for backend and agents | Node/Express (then agents become a separate service) |
| Agents | **LangGraph** | Explicit state graphs, checkpointing, `interrupt()` for human approval | Plain tool-use loop, CrewAI |
| LLM | **Local Llama via Ollama** (`langchain-ollama` `ChatOllama`), behind a single `llm.py` wrapper. Agent model: **`llama3.1:8b`** (`mistral` as a benchmark comparison). Judge model for evals: **`qwen2.5:14b`**. Runs on a MacBook Pro M5, 24 GB (70B models don't fit) | Financial data never leaves your machine; free to run thousands of eval calls; wrapper still lets you swap models for benchmarking | A hosted API (Claude, OpenAI) as a comparison baseline |
| Embeddings | **`all-MiniLM-L6-v2` (sentence-transformers), run locally** | ChromaDB's default; nothing leaves your machine; same model can score semantic similarity in evals | `nomic-embed-text` via Ollama |
| Vector store | **ChromaDB** (persistent client, local dir) | Required by the proposal; zero ops | pgvector |
| App DB | **SQLite** in dev, **PostgreSQL** later, via **SQLAlchemy 2 + Alembic** | Start simple; migrations keep the schema honest | Supabase |
| Agent state | **LangGraph checkpointer** (`SqliteSaver`, then `PostgresSaver`) | Resumable runs, per-user thread memory | Roll your own |
| Scheduler | **APScheduler** inside the backend | One process, easy to trigger manually in dev | cron + CLI, Celery beat |
| Secrets | `.env` + `python-dotenv`; Plaid access tokens encrypted at rest with `cryptography.Fernet` | Access tokens are the keys to a user's bank data | KMS / Vault |

**Plaid environments:** build entirely in **Sandbox** (fake banks, test users). Plaid retired the old "Development" environment; for real accounts you'd apply for **Limited Production**. Sandbox is enough for the whole project including benchmarks.

---

## 4. Components

Refer to `system-design.png` while reading.

### 4.1 Frontend
- **Dashboard:** net worth, account balances, debts sorted by APR, and a "budget pace" bar per category (green under 100%, amber 100-110%, red above).
- **Plaid Link widget:** `react-plaid-link`. Gets a `link_token` from the backend, returns a `public_token`. Bank credentials go to Plaid only.
- **Chat UI:** streams tokens over a WebSocket (or Server-Sent Events). Shows which tools the agent called, which is great for debugging and for your demo.
- **Goals & Alerts:** list of agent alerts with Accept / Dismiss buttons; accepted suggestions feed back into the agent's memory.

### 4.2 Backend API (FastAPI)
- **Auth & Session:** keep it simple: email + password with hashed passwords (or a single demo user). Every row in the DB is scoped by `user_id`.
- **Routes:** see section 9.
- **Plaid Service:** the only module that talks to Plaid. Wraps link token creation, token exchange, `/transactions/sync`, `/accounts/balance/get`, `/liabilities/get`, and webhook handling.
- **Privacy Gateway:** the only module that builds LLM prompts from user data. Section 7.
- **Local LLM (Ollama):** runs on the same machine as the backend at `http://localhost:11434`. No API key.
- **Scheduler:** registers per-user jobs: daily sync + budget run; period close.

### 4.3 Agent layer (LangGraph)
Four graphs, sharing one tool set:

| Graph | Trigger | Shape |
|---|---|---|
| **Chat Agent** | User message | ReAct loop: LLM ↔ tools until it answers |
| **Budget Agent** | Daily schedule | Fixed pipeline with one LLM branch (diagnose + decide). See `budget-agent-loop.png` |
| **Period-Close Agent** | Period end | Compare budget vs actual, apply adjustments, open next period |
| **Goal Agent** | User request or after period close | Gather debts/income/surplus → propose goals → user approves via `interrupt()` |

**Deterministic Budget Engine:** plain Python functions for totals, pace, thresholds and budget fitting. The LLM never does arithmetic that matters. This is a core design rule: it makes the system testable and keeps the benchmarks meaningful.

### 4.4 Knowledge base (RAG)
ChromaDB collection `fin_guidance`, filled by an offline ingestion script from curated public sources (section 8). The `search_kb` tool returns top-k chunks with source URLs so answers can cite them.

### 4.5 Evaluation harness
Offline Python package that replays scenarios against the agents and computes metrics (section 10).

---

## 5. Data model

Start with these tables (SQLAlchemy models). Amounts are stored as integer cents to avoid float errors.

```
users            id (uuid), email, password_hash, created_at, prefs_json
plaid_items      id, user_id, plaid_item_id, access_token_enc, institution_name,
                 txn_cursor, status, created_at
accounts         id, user_id, item_id, plaid_account_id, name, mask, type, subtype,
                 current_balance_cents, available_balance_cents, limit_cents, updated_at
liabilities      id, user_id, account_id, kind (credit|student|mortgage), apr,
                 min_payment_cents, next_due_date, balance_cents
transactions     id, user_id, account_id, plaid_txn_id (unique), date, amount_cents,
                 merchant_name, pf_category_primary, pf_category_detailed,
                 our_category, is_recurring, pending, removed
budget_periods   id, user_id, start_date, end_date, status (open|closed)
budget_lines     id, period_id, category, kind (fixed|flexible), limit_cents,
                 spent_cents, last_pace, source (init|adapted|user)
alerts           id, user_id, period_id, category, severity, cause (one_time|seasonal|recurring),
                 action (alert|suggest_cuts|adjust_next_budget), message, status
                 (pending_user|accepted|dismissed), created_at
advice_log       id, user_id, run_id, category, advice_text, created_at, outcome
goals            id, user_id, kind (debt_payoff|savings|emergency_fund), target_cents,
                 target_date, monthly_cents, rationale, status
agent_runs       id, user_id, graph, started_at, finished_at, status, summary_json
```

Notes:
- `plaid_txn_id` is unique so re-syncing is idempotent (upsert).
- `/transactions/sync` returns `added`, `modified`, `removed`; handle all three. Pending transactions get replaced by posted ones with a new id.
- Plaid amounts are **positive for money leaving the account**.
- **Sign convention (decided):** `transactions.amount_cents` keeps Plaid's sign as-is: **positive = money out** (purchases, bills, transfers out), **negative = money in** (paychecks, refunds, transfers in). Store Plaid's amount converted to cents without flipping it. When summing spending for a category, add up the positive amounts, and flip the sign only when displaying.

---

## 6. The dynamic budget agent (Milestone 3, the core)

### 6.1 State

```python
class BudgetState(TypedDict):
    user_id: str
    period_id: int
    today: date
    new_txns: list[dict]            # from sync
    totals: dict[str, int]          # category -> spent_cents this period
    flags: list[OverspendFlag]      # from detect_overspend
    diagnoses: list[Diagnosis]      # LLM output, structured
    actions: list[Action]           # LLM output, structured
    period_ended: bool
```

Each node takes the state and returns a partial update. That's the whole LangGraph model; once this clicks, the rest is wiring.

### 6.2 Nodes

| Node | LLM? | Does |
|---|---|---|
| `load_state` | no | Load open period, budget lines, recent advice |
| `initialize_budget` | no | Only if no budget exists. See 6.3 |
| `sync_transactions` | no | Call Plaid Service sync, upsert rows |
| `categorize` | rarely | Use Plaid's primary `personal_finance_category` as the budget category (decided); send only uncategorized/ambiguous merchants to the LLM |
| `update_totals` | no | Recompute `spent_cents` per line |
| `detect_overspend` | no | See 6.4 |
| `diagnose` | **yes** | For each flag: classify cause as one-time / seasonal / recurring, with evidence |
| `decide_action` | **yes** | Pick `alert`, `suggest_cuts`, `adjust_next_budget`, or `no_op`; must check `advice_log` to avoid repeating itself |
| `log_advice` | no | Write alerts + advice_log |
| `close_period` | some | Section 6.5 |
| `persist_state` | no | Write `agent_runs` row |

Use **structured output** (Pydantic models) for `diagnose` and `decide_action` so the graph can branch on the result and the benchmarks can score it.

### 6.3 Initial budget (from ~90 days of history)

When creating the Plaid link token, set `transactions.days_requested` to at least 90.

1. Drop transfers, income, and credit-card payments (they're not spending; they'd double count).
2. Mark **fixed** categories (rent, utilities, loan payments, insurance, subscriptions flagged recurring by Plaid). Budget = the most recent amount.
3. For **flexible** categories, compute monthly totals for each of the last 3 months. Budget = **median** × 1.05. Median, not mean, so one vacation doesn't inflate the budget.
4. Store which transactions you treated as outliers so the agent can explain the budget.

Learning exercise: compare median vs mean vs trimmed mean on your benchmark users and record the "initial budget accuracy" metric for each.

### 6.4 Overspend detection

Budget periods are **calendar months** (decided). `days_in_period` is the length of the current month.

```
elapsed_fraction = days_elapsed / days_in_period
pace = spent / (limit * elapsed_fraction)
flag if pace >= 1.10 and days_elapsed >= 5     # flexible categories
flag if spent > limit                          # any category, any day
```

The `days_elapsed >= 5` guard avoids flagging on day 1 because of one grocery trip. Fixed categories are lumpy (rent lands on the 1st), so judge them only against their limit, not pace. Tune these thresholds with the benchmark, then record the chosen values in the decision log.

### 6.5 Period close and adaptation
At the end of each calendar month: for each line, compare `limit` vs actual. Apply any `adjust_next_budget` actions the user accepted. For lines without an action, nudge toward actual with smoothing: `new = 0.7 * old + 0.3 * actual`, clamped to ±15% per period. This keeps the budget from chasing one bad month. The "does the budget improve over time" benchmark checks this works.

### 6.6 Human in the loop
Anything that changes the user's money or their budget without being asked goes through LangGraph's `interrupt()` and becomes an alert with status `pending_user`. The agent proposes; the user disposes.

---

## 7. Privacy design

Rule: **the Privacy Gateway is the only code path that puts user data into an LLM prompt.** Agents call `gateway.build_context(user_id, purpose, ...)`, never the raw DB.

| Sent to the LLM | Never sent |
|---|---|
| Amounts, dates, categories | Name, email, address, phone |
| Merchant names (needed for advice like "cancel Hulu") | Account and routing numbers, masks |
| APRs, minimum payments, balances | Plaid `access_token`, `item_id`, account ids |
| A per-session pseudonym (`user_7f3a`) | Your internal `user_id` |
| Aggregates where detail isn't needed | Free-text fields that might contain PII (transaction `name` raw strings can include names) |

Implementation steps:
1. **Allowlist, not blocklist.** The gateway builds context from explicit fields. New fields don't leak by accident.
2. **Minimize per purpose.** `diagnose` for "Dining" gets dining transactions only, not the full ledger.
3. **Scrub** merchant strings with a regex pass (emails, long digit runs, "Zelle to FIRSTNAME") before they're included.
4. **Pseudonymize** with a random token per session; map back only in the backend.
5. Use Plaid's `client_user_id` = a random UUID, never an email.
6. Log every prompt the gateway builds (locally) so you can audit what left the system. This log is also great evidence for your report.

Because the LLM runs locally (Ollama), prompts never leave your machine, which removes the main leak risk from the proposal. Keep the gateway anyway: it still limits what ends up in logs and traces, it keeps prompts small (which matters a lot for an 8B model's accuracy and speed), and it means a hosted model can be swapped in for comparison without a privacy regression. In the report, say clearly that the privacy guarantee comes from local inference *plus* minimization, and that a hosted model would rely on minimization alone.

---

## 8. Knowledge base (RAG)

**Sources** (public, citable): CFPB consumer guides, IRS pages on IRA/401(k) limits, investor.gov, FDIC on savings accounts, a couple of well-known methods (avalanche vs snowball debt payoff, 50/30/20). Save them as markdown/HTML in `kb/sources/` with the URL and fetch date in front matter.

**Ingestion** (`scripts/ingest_kb.py`): split into ~500-token chunks with ~50-token overlap, embed, upsert into Chroma with metadata `{source_url, title, topic, fetched_at}`.

**Retrieval** (`search_kb` tool): top-k=4, return chunk text + source URL. The chat agent should cite sources in answers.

Note on vocabulary: the proposal says "knowledge graph." What's described (LangGraph + ChromaDB, retrieve guidance) is a **vector-store RAG** plus an **agent graph**. That's fine; just use the precise terms in the report. A real knowledge graph (entities + relations) would be an optional extension.

---

## 9. API surface (FastAPI)

```
POST /auth/register, /auth/login
POST /plaid/link-token          -> { link_token }
POST /plaid/exchange            { public_token } -> 200
POST /plaid/webhook             (Plaid calls this)
GET  /dashboard                 -> balances, debts, budget pace
GET  /transactions?from&to&category
GET  /budget/current            -> period + lines
POST /budget/lines/{id}         (user override)
GET  /alerts ; POST /alerts/{id}/accept | /dismiss
GET  /goals ; POST /goals/generate ; POST /goals/{id}/accept
WS   /chat                      streaming chat with the Chat Agent
POST /admin/run/{graph}?user_id (dev only: trigger an agent run manually)
POST /admin/sandbox/advance     (dev only: inject sandbox transactions, see 12)
```

---

## 10. Evaluation and benchmarks (Milestone 5)

Build the harness **early** (alongside Milestone 2), even with only two scenarios. It's much easier to grow than to retrofit.

### 10.1 Test data
- **Synthetic users:** JSON fixtures of accounts, liabilities and ~6 months of transactions with known ground truth (e.g., "Dining spend jumps 40% in month 5 because of a recurring new subscription"). Load them straight into the DB, bypassing Plaid, so tests are fast and deterministic.
- **Plaid Sandbox users:** `user_transactions_dynamic` and custom sandbox users for end-to-end runs.

### 10.2 Metrics

| Metric | How | Target |
|---|---|---|
| Golden-answer match | Each scenario has a golden action (e.g. "pay the 24% APR card first"). LLM judge or rule check: does the answer contain it? | report % |
| Semantic similarity | Cosine similarity between agent answer and golden answer embeddings | **≥ 0.85** (decided; note in the report that the proposal said 0.90) |
| Coherence / safety | LLM-as-judge rubric (coherent, consistent with the user's numbers, not unsafe) on a 1-5 scale, plus a few human spot checks | proposal: **85%** pass |
| Specificity | Rubric or classifier: does advice name a concrete vehicle/action (Roth IRA, HYSA, specific subscription)? | report % |
| Initial budget accuracy | MAPE between initial budget and next month's actual per category | lower is better |
| Time-to-flag | Days between a scenario's overspend crossing 10% and the alert being created | ≤ 1 run |
| Budget improvement | MAPE per period over 3+ simulated periods; should trend down | trend |
| False alarm rate | Alerts on scenarios with no real overspend | low |

### 10.3 Tips
- Fix the LLM `temperature` (e.g. 0) during evals and run each scenario 3 times; report mean and spread.
- Don't use the same model as both agent and judge if you can avoid it, or at least report that you did. The agent is `llama3.1:8b` and the judge is `qwen2.5:14b`, so a different model family grades the answers.
- Track latency too: a local model's speed per agent run is a real result worth reporting.
- Save every run's outputs as JSON in `eval/results/<date>/` so you can chart progress in the final report.

---

## 11. Stretch goal: "fun money" transfers

Only in Sandbox. Plaid's Transfer product needs separate approval for production and moves real money, so treat it as a demo of the *decision* layer:
- The agent may only **propose** a transfer; every one goes through `interrupt()` and explicit user confirmation.
- Hard caps in code (not in the prompt): max per transfer, max per week, only between the user's own accounts.
- Log every proposal and decision.

---

## 12. Development workflow tips

- **Sandbox time travel:** Sandbox won't produce new transactions on its own the way a real bank does. For the daily loop, either call `/sandbox/transactions/create` (for dynamic sandbox users) or load the next day's slice from a synthetic fixture. Wrap both behind `POST /admin/sandbox/advance` so you can simulate a month in a minute.
- **Run graphs by hand:** every graph should be callable from a CLI (`python -m app.agents.budget --user demo --date 2026-11-03`) before you put it on a schedule.
- **Visualize the graph:** `graph.get_graph().draw_mermaid()` prints the LangGraph structure. Compare it to `budget-agent-loop.png`.
- **LangSmith (optional):** free tier tracing shows each node's inputs/outputs. Very helpful when debugging tool calls. Mind privacy: traces contain prompts.

---

## 13. Suggested repository layout

```
plaid-agent/
├── README.md
├── docs/
│   ├── architecture.md          # this file
│   └── diagrams/*.puml
├── frontend/                    # Next.js
│   ├── app/                     # routes: /, /dashboard, /chat, /goals
│   ├── components/              # PlaidLinkButton, BudgetPaceBar, ChatPanel, AlertCard
│   └── lib/api.ts
├── backend/
│   ├── pyproject.toml
│   ├── .env.example             # PLAID_CLIENT_ID, PLAID_SECRET, PLAID_ENV=sandbox, OLLAMA_BASE_URL, OLLAMA_MODEL, FERNET_KEY
│   ├── alembic/
│   └── app/
│       ├── main.py              # FastAPI app, routers, scheduler start
│       ├── config.py
│       ├── db/                  # models.py, session.py
│       ├── api/                 # auth.py, plaid.py, dashboard.py, budget.py, chat.py, admin.py
│       ├── plaid_service/       # client.py, sync.py, webhooks.py
│       ├── privacy/             # gateway.py, scrub.py
│       ├── budget_engine/       # fit.py, pace.py, close.py   (pure functions, unit-tested)
│       ├── agents/
│       │   ├── llm.py           # model wrapper
│       │   ├── tools.py
│       │   ├── chat_graph.py
│       │   ├── budget_graph.py
│       │   ├── close_graph.py
│       │   └── goal_graph.py
│       ├── rag/                 # store.py, search.py
│       └── scheduler.py
├── kb/sources/                  # curated guidance docs
├── scripts/ingest_kb.py
├── eval/
│   ├── scenarios/               # golden JSON fixtures
│   ├── metrics.py
│   ├── run_eval.py
│   └── results/
└── tests/                       # pytest: budget_engine, privacy, plaid sync idempotency
```

---

## 14. Build plan by milestone

Each milestone ends with something you can demo. "Learn" lists the concept you'll come away with.

### Schedule (tracked in Jira project TPC, "Term Project CAP6938")
Assignment due **November 9, 2026**; everything is scheduled to finish by **November 7** for two days of buffer.

| Epic | Jira | Dates |
|---|---|---|
| M1 Dashboard + Plaid linking | TPC-2 (stories TPC-1, 8-11) | Oct 5 - Oct 11 |
| M2 Chat agent | TPC-3 (TPC-12 to 16) | Oct 12 - Oct 18 |
| M3 Autonomous budget agent | TPC-4 (TPC-17 to 22) | Oct 19 - Oct 28 |
| M4 Goal generation | TPC-5 (TPC-23 to 25) | Oct 29 - Nov 1 |
| M5 Benchmarking | TPC-6 (TPC-26 to 28) | Nov 2 - Nov 5 |
| Writeup explaining these docs | TPC-7 (TPC-29, 30) | Nov 4 - Nov 7 |


### M1. Dashboard with Plaid linking
1. Plaid Sandbox account and keys; FastAPI skeleton with `/health`.
2. DB models + Alembic migration for users, plaid_items, accounts, liabilities, transactions.
3. `/plaid/link-token` and `/plaid/exchange`; encrypt access token.
4. `/transactions/sync` loop with cursor; upsert; handle removed.
5. Next.js page with Plaid Link button and dashboard (balances, debts by APR).
- **Done when:** link `user_good` in Sandbox, refresh, see balances and a credit card with APR.
- **Learn:** OAuth-style token exchange, cursor-based incremental sync, idempotent upserts.

### M2. Agent that reads data and answers questions
1. `llm.py` wrapper around `ChatOllama`; first check that your Llama model returns tool calls correctly with one toy tool. Then Privacy Gateway v1 (allowlist + pseudonym).
2. Tools: `get_accounts`, `get_debts`, `get_spending(category, from, to)`, `search_kb`.
3. KB ingestion with 10-20 source docs.
4. Chat graph (ReAct) + WebSocket streaming.
5. Eval harness skeleton with 5 golden Q&A scenarios.
- **Done when:** "Which debt should I pay first?" names the highest-APR card with the right numbers and cites a source.
- **Learn:** tool calling (and its failure modes on small local models), RAG, prompt minimization, LLM-as-judge.

### M3. Autonomous budget agent
1. `budget_engine` pure functions + unit tests (fit, pace, close).
2. Budget tables + `initialize_budget`.
3. Budget graph nodes per section 6, with structured outputs.
4. Alerts UI with accept/dismiss; `advice_log` read back into `decide_action`.
5. Scheduler + `/admin/sandbox/advance` to simulate days.
6. Period-Close graph.
- **Done when:** simulate a month on a synthetic user with a planted overspend; the alert fires within one run of crossing 10%, with the right cause, and next month's budget adapts.
- **Learn:** stateful graphs, checkpointing, scheduling, human-in-the-loop, separating deterministic logic from LLM judgment.

### M4. Goal generation
1. Goal graph: compute monthly surplus deterministically, then LLM proposes 1-3 goals within the surplus and the user's stated preferences (`users.prefs_json`, e.g. "don't touch my gym budget").
2. Validate: goals must be affordable from surplus and dated; reject and retry otherwise.
3. `interrupt()` for acceptance; accepted goals become budget lines (e.g. "Debt payoff: $300/mo").
- **Learn:** constrained generation, validate-and-retry loops.

### M5. Benchmarking
Grow scenarios to ~30-50, run the full metric table, chart results, and write up what changed between versions (prompt changes, thresholds, model swaps).

---

## 15. Risks and open questions

| Risk | Mitigation |
|---|---|
| LLM gives confident but wrong financial advice | Deterministic math, KB citations, coherence judge, "not financial advice" disclaimer |
| PII leaks to the LLM | Privacy Gateway allowlist + prompt audit log |
| Sandbox data too thin to test the loop | Synthetic fixtures with known ground truth |
| Over-alerting annoys users | `days_elapsed` guard, dedupe via `advice_log`, false-alarm metric |
| Local 8B model fumbles tool calls or JSON | Structured output with Pydantic + retry on parse failure; keep tool count small per graph; test a larger model if needed |
| Scope creep | Stretch goal stays last; M3 is the core novelty, protect its time |

All initial open questions were decided on 2026-10-04 (see the decision log). Add new ones here as they come up.

---

## 16. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-10-04 | Python/FastAPI backend, Next.js frontend, SQLite→Postgres | One language for agents + API; React Plaid Link support |
| 2026-10-04 | Budget math is deterministic code; LLM only diagnoses and decides | Testable, benchmarkable, no arithmetic hallucinations |
| 2026-10-04 | Budget periods are calendar months | Matches statements and how people think about budgets |
| 2026-10-04 | Semantic similarity target 0.85 | Armin's call; proposal said 0.90 |
| 2026-10-04 | Local Llama via Ollama for the agent LLM | Privacy: user data never leaves the machine; free eval runs |
| 2026-10-04 | Agent `llama3.1:8b`, judge `qwen2.5:14b` (Ollama, M5 24 GB) | Reliable tool calling at 8B; judge from a different model family; both fit in memory together |
| 2026-10-04 | `mistral` (7B) included as a comparison agent model in benchmarks | Already installed; gives a model-vs-model results table |
| 2026-10-04 | Use Plaid's primary `personal_finance_category` values as budget categories | No mapping layer to maintain; LLM only categorizes what Plaid leaves unclear |
| | | |

---

## References
- Plaid docs: https://plaid.com/docs/ (Link, Transactions `/transactions/sync`, Liabilities, Sandbox)
- LangGraph docs: https://langchain-ai.github.io/langgraph/ (StateGraph, checkpointers, `interrupt`)
- ChromaDB docs: https://docs.trychroma.com/
- Ollama: https://ollama.com/ and `langchain-ollama`
- PlantUML: https://plantuml.com/
