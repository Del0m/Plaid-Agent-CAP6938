# TPC-12 LLM Wrapper and Privacy Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One place that creates the local LLM client (proven to round-trip a tool call) and one place that turns user data into minimized, scrubbed, pseudonymized, audited prompt text.

**Architecture:** `app/agents/llm.py` wraps `ChatOllama` and adds a minimal tool loop. `app/privacy/scrub.py` holds pure regex scrubs. `app/privacy/gateway.py` reads one user's rows, picks allowlisted fields per purpose, and appends each prompt to a JSONL audit log.

**Tech Stack:** Python 3.12, `langchain-ollama` / `langchain-core`, SQLAlchemy 2, pytest.

**Spec:** `docs/superpowers/specs/2026-10-21-tpc-12-llm-wrapper-privacy-gateway-design.md`

## Global Constraints

- Defaults: `OLLAMA_BASE_URL=http://localhost:11434`, `OLLAMA_MODEL=llama3.1:8b`, `LLM_TEMPERATURE=0`, `PROMPT_AUDIT_PATH=backend/logs/prompt_audit.jsonl`.
- Nothing outside `app/agents/llm.py` constructs a chat model.
- Nothing outside `app/privacy/` turns user rows into prompt text, and gateway rows are built by picking allowlisted fields, never by serializing a model object.
- Never in a prompt or audit line: user id, email, password hash, mask, `plaid_account_id`, `plaid_item_id`, tokens, `institution_name`, internal row ids, `pf_category_detailed`.
- Money in prompts is dollars rounded to 2 decimals (`round(cents / 100, 2)`). Spending totals sum positive amounts only.
- Every test function gets an entry in `docs/tests/<module>.md` in the same commit. Run tests from `backend/` with `../venv/bin/python -m pytest`.
- Comments are short, lowercase, one-line "why" notes, as in existing backend files.
- No Claude attribution in commits.

## Review Focus

1. **The model call itself fails** (Ollama stops mid-loop, connection refused). `call_with_tools` lets the error propagate rather than returning a fake answer. Pinned by `test_model_error_propagates` (Task 1).
2. **A tool raises.** The model gets an error `ToolMessage` containing the exception text and can recover; the loop doesn't crash. Pinned by `test_tool_exception_becomes_error_message` (Task 1).
3. **Merchant strings that are odd but harmless.** `None`, `""`, `7-Eleven` (one digit is kept), and `Store #12345` (becomes `Store #[number]`). Pinned by `test_harmless_strings_survive` (Task 2).
4. **A user with nothing linked, or an empty date range.** Every purpose returns empty `data` (`[]` or `{}`) and still writes an audit line. Pinned by `test_empty_user_builds_empty_context` (Task 3).
5. **The audit directory doesn't exist yet** (fresh clone). It's created, and the line is written. Pinned by `test_audit_creates_missing_directory` (Task 3).

---

### Task 1: Config, dependency and `agents/llm.py`

**Files:**
- Modify: `backend/requirements.txt` (add `langchain-ollama`), `backend/app/config.py`, `.env.example`, `.gitignore` (add `logs/`)
- Create: `backend/app/agents/__init__.py`, `backend/app/agents/llm.py`
- Test: `backend/tests/test_llm.py`
- Docs: `docs/tests/test_llm.md`

**Interfaces:**
- Produces:
  - `config.OLLAMA_BASE_URL: str`
  - `config.OLLAMA_MODEL: str`
  - `config.LLM_TEMPERATURE: float`
  - `config.PROMPT_AUDIT_PATH: Path` (Task 3 uses this one)
  - `get_chat_model(temperature: float | None = None, model: str | None = None) -> BaseChatModel`
  - `call_with_tools(messages: list[BaseMessage], tools: list[BaseTool], *, model: BaseChatModel | None = None, max_steps: int = 3) -> AIMessage`

- [ ] **Step 1: Install and configure**

Add `langchain-ollama` to `requirements.txt` and run `../venv/bin/pip install -r requirements.txt`. Then add the four settings to `config.py` with the Global Constraints defaults:
- `LLM_TEMPERATURE` is parsed with `float(...)`.
- `PROMPT_AUDIT_PATH` is a `Path`; the default is `Path(__file__).parent.parent / "logs" / "prompt_audit.jsonl"`.

Mirror the settings in `.env.example`, each with a one-line comment, and add `logs/` to the root `.gitignore`.

- [ ] **Step 2: Write the failing offline tests**

At the top of `test_llm.py`, add a scripted fake and the toy tool. `call_with_tools` only uses `bind_tools` and `invoke`, so a duck-typed fake is enough:

