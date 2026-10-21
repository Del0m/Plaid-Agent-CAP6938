# TPC-12: LLM wrapper on Ollama and Privacy Gateway v1

Jira: TPC-12 (epic TPC-3, M2). Branch: `TPC-12`, cut from `TPC-11` (rebase onto `main` once TPC-10/11 merge).

## Goal

Lay the two foundations the M2 chat agent (TPC-13 to TPC-16) stands on:

1. **`agents/llm.py`**: the only place the app creates an LLM client. It wraps `ChatOllama` (`llama3.1:8b`, temperature configurable) and proves that tool calling round-trips on the local model.
2. **`privacy/`**: the only code path that turns user data into LLM prompt text (architecture section 7). It uses field allowlists per purpose, a per-session pseudonym, a regex scrub of free-text strings, and a local audit log of every prompt it builds.

**Done when:**
- a toy tool call round-trips on the real local model: the model calls the tool, gets the result, and answers with it;
- a unit test shows no name, email or account number in any gateway-built prompt.

**Out of scope:** LangGraph, the M2 data tools (`get_accounts` and friends, TPC-13), the knowledge base, the chat endpoint, and any frontend.

## Decisions

| Question | Decision |
|---|---|
| How the round trip is proven | One live pytest against real Ollama, skipped when Ollama isn't reachable or the model isn't pulled (same pattern as the Plaid Sandbox tests), plus offline tests with a scripted fake chat model |
| What the gateway builds context for | Three purposes matching the M2 tools: `accounts`, `debts`, `spending`. Each has its own field allowlist |
| Where the audit log goes | Append-only JSONL file, `backend/logs/prompt_audit.jsonl`, git-ignored |
| Account names | Sent after scrubbing; masks, Plaid ids, account numbers and institution names are never sent |
| Pseudonym lifetime | One per `PrivacyGateway` instance (one chat session), held in memory only |
| Money in prompts | Dollars as numbers rounded to 2 decimals (cents ÷ 100), so the model isn't reading `41000` as dollars |

## Setup (one-time, on the Mac)

```bash
ollama serve                 # or open the Ollama app
ollama pull llama3.1:8b
venv/bin/pip install -r backend/requirements.txt   # after langchain-ollama is added
```

## Configuration (`app/config.py`, `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `OLLAMA_MODEL` | `llama3.1:8b` | Agent model (architecture decision log) |
| `LLM_TEMPERATURE` | `0` | Default sampling temperature; 0 keeps evals repeatable (architecture 10.3) |
| `PROMPT_AUDIT_PATH` | `backend/logs/prompt_audit.jsonl` (resolved from `app/`) | Where the gateway appends every prompt it builds |

`requirements.txt` gains `langchain-ollama` (which brings `langchain-core`). The root `.gitignore` gains `logs/`.

## `app/agents/llm.py`

```python
def get_chat_model(temperature: float | None = None, model: str | None = None) -> BaseChatModel
```
Returns `ChatOllama(model=model or OLLAMA_MODEL, base_url=OLLAMA_BASE_URL, temperature=LLM_TEMPERATURE if temperature is None else temperature)`. Nothing else in the app constructs a model; swapping in `mistral` or a hosted model for benchmarks means changing only this function.

```python
def call_with_tools(messages: list[BaseMessage], tools: list[BaseTool], *,
                    model: BaseChatModel | None = None, max_steps: int = 3) -> AIMessage
```
This is the smallest possible tool loop, and it exists to prove the round trip; TPC-15 replaces it with a LangGraph ReAct graph. It works like this:
- Bind the tools and invoke the model.
- For each tool call in the reply, run the matching tool and append a `ToolMessage` with its result and the call's id.
- Invoke the model again, until a reply has no tool calls.
- Return that final `AIMessage`.
- A tool name the model invents gets a `ToolMessage` saying the tool doesn't exist, rather than raising.
- Running past `max_steps` raises `RuntimeError("tool loop did not finish in N steps")`.

### Toy tool

The toy tool lives with the tests, not in the app:

```python
@tool
def get_lucky_number(name: str) -> int:
    """Return the lucky number for a person's name."""
    return len(name) * 7 + 3
```

It's chosen because the model can't know the answer without calling the tool. For example, "Ada" gives 24.

## `app/privacy/scrub.py`

Pure functions. `None` passes through unchanged.

- `scrub_text(s)` is the base pass:
  - emails become `[email]`;
  - phone numbers (`(407) 555-1234`, `407-555-1234`, `+1 407 555 1234`) become `[phone]`;
  - any run of 4 or more digits becomes `[number]`, which catches card and account numbers, masks, and long reference ids.
- `scrub_merchant(s)` runs `scrub_text`, then replaces person-to-person transfer counterparties: `Zelle|Venmo|Cash App|PayPal` followed by `to|from` and up to two words becomes, for example, `Zelle to [person]`. Business possessives like "McDonald's" are left alone.
- `scrub_account_name(s)` runs `scrub_text`, then replaces a capitalized possessive name, so `Jane's Checking` becomes `[person]'s Checking`. Account names are the user's own labels, so a possessive there is a person, not a brand.

## `app/privacy/gateway.py`

