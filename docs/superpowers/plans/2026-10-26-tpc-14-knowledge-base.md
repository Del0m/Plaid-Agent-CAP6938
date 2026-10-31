# TPC-14 Knowledge Base and `search_kb` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch ~16 public finance guidance pages into `kb/sources/`, chunk and embed them locally with `nomic-embed-text` into the Chroma collection `fin_guidance`, and give the chat agent a `search_kb(query)` tool that returns the top 4 chunks with source URLs.

**Architecture:** `app/rag/fetch.py` (httpx + trafilatura → markdown with front matter), `app/rag/chunk.py` (pure heading-aware chunking), `app/rag/store.py` (`KnowledgeBase` over a Chroma persistent client; embeddings are computed by our code, not by Chroma). `search_kb` joins the TPC-13 tools in `app/agents/tools.py`. Thin CLIs live in `backend/scripts/`.

**Tech Stack:** Python 3.9.6 (the venv), `chromadb`, `trafilatura`, `langchain-ollama` (`OllamaEmbeddings`), httpx, pytest.

**Spec:** `docs/superpowers/specs/2026-10-26-tpc-14-knowledge-base-design.md`

## Global Constraints

- Python 3.9: `Optional[...]`, not `X | None`; `list[...]`/`dict[...]` are fine.
- Defaults: `OLLAMA_EMBED_MODEL=nomic-embed-text`, `KB_DIR=<repo>/kb` resolved from `app/` (`Path(__file__).parent.parent.parent / "kb"`). Chroma lives at `KB_DIR/chroma`, sources at `KB_DIR/sources` and `KB_DIR/sources/local`.
- Collection `fin_guidance`, cosine distance, `anonymized_telemetry=False`, no Chroma embedding function (Chroma must never download its default ONNX model).
- Prefixes: `search_document: ` on every embedded chunk, `search_query: ` on every query.
- Chunking: `CHUNK_WORDS = 375`, `OVERLAP_WORDS = 38`; chunks never cross a section. Ids `<slug>-<n>`.
- `search_kb`: top `4`, query trimmed to `1000` chars, output `{"purpose":"kb","results":[{title, source_url, section, text}]}` with `separators=(",", ":")`, `ensure_ascii=False`. No scores.
- Front-matter keys, in this order: `title`, `source_url`, `topic`, `fetched_at`. Fetch failure = non-200 or under `150` words.
- Nothing outside `app/agents/llm.py` constructs a model client (chat or embeddings).
- Every test function gets an entry in `docs/tests/<module>.md` in the same commit. Run tests from `backend/` with `../venv/bin/python -m pytest`.
- Comments are short, lowercase, one-line "why" notes, as in existing backend files.
- No Claude attribution in commits. Commit prefix `feat(TPC-14):` / `test(TPC-14):` / `docs(TPC-14):`.

## Review Focus

1. **Titles with colons and non-ASCII text** (IRS titles like `Retirement topics: IRA contribution limits`, curly quotes, `é`). The front matter round-trips: written and read as UTF-8, split on the first `:` only. Pinned by `test_title_with_colon_and_unicode_round_trips` (Task 2).
2. **A KB with fewer chunks than k.** `search` returns every chunk it has instead of raising. Pinned by `test_search_with_fewer_chunks_than_k` (Task 3).
3. **Gov sites redirect** (missing trailing slash → 301). Fetch follows the redirect and records the URL from `sources.json`, not the redirected one. Pinned by `test_follows_redirect` (Task 2).
4. **Markdown tables in a section** (the Roth comparison chart). A table under `CHUNK_WORDS` stays in one chunk, rows in order. Pinned by `test_table_paragraph_stays_whole` (Task 1).
5. **Ollama lists the model as `nomic-embed-text:latest`.** The live test's skip check accepts the bare name, so the done-when test actually runs here. Pinned by Task 5 step 4 (`-rs` shows it was not skipped).

---

### Task 1: Config and `rag/chunk.py`

**Files:**
- Modify: `backend/app/config.py`, `.env.example`, `.gitignore` (add `kb/chroma/` and `kb/sources/local/`)
- Create: `backend/app/rag/__init__.py`, `backend/app/rag/chunk.py`
- Test: `backend/tests/test_chunk.py`
- Docs: `docs/tests/test_chunk.md`