```python
# replays canned AIMessages in order and records what it was sent; stands in for ChatOllama
class ScriptedModel:
    def __init__(self, *replies): self.replies, self.seen = list(replies), []
    def bind_tools(self, tools): return self
    def invoke(self, messages):
        self.seen.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception): raise reply
        return reply

@tool
def get_lucky_number(name: str) -> int:
    """Return the lucky number for a person's name."""
    return len(name) * 7 + 3

def call(name, args, id="call-1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])
```

| Test | Setup | Assert |
|---|---|---|
| `test_get_chat_model_uses_config` | monkeypatch `config.OLLAMA_MODEL="m1"`, `OLLAMA_BASE_URL="http://h:1"`, `LLM_TEMPERATURE=0.0` | `m.model == "m1"`, `m.base_url == "http://h:1"`, `m.temperature == 0.0` |
| `test_get_chat_model_overrides` | `get_chat_model(temperature=0.7, model="mistral")` | `m.temperature == 0.7`, `m.model == "mistral"` |
| `test_tool_call_round_trips` | `ScriptedModel(call("get_lucky_number", {"name": "Ada"}), AIMessage("Ada's lucky number is 24."))` | final `.content` contains `"24"`; second `seen` ends with a `ToolMessage` whose `content == "24"` and `tool_call_id == "call-1"` |
| `test_no_tool_call_returns_first_reply` | one plain `AIMessage("hi")` | returns it; `len(seen) == 1` |
| `test_unknown_tool_gets_error_message` | `call("nope", {})` then `AIMessage("ok")` | the `ToolMessage` content contains `"unknown tool"`; no exception |
| `test_tool_exception_becomes_error_message` | a `@tool` that raises `ValueError("boom")`, called once, then `AIMessage("ok")` | the `ToolMessage` content contains `"boom"`; returns `"ok"` |
| `test_model_error_propagates` | `ScriptedModel(ConnectionError("down"))` | `pytest.raises(ConnectionError)` |
| `test_loop_stops_after_max_steps` | four tool-call replies, `max_steps=3` | `pytest.raises(RuntimeError, match="did not finish in 3 steps")` |

- [ ] **Step 3: Run to verify they fail**

Run: `../venv/bin/python -m pytest tests/test_llm.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError: app.agents.llm`.

- [ ] **Step 4: Implement `backend/app/agents/llm.py`**

- `get_chat_model` reads `config` attributes at call time, not at import, so the monkeypatched test sees them.
- `call_with_tools`:
  - Bind the tools once and copy `messages` into a working list.
  - Each step invokes the model and appends the reply. A reply with no `tool_calls` is returned.
  - Otherwise, for each call, look the tool up by name and `invoke(call["args"])`. Convert the result with `str()` and append `ToolMessage(content=..., tool_call_id=call["id"])`.
  - A missing tool gets `f"error: unknown tool {name}"`. A tool exception gets `f"error: {exc}"`.
  - After `max_steps` model calls that all requested tools, raise `RuntimeError(f"tool loop did not finish in {max_steps} steps")`.

- [ ] **Step 5: Run to verify they pass**

Run: `../venv/bin/python -m pytest tests/test_llm.py -v`
Expected: 8 passed.

- [ ] **Step 6: Live round-trip test**

```python
# skipped unless ollama answers /api/tags and lists the configured model
def _ollama_has_model() -> bool: ...

@pytest.mark.skipif(not _ollama_has_model(), reason="needs ollama running with OLLAMA_MODEL pulled")
def test_live_tool_call_round_trip():
    calls = []

    @tool
    def get_lucky_number(name: str) -> int:
        """Return the lucky number for a person's name."""
        calls.append(name)
        return len(name) * 7 + 3

    answer = call_with_tools([HumanMessage("What is Ada's lucky number? Use the tool.")], [get_lucky_number])
    assert calls == ["Ada"]
    assert "24" in answer.content
```

`_ollama_has_model` does `httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=2)` and checks `config.OLLAMA_MODEL in [m["name"] for m in resp.json()["models"]]`. Any exception means `False`.

Run with Ollama up 3 times: `../venv/bin/python -m pytest tests/test_llm.py::test_live_tool_call_round_trip -v`
Expected: PASS each time, or a recorded failure mode (no call, wrong arguments, or a final answer without 24). Without Ollama: SKIPPED.

- [ ] **Step 7: `docs/tests/test_llm.md`**

Use the `test_plaid.md` structure: run command, helpers (`ScriptedModel`, `get_lucky_number`, `call`), a What/Why entry for all 9 tests, and a "Not covered" section noting that the live test proves one model and one prompt, not tool-calling reliability in general.

- [ ] **Step 8: Commit**