```python
Purpose = Literal["accounts", "debts", "spending"]

class PrivacyGateway:
    def __init__(self, session: Session, user_id: str, audit_path: Path | None = None): ...
    pseudonym: str                      # "user_" + 4 random hex chars, e.g. "user_7f3a"
    def build_context(self, purpose: Purpose, *, start: date | None = None,
                      end: date | None = None, category: str | None = None) -> str: ...
```

`build_context` reads this user's rows only and builds each output row by picking allowlisted fields explicitly. It never serializes a whole model object, so a column added later can't leak by accident. It returns compact JSON, `{"user": "<pseudonym>", "purpose": "...", "data": ...}`, and appends one audit line before returning. An unknown purpose raises `ValueError`.

| Purpose | Rows | Allowlisted fields |
|---|---|---|
| `accounts` | every account | `name` (scrubbed), `type`, `subtype`, `balance`, `available`, `limit` |
| `debts` | accounts with a liability row, APR descending, unknown APR last (same order as `/dashboard`) | `name` (scrubbed), `kind`, `apr`, `balance`, `min_payment`, `next_due_date` |
| `spending` | non-removed transactions with `start <= date <= end` (default: the last 30 days) | without `category`: `{category: total}` over positive amounts only (spending, per the sign convention). With `category`: that total plus up to 50 of that category's transactions as `date`, `amount`, `merchant` (scrubbed), newest first |

Never sent, by construction: user id, email, `password_hash`, account `mask`, `plaid_account_id`, `plaid_item_id`, access tokens, `institution_name`, internal row ids, and `pf_category_detailed`. The transactions table doesn't store Plaid's raw `name` string at all, so it can't leak.

**Audit line** (one JSON object per line, appended):

```json
{"ts": "2026-10-21T19:04:11Z", "pseudonym": "user_7f3a", "purpose": "spending",
 "params": {"start": "2026-09-21", "end": "2026-10-21", "category": "FOOD_AND_DRINK"},
 "prompt": "<the exact string returned>"}
```

The audit line never contains `user_id`. The parent directory is created if it's missing.

## Tests

Every module gets `docs/tests/<module>.md` (architecture section 12).

**`tests/test_llm.py`**:
- `get_chat_model` uses the configured model, base URL and default temperature, and a passed `temperature` overrides the default.
- `call_with_tools` with a scripted fake model:
  - one tool call runs the tool and sends its result back as a `ToolMessage` with the matching `tool_call_id`, and the final answer is returned;
  - a reply with no tool call returns immediately;
  - an unknown tool name produces an error `ToolMessage`, not an exception;
  - looping past `max_steps` raises.
- `test_live_tool_call_round_trip`:
  - Asks the real model "What is Ada's lucky number? Use the tool."
  - Asserts `get_lucky_number` was called with `name == "Ada"` and that the final text contains `24`.
  - Skipped when `GET {OLLAMA_BASE_URL}/api/tags` fails or doesn't list the model.

**`tests/test_scrub.py`**:
- emails, the three phone formats, digit runs (4 or more scrubbed, 3 or fewer kept);
- Zelle, Venmo and PayPal counterparties;
- a possessive in account names;
- "McDonald's" kept by `scrub_merchant`;
- `None` passes through.

**`tests/test_gateway.py`**:
- **Done-when test.** Seed a user with:
  - email `jane.doe@example.com`;
  - an account named `Jane Doe's Checking ••4321` with mask `4321` and `plaid_account_id` `acc-secret-1`;
  - a credit card with a liability;
  - transactions with merchants `Zelle to Jane Doe` and `Starbucks`.

  Build all three purposes. No prompt may contain `Jane`, `Doe`, the email, `4321`, `acc-secret-1`, the plaid item id, or the user id.
- Each purpose's rows have exactly their allowlisted keys.
- Pseudonym:
  - matches `user_[0-9a-f]{4}`;
  - stays the same across calls on one gateway;
  - differs between two gateways (seeded so the test is deterministic).
- Another user's rows never appear.
- Spending:
  - only positive amounts are summed;
  - removed transactions are excluded;
  - the date range is respected;
  - with `category`, only that category's transactions are listed, capped at 50.
- Each `build_context` call writes one audit line with the pseudonym, the purpose and the exact prompt, and no user id.
- An unknown purpose raises `ValueError`.

## Verification

1. `cd backend && ../venv/bin/python -m pytest` passes. The live LLM test runs when Ollama is up with `llama3.1:8b` pulled, and is skipped otherwise.
2. With Ollama running, the live test passes. A small model can be flaky, so run it 3 times and record the result; if it fails, note which step failed (no tool call, wrong arguments, or a final answer without 24).
3. Open `backend/logs/prompt_audit.jsonl` after the gateway tests and confirm every line is readable and free of PII.

## Risks

- **`llama3.1:8b` may skip the tool or call it with wrong arguments.** Mitigations: temperature 0, a tool docstring that states its purpose plainly, and a user message that says "Use the tool." If it's still unreliable, record that in the decision log; it's a real result for the report.
- **A regex scrub can miss PII it wasn't written for.** The allowlist, not the scrub, is the main defense: only the free-text fields (account names and merchants) go through the scrub. The done-when test pins the known cases.

## Follow-ups (not this ticket)

- TPC-13: wrap `build_context` purposes as the agent's tools.
- TPC-15: replace `call_with_tools` with a LangGraph ReAct graph.
- Decision log rows for the scrub rules and the pseudonym lifetime, once they've held up under TPC-13.