**Interfaces:**
- Produces:
  - `config.OLLAMA_EMBED_MODEL: str`, `config.KB_DIR: Path`
  - `REQUIRED_KEYS = ("title", "source_url", "topic", "fetched_at")`, `CHUNK_WORDS = 375`, `OVERLAP_WORDS = 38`
  - `@dataclass(frozen=True) class Chunk: id, text, title, section, source_url, topic, fetched_at` (all `str`)
  - `parse_source(path: Path) -> tuple[dict, str]`
  - `chunk_source(path: Path) -> list[Chunk]` (slug = `path.stem`)

- [ ] **Step 1: Config.** Add both settings to `config.py`. `KB_DIR` uses the `or` pattern of `PROMPT_AUDIT_PATH`, so a blank `.env` value falls back to the default. Mirror them in `.env.example`, each with a one-line comment, and add the two ignore lines.

- [ ] **Step 2: Write the failing tests.** A `write_source(tmp_path, slug, body, **front)` helper writes a file with all four keys by default (override or drop a key with `key=None`). Words are generated as `" ".join(f"w{i}" for i in range(n))`, so overlap is checkable.
  - `test_parse_front_matter`: the returned dict has the four values, and the body excludes the fences.
  - `test_missing_or_empty_key_raises` (parametrized over the four keys × {missing, empty}): `pytest.raises(ValueError, match=f"{slug}.md: missing {key}")`.
  - `test_title_with_colon_and_unicode_round_trips`: title `Retirement topics: IRA “limits” – café` parses back exactly.
  - `test_splits_at_headings`: a body `intro\n\n# A\n\ntext a\n\n## B\n\ntext b\n\n# Empty\n` gives sections `[<title>, "A", "B"]`, and no chunk for `Empty`.
  - `test_chunks_respect_limit_and_overlap`: one section with 900 words in 9 paragraphs. Every chunk has ≤ 375 words, and each chunk's first 38 words equal the previous chunk's last 38.
  - `test_long_paragraph_is_split`: one 1,000-word paragraph gives ≥ 3 chunks, each ≤ 375 words.
  - `test_chunks_never_cross_sections`: two 300-word sections give exactly 2 chunks, with no section-A words in B's chunk.
  - `test_table_paragraph_stays_whole`: a 5-row markdown table (one paragraph, `|` lines) under 375 words appears whole, in row order, in one chunk's text.
  - `test_short_doc_one_chunk_stable_ids`: ids are `["irs-roth-iras-0"]`; calling twice gives equal lists; metadata fields are copied from the front matter.

- [ ] **Step 3: Run them.** `../venv/bin/python -m pytest tests/test_chunk.py -v`. Expected: collection error / ImportError for `app.rag.chunk`.

- [ ] **Step 4: Implement `chunk.py`.** Front matter: the file must start with a `---` line; read `key: value` lines until the next `---`, using `line.partition(":")` and stripping both sides. Open with `encoding="utf-8"`. Headings match `^#{1,6}\s+(.+)$`. Packing within a section, with paragraphs = blocks split on blank lines:

```text
current = []                                   # words of the chunk being built
for each paragraph p (as a word list, kept as one unit when len(p) <= CHUNK_WORDS):
    if current and len(current) + len(p) > CHUNK_WORDS:
        emit(current); current = current[-OVERLAP_WORDS:]
    if len(p) > CHUNK_WORDS: slide over p in windows of CHUNK_WORDS with step CHUNK_WORDS - OVERLAP_WORDS,
                             starting from `current`'s overlap; the last window becomes `current`
    else: current += p   (paragraph break kept as "\n\n" in the emitted text)
emit(current) unless it holds only overlap words
```

The emitted text joins a paragraph's words with single spaces and paragraphs with `\n\n`, so table rows keep their line breaks: split a paragraph into lines, then into words, and keep the line structure.

- [ ] **Step 5: Run them.** Same command. Expected: all pass. Then run the full suite: 102 passed plus the new tests.