```bash
git add backend/requirements.txt backend/app/config.py .env.example .gitignore backend/app/agents backend/tests/test_llm.py docs/tests/test_llm.md
git commit -m "feat(TPC-12): Ollama chat model wrapper with a minimal tool loop"
```

---

### Task 2: `privacy/scrub.py`

**Files:**
- Create: `backend/app/privacy/__init__.py`, `backend/app/privacy/scrub.py`
- Test: `backend/tests/test_scrub.py`
- Docs: `docs/tests/test_scrub.md`

**Interfaces:**
- Produces (each takes `Optional[str]` and returns `Optional[str]`, with `None` in giving `None` out):
  - `scrub_text(s)`
  - `scrub_merchant(s)`
  - `scrub_account_name(s)`

- [ ] **Step 1: Write the failing tests**

| Test | Input → expected |
|---|---|
| `test_emails` | `"pay jane.doe@example.com now"` → `"pay [email] now"` |
| `test_phones` (parametrized) | `"(407) 555-1234"`, `"407-555-1234"`, `"+1 407 555 1234"` each → `"[phone]"` |
| `test_digit_runs` | `"card 4111111111111111"` → `"card [number]"`; `"acct 4321"` → `"acct [number]"`; `"Route 66"` unchanged |
| `test_p2p_counterparties` (parametrized) | `"Zelle to Jane Doe"` → `"Zelle to [person]"`; `"Venmo from Bob"` → `"Venmo from [person]"`; `"PayPal to Jane Doe Smith"` → `"PayPal to [person] Smith"` (two words max) |
| `test_account_possessive` | `scrub_account_name("Jane's Checking")` → `"[person]'s Checking"` |
| `test_merchant_keeps_brand_possessive` | `scrub_merchant("McDonald's")` → `"McDonald's"` |
| `test_harmless_strings_survive` | `None` → `None`; `""` → `""`; `"7-Eleven"` unchanged; `scrub_merchant("Store #12345")` → `"Store #[number]"` |
| `test_done_when_account_name` | `scrub_account_name("Jane Doe's Checking ••4321")` contains neither `"Jane"` nor `"Doe"` nor `"4321"` |

The last row needs the possessive rule to cover a two-word name before `'s`: `[A-Z][a-z]+(?: [A-Z][a-z]+)?'s` becomes `[person]'s`.

- [ ] **Step 2: Run to verify they fail**

Run: `../venv/bin/python -m pytest tests/test_scrub.py -v`
Expected: ERROR, `ModuleNotFoundError: app.privacy.scrub`.

- [ ] **Step 3: Implement `backend/app/privacy/scrub.py`**

Module-level compiled patterns, applied in this order: email, then phone, then 4+ digit run (so phone digits aren't half-replaced). The P2P pattern is case-insensitive on the service name.

- [ ] **Step 4: Run to verify they pass**

Run: `../venv/bin/python -m pytest tests/test_scrub.py -v`
Expected: all pass.

- [ ] **Step 5: `docs/tests/test_scrub.md`, then commit**

```bash
git add backend/app/privacy backend/tests/test_scrub.py docs/tests/test_scrub.md
git commit -m "feat(TPC-12): regex scrubs for emails, phones, numbers and person names"
```

---

### Task 3: `privacy/gateway.py`

**Files:**
- Create: `backend/app/privacy/gateway.py`
- Test: `backend/tests/test_gateway.py`
- Docs: `docs/tests/test_gateway.md`

**Interfaces:**
- Consumes: `scrub_account_name` and `scrub_merchant` (Task 2), `config.PROMPT_AUDIT_PATH` (Task 1), and the models in `app/db/models.py`.
- Produces:
  - `Purpose = Literal["accounts", "debts", "spending"]`
  - `PrivacyGateway(session: Session, user_id: str, audit_path: Path | None = None)` with attribute `pseudonym: str`
  - `PrivacyGateway.build_context(purpose, *, start: date | None = None, end: date | None = None, category: str | None = None) -> str`
  - Module constants `ACCOUNT_FIELDS`, `DEBT_FIELDS`, `TXN_FIELDS` (tuples of output keys)
  - Module function `_today() -> date`, which tests monkeypatch

- [ ] **Step 1: Write the failing tests**

Helpers at the top of the test file:
- `seed_pii_user(db) -> str` creates the spec's Jane Doe user and returns the user id. It has:
  - email `jane.doe@example.com`;
  - an item with `plaid_item_id="item-secret-1"`;
  - a checking account named `"Jane Doe's Checking ••4321"` with mask `"4321"` and `plaid_account_id="acc-secret-1"`;
  - a credit card with a liability (APR 22.0);
  - transactions: `Zelle to Jane Doe` (+5000, `TRANSFER_OUT`) and `Starbucks` (+650, `FOOD_AND_DRINK`), both dated 2026-10-10.
- `gateway(db, user_id, tmp_path)` opens a session from `db()` and returns `PrivacyGateway(s, user_id, audit_path=tmp_path / "audit.jsonl")`.
- Monkeypatch `app.privacy.gateway._today` to return `date(2026, 10, 21)` in every test.

| Test | Assert |
|---|---|
| `test_no_pii_in_any_prompt` (done-when) | for each purpose (plus `spending` with `category="TRANSFER_OUT"`), the prompt contains none of `Jane`, `Doe`, `jane.doe@example.com`, `4321`, `acc-secret-1`, `item-secret-1`, or the user id |
| `test_rows_have_only_allowlisted_keys` | `json.loads(prompt)["data"]` rows' keys equal `set(ACCOUNT_FIELDS)` / `set(DEBT_FIELDS)` / `set(TXN_FIELDS)` |
| `test_money_in_dollars` | the checking account at 12345 cents shows `"balance": 123.45` |
| `test_pseudonym_format_and_lifetime` | monkeypatch `secrets.token_hex` in the gateway module to return `"7f3a"` then `"b2c1"`. First gateway: `pseudonym == "user_7f3a"`, the same in two prompts. Second gateway: `"user_b2c1"` |
| `test_other_users_data_hidden` | a second user's account named `"Theirs"` never appears |
| `test_debts_sorted_like_dashboard` | APRs 15.0, 24.99, None become the order 24.99, 15.0, None |
| `test_spending_totals_positive_only_in_range` | +650 and -200 (refund) in FOOD_AND_DRINK, +100 dated before `start`, and a removed +999: `data == {"FOOD_AND_DRINK": 6.5}` |
| `test_spending_category_lists_capped_newest_first` | 60 FOOD_AND_DRINK txns give 50 rows, the first being the newest date; no TRANSFER_OUT rows |
| `test_spending_defaults_to_last_30_days` | a txn on 2026-09-21 is included, one on 2026-09-20 is not |
| `test_empty_user_builds_empty_context` | a user with no rows gets `data == []` for accounts and debts and `{}` for spending; 3 audit lines |
| `test_audit_line_per_call` | 2 calls give 2 lines; each has `pseudonym`, `purpose`, `params`, and `prompt` equal to the returned string; no line contains the user id |
| `test_audit_creates_missing_directory` | `audit_path=tmp_path / "new" / "dir" / "a.jsonl"`: the file exists after one call |
| `test_unknown_purpose_raises` | `pytest.raises(ValueError)` for `"transactions"` |

Spending `data` shape, matching the spec:
- without `category`: `{CATEGORY: dollars}`
- with `category`: `{"category": C, "total": dollars, "transactions": [rows]}`
- `{}` when there are no positive in-range transactions (and, with `category`, none in that category)

- [ ] **Step 2: Run to verify they fail**

Run: `../venv/bin/python -m pytest tests/test_gateway.py -v`
Expected: ERROR, `ModuleNotFoundError: app.privacy.gateway`.

- [ ] **Step 3: Implement `backend/app/privacy/gateway.py`**

- **Constants:**
  - `ACCOUNT_FIELDS = ("name", "type", "subtype", "balance", "available", "limit")`
  - `DEBT_FIELDS = ("name", "kind", "apr", "balance", "min_payment", "next_due_date")`
  - `TXN_FIELDS = ("date", "amount", "merchant")`
  - `TXN_LIMIT = 50`
  - `DEFAULT_SPENDING_DAYS = 30`
- **Pseudonym:** `"user_" + secrets.token_hex(2)`, set in `__init__`.
- **Rows:** each purpose has a private builder that returns `data` as dicts built field by field.
- **Debts order:** the same key as `/dashboard`, `(apr is None, -(apr or 0), -(balance or 0))`, with a comment pointing to `api/dashboard.py`.
- **Prompt:** `json.dumps({"user": pseudonym, "purpose": purpose, "data": data}, separators=(",", ":"), default=str)`; dates become ISO strings.
- **Audit:** `path.parent.mkdir(parents=True, exist_ok=True)`, then append one `json.dumps` line with `ts` (UTC ISO with a `Z` suffix), `pseudonym`, `purpose`, `params` (only the given keys, dates ISO), and `prompt`.

- [ ] **Step 4: Run to verify they pass**

Run: `../venv/bin/python -m pytest -v`
Expected: all pass, with the live LLM test skipped if Ollama is down.

- [ ] **Step 5: `docs/tests/test_gateway.md`, then commit**

```bash
git add backend/app/privacy/gateway.py backend/tests/test_gateway.py docs/tests/test_gateway.md
git commit -m "feat(TPC-12): privacy gateway with per-purpose allowlists, pseudonym and audit log"
```