- [ ] **Step 6: Docs and commit.** Write `docs/tests/test_chunk.md` in the `test_tools.md` format (what it does / why it's there per test). Then `git commit -m "feat(TPC-14): heading-aware chunking of kb sources with front matter"`.

---

### Task 2: `rag/fetch.py`, `sources.json` and `scripts/fetch_kb.py`

**Files:**
- Modify: `backend/requirements.txt` (add `trafilatura`)
- Create: `kb/sources.json`, `backend/app/rag/fetch.py`, `backend/scripts/fetch_kb.py`
- Test: `backend/tests/test_fetch_kb.py`
- Docs: `docs/tests/test_fetch_kb.md`

**Interfaces:**
- Consumes: `config.KB_DIR`; `parse_source` (Task 1), used in tests to check the written files.
- Produces:
  - `class FetchError(Exception)`
  - `fetch_source(entry: dict, client: httpx.Client, today: date) -> str`. Returns the full file text (front matter + body) or raises `FetchError`.
  - `source_path(kb_dir: Path, entry: dict) -> Path`. Gives `kb_dir/sources/<slug>.md`, or `kb_dir/sources/local/<slug>.md` when `entry["license"] == "copyrighted"`.
  - `fetch_all(kb_dir: Path, client: httpx.Client, refresh: bool = False, today: Optional[date] = None) -> list[str]`. Returns the failed slugs.
  - `USER_AGENT`, `MIN_WORDS = 150`

- [ ] **Step 1: Install.** Add `trafilatura` to `requirements.txt` and run `../venv/bin/pip install -r requirements.txt`. Confirm with `../venv/bin/python -c "import trafilatura; print(trafilatura.__version__)"`, which should print 2.x.

- [ ] **Step 2: Write `kb/sources.json`.** Use the 16 entries from the spec's Sources table, in that order, each `{"slug", "url", "topic", "license"}`. Only `unfcu-50-30-20` is `"copyrighted"`.

- [ ] **Step 3: Write the failing tests.** Use an `httpx.Client(transport=httpx.MockTransport(handler))` whose handler maps URL → `httpx.Response`. The page fixture is inline HTML: a `<nav>` with `Home About Contact Menu`, an `<article>` with an `<h1>`, an `<h2>`, ~200 generated words in `<p>`s, and a small `<table>`. Tests:
  - `test_writes_front_matter_and_article`: the parsed file has the four keys, `source_url` = the entry url, `fetched_at` = `"2026-10-26"`, the body contains the article words and not `Contact Menu`.
  - `test_table_survives`: the body contains a markdown table row (a line starting with `|`) holding the table's cell text.
  - `test_title_override_and_fallback`: an entry `title` wins; without one the HTML `<title>` is used.
  - `test_title_with_colon_and_unicode_round_trips`: a page titled `Retirement topics: IRA “limits”` gives exactly that `title` from `parse_source`.
  - `test_follows_redirect`: the url returns 301 to `url + "/"`, which serves the page. The file is written and `source_url` is the original url.
  - `test_failures_reported_others_written`: one 404 and one 40-word page, plus one good entry, in a temp `kb/sources.json`. `fetch_all` returns both bad slugs, and the good file exists.
  - `test_skips_existing_unless_refresh`: a pre-existing file is untouched (same mtime/content) without `refresh` and rewritten with it.
  - `test_copyrighted_goes_to_local`: the file lands at `sources/local/<slug>.md`.
  - `test_sends_user_agent`: the handler asserts `request.headers["user-agent"] == USER_AGENT`.

- [ ] **Step 4: Run them.** `../venv/bin/python -m pytest tests/test_fetch_kb.py -v`. Expected: ImportError.

- [ ] **Step 5: Implement `fetch.py`.** Make the request with `client.get(url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)`, and use a browser UA string. The body comes from `trafilatura.extract(html, output_format="markdown", include_tables=True, include_links=False)`, and the title from `trafilatura.extract_metadata(html).title` after an entry `title`, with the slug as the last fallback. Collapse any newline in the title to a space. Write files as UTF-8 and create parent dirs. `fetch_all` reads `kb_dir/"sources.json"`, logs each failure with `logging`, and continues.

- [ ] **Step 6: Implement `scripts/fetch_kb.py`.** It's a thin `argparse` CLI with a `--refresh` flag. It runs `fetch_all(config.KB_DIR, httpx.Client())`, prints `ok <slug>` / `skip <slug>` / `FAIL <slug>` lines, and calls `sys.exit(1)` if anything failed. Put `sys.path` setup at the top the same way `scripts/sandbox_link.py` does (read it first).

- [ ] **Step 7: Run.** Run the Task 2 tests, then the full suite. Expected: all pass. If trafilatura drops the synthetic article as "too short", make the fixture's paragraphs longer rather than loosening extraction settings.

- [ ] **Step 8: Docs and commit.** Write `docs/tests/test_fetch_kb.md`, then `git commit -m "feat(TPC-14): fetch kb sources to markdown with trafilatura"`. Don't commit any fetched `.md` files yet; that's Task 5.

---

### Task 3: `get_embeddings` and `rag/store.py`

**Files:**
- Modify: `backend/requirements.txt` (add `chromadb`), `backend/app/agents/llm.py`
- Create: `backend/app/rag/store.py`
- Test: `backend/tests/test_kb_store.py`, plus one test in `backend/tests/test_llm.py`
- Docs: `docs/tests/test_kb_store.md`, `docs/tests/test_llm.md`

**Interfaces:**
- Consumes: `Chunk`, `chunk_source` (Task 1); `config.OLLAMA_EMBED_MODEL`, `config.OLLAMA_BASE_URL`, `config.KB_DIR`.
- Produces:
  - `get_embeddings(model: Optional[str] = None) -> Embeddings` in `llm.py`
  - `COLLECTION = "fin_guidance"`, `DOC_PREFIX = "search_document: "`, `QUERY_PREFIX = "search_query: "`
  - `class KnowledgeBase(path: Optional[Path] = None, embeddings: Optional[Embeddings] = None)` with `rebuild(chunks: list[Chunk]) -> int`, `search(query: str, k: int = 4) -> list[dict]` (keys `title, section, source_url, text`) and `count() -> int`

- [ ] **Step 1: Install.** Add `chromadb` to `requirements.txt` and install. Run `../venv/bin/python -c "import chromadb; print(chromadb.__version__)"`, which should print 1.x. Run the full suite again to confirm nothing broke: chromadb pins its own versions of pydantic, httpx and others. If pip reports a conflict with `langchain-core` or `fastapi`, stop and report it rather than forcing versions.

- [ ] **Step 2: Write the failing tests.** In `test_kb_store.py`, add a `HashEmbeddings` fake: lowercase words → `hash(word) % 64` buckets → an L2-normalized 64-float vector. It records every string in `self.seen`. Use `hashlib.md5`, not built-in `hash`, so runs are deterministic. Implement `embed_documents` and `embed_query`. A `kb` fixture gives `KnowledgeBase(tmp_path / "chroma", embeddings=HashEmbeddings())`, and chunks are built directly with `Chunk(...)`. Tests:
  - `test_count_zero_before_rebuild`
  - `test_search_returns_best_match_first`: three chunks about `roth ira`, `credit card apr` and `emergency fund`. Query `credit card apr` gives the APR chunk first, with exactly the keys `{title, section, source_url, text}`.
  - `test_rebuild_twice_no_duplicates`: `count()` equals `len(chunks)` after two rebuilds, and a rebuild with a smaller list shrinks it.
  - `test_prefixes`: every document string seen starts with `"search_document: <title> - <section>\n\n"`, and the query string is `"search_query: credit card apr"`.
  - `test_search_with_fewer_chunks_than_k`: 2 chunks with `k=4` return 2 results without raising.
  - `test_persists_across_instances`: after a rebuild, a new `KnowledgeBase` on the same path reports the same `count()`.

  In `test_llm.py`, add `test_get_embeddings_uses_config`: monkeypatch `config.OLLAMA_EMBED_MODEL`, then check `.model` and `.base_url` on the result.

- [ ] **Step 3: Run them.** Expected: ImportError / AttributeError.

- [ ] **Step 4: Implement.** Implement `get_embeddings` with `OllamaEmbeddings(model=model or config.OLLAMA_EMBED_MODEL, base_url=config.OLLAMA_BASE_URL)`. In `store.py`:
  - The client is `chromadb.PersistentClient(path=str(path or config.KB_DIR / "chroma"), settings=Settings(anonymized_telemetry=False))`.
  - Create the collection with `get_or_create_collection(COLLECTION, embedding_function=None, configuration={"hnsw": {"space": "cosine"}})`. If this chromadb version rejects `configuration`, use `metadata={"hnsw:space": "cosine"}`.
  - `rebuild` calls `delete_collection`, catching chromadb's not-found error, then recreates the collection and adds in batches of 32 with the ids, documents (plain text), embeddings and metadata from the spec.
  - `search` queries with `n_results=min(k, count)` and returns `[]` when count is 0.

- [ ] **Step 5: Run.** Run the Task 3 tests, then the full suite. Expected: all pass, with no network access. Check that `~/.cache/chroma` didn't gain an `onnx_models` directory during the run.

- [ ] **Step 6: Docs and commit.** Write `docs/tests/test_kb_store.md` and add the new entry to `docs/tests/test_llm.md`. Then `git commit -m "feat(TPC-14): chroma knowledge base with nomic-embed-text embeddings"`.

---

### Task 4: `search_kb` tool

**Files:**
- Modify: `backend/app/agents/tools.py`, `backend/tests/test_tools.py`
- Docs: `docs/tests/test_tools.md`

**Interfaces:**
- Consumes: `KnowledgeBase.search(query, k)` and `KnowledgeBase.count()` (Task 3).
- Produces: `make_tools(gateway: PrivacyGateway, kb: KnowledgeBase) -> list[BaseTool]`, which returns four tools; `SEARCH_K = 4`, `MAX_QUERY_CHARS = 1000`.

- [ ] **Step 1: Write the failing tests.** Add a `FakeKB` with `count()`, and a `search(query, k)` that records `(query, k)` and returns one canned result dict. The `tools` fixture passes `FakeKB()`. Changes:
  - Rename `test_three_tools_with_descriptions` to `test_four_tools_with_descriptions`, with the set `{"get_accounts", "get_debts", "get_spending", "search_kb"}`.
  - Extend `test_no_tool_accepts_a_user` so `search_kb`'s args are exactly `{"query"}`.
  - `test_search_kb_output_shape`: `json.loads(out) == {"purpose": "kb", "results": [<canned>]}`, and the fake saw `k == 4`.
  - `test_search_kb_rejects_empty_query` (parametrized `""`, `"   "`): `ToolException` matching `query must not be empty`.
  - `test_search_kb_trims_long_query`: `"x" * 5000` reaches the fake as a 1,000-character string.
  - `test_search_kb_empty_kb`: with `count() == 0`, raises `ToolException` matching `knowledge base is empty`, and `search` was never called.
  - Update the existing `call_with_tools` / live tests that call `make_tools` to pass a `FakeKB`.

- [ ] **Step 2: Run them.** `../venv/bin/python -m pytest tests/test_tools.py -v`. Expected: the new tests fail (TypeError on the `make_tools` arity / missing tool).

- [ ] **Step 3: Implement.** Add `search_kb` as a closure in `make_tools`: strip, reject empty, trim to `MAX_QUERY_CHARS`, check `count()`, `logger.warning` to run `scripts/ingest_kb.py`, then `json.dumps(...)` per the Global Constraints. Use the description copy exactly as the spec gives it. Add a one-line comment saying why it skips the gateway (public text; query stays local).

- [ ] **Step 4: Run.** Run the Task 4 tests, then the full suite. Expected: all pass.

- [ ] **Step 5: Docs and commit.** Update `docs/tests/test_tools.md`: the shared setup gets the `FakeKB` and the renamed test, plus the new entries. Then `git commit -m "feat(TPC-14): search_kb tool over the knowledge base"`.

---

### Task 5: Real corpus, ingest/search scripts and the done-when test

**Files:**
- Create: `kb/sources/*.md` (15 public-domain files, committed), `backend/scripts/ingest_kb.py`, `backend/scripts/search_kb.py`
- Modify: `kb/sources.json` (title overrides only if needed), `backend/tests/test_kb_store.py`
- Docs: `docs/tests/test_kb_store.md`

**Interfaces:**
- Consumes: `fetch_all` (Task 2), `chunk_source` (Task 1), `KnowledgeBase` (Task 3).
- Produces: `ingest(kb_dir: Path, kb: KnowledgeBase) -> tuple[int, int]` in `store.py`, which returns `(chunks, sources)`. It reads `sources/*.md` then `sources/local/*.md`, sorted, and raises `FileNotFoundError("no sources in <dir>; run scripts/fetch_kb.py")` when there are none.

- [ ] **Step 1: Fetch for real.** From `backend/`, run `../venv/bin/python scripts/fetch_kb.py`. Expected: 16 `ok` lines and exit 0. For any `FAIL`, check the page in a browser. If it moved, update the URL and note it in the PR. If it's JavaScript-only, replace it with another page on the same topic from the same agency.

- [ ] **Step 2: Read every file by eye.** Each file should be the right article, with no menus, footers or "Was this page helpful" text, and its tables intact. In `irs-roth-comparison-chart.md` the three columns must be readable. Fix bad titles with a `title` override and `--refresh`. Record anything trafilatura couldn't clean.

- [ ] **Step 3: Add `ingest` and the scripts, and write the live test.**
  - `ingest_kb.py` prints `N chunks from M sources`.
  - `search_kb.py "<query>"` prints rank, title, section, url, and the first 200 characters of each result.
  - Add an `_ollama_has_embed_model()` helper. It accepts `name == model or name == model + ":latest"`, because Ollama lists `nomic-embed-text:latest`.
  - Add `test_live_kb_search`, parametrized with two cases:
    - `("Roth IRA vs 401k", lambda url: url.startswith("https://www.irs.gov/retirement-plans"))`
    - `("which debt should I pay off first", lambda url: "how-reduce-your-debt" in url)`

    It ingests the real `config.KB_DIR` sources into `tmp_path / "chroma"` with real embeddings, and asserts that some top-4 `source_url` passes the check. It's skipped unless the model is listed and `kb/sources/*.md` exist. To keep the run fast, use a module-scoped fixture so ingest happens once for both cases.
  - Add `test_ingest_without_sources_raises` (offline, empty `tmp_path`).

- [ ] **Step 4: Run.** `../venv/bin/python -m pytest -rs`. Expected: all pass, and `test_live_kb_search` is **not** in the skip summary. If a live case fails, look at what `search_kb.py` returns for that query before changing anything. A wrong page ranking first is a corpus or chunking problem, so don't weaken the assertion.

- [ ] **Step 5: Manual verification.** Run `scripts/ingest_kb.py`, then `scripts/search_kb.py` for each of these queries, and save the top-4 sources for the PR description:
  - "Roth IRA vs 401k"
  - "which debt should I pay off first"
  - "how much should I keep in an emergency fund"
  - "what is the 50/30/20 rule"

- [ ] **Step 6: Commit.** Stage `kb/sources.json`, `kb/sources/*.md` (`git status` should show nothing under `local/` or `chroma/`), the scripts, `store.py`, the test and its docs. Then `git commit -m "feat(TPC-14): fetched kb corpus, ingest and search scripts, live retrieval test"`.

---

### Task 6: Architecture, decision log and README

**Files:**
- Modify: `docs/architecture.md` (sections 3, 7, 8, 16), `README.md`

- [ ] **Step 1: Architecture.**
  - Section 3: the embeddings row becomes nomic-embed-text via Ollama, and MiniLM moves to the alternatives column.
  - Section 7: add one paragraph on why `search_kb` bypasses the gateway.
  - Section 8: describe `sources.json`, `fetch_kb.py`, the license split, the prefixes and telemetry off. The ~500/50 chunk size stays.
  - Section 16: add the four 2026-10-26 rows listed in the spec's Docs section, one sentence of "why" each.

- [ ] **Step 2: README.** Add a "Knowledge base" section after "Run locally", with the spec's Setup commands.

- [ ] **Step 3: Check and commit.** Run `grep -n "MiniLM" docs/architecture.md`; it should only match the alternatives column and the decision-log row. Then `git commit -m "docs(TPC-14): knowledge base in architecture, decision log and README"`.
