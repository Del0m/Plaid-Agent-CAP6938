# TPC-14: Knowledge base ingestion into ChromaDB and `search_kb` tool

Jira: TPC-14 (epic TPC-3, M2). Branch: `TPC-14`, cut from `TPC-13` (rebase onto `main` once PRs #5-#7 merge).

## Goal

Give the chat agent a source of general financial guidance it can cite (architecture section 8):

1. **Sources**: about 16 public pages (CFPB, IRS, investor.gov, FDIC, plus one 50/30/20 guide), fetched by a script into `kb/sources/` as markdown with front matter.
2. **Ingestion**: `scripts/ingest_kb.py` chunks them, embeds them locally and rebuilds the Chroma collection `fin_guidance`.
3. **Retrieval**: a `search_kb(query)` tool that returns the top 4 chunks with their source URLs.

This is the "cites a source" half of the M2 done-when: *"Which debt should I pay first?" names the highest-APR card with the right numbers and cites a source.*

**Done when:** `search_kb("Roth IRA vs 401k")` returns relevant chunks with irs.gov URLs, from the real corpus and the real embedding model.

**Out of scope:** the chat graph and its system prompt (TPC-15), evals (TPC-16), a relevance cutoff, PDF sources, and any frontend.

## Decisions

| Question | Decision |
|---|---|
| Embedding model | **`nomic-embed-text` via Ollama** instead of all-MiniLM-L6-v2. Its 8192-token context fits 500-token chunks (MiniLM truncates at 256 word-pieces, so half of each chunk would never be embedded), and it runs on the Ollama server the agent already needs. Already pulled locally (Ollama 0.33.2) |
| Who embeds | Our code, through LangChain's `OllamaEmbeddings`; Chroma only stores and searches vectors. Chroma is never allowed to fall back to its own default ONNX model |
| nomic task prefixes | Chunks are embedded as `search_document: <text>`, queries as `search_query: <text>`, as the model card requires. Kept next to the embedding call in `app/rag/store.py` |
| How sources get in | `kb/sources.json` lists `{slug, url, topic, license}`; `scripts/fetch_kb.py` downloads each page and extracts the article text to markdown with trafilatura |
| What gets committed | Public-domain pages (US federal sites) are committed as `kb/sources/<slug>.md`, so ingest and tests run offline and the cited text is pinned. Copyrighted pages (`"license": "copyrighted"`, only the UNFCU 50/30/20 guide) go to `kb/sources/local/`, which is git-ignored and refetched on each machine |
| Chunking | Split at markdown headings, then pack paragraphs into chunks of **~500 tokens with ~50 overlap**, as the ticket says. Tokens are estimated from words (375 words ≈ 500 tokens); no tokenizer dependency, since nomic has no hard limit near 500 |
| Re-ingest | Drop and recreate the collection every run. With ~16 docs this takes seconds and can't leave stale or duplicate chunks |
| Results | Top 4, always; no similarity scores in the tool output |
| Privacy | `search_kb` does not go through the privacy gateway: results are public guidance, and the query goes only to local Ollama. Chroma telemetry is turned off |

## Setup (one-time, on the Mac)

```bash
ollama pull nomic-embed-text                        # already done on the M5
venv/bin/pip install -r backend/requirements.txt    # after chromadb and trafilatura are added
cd backend
../venv/bin/python scripts/fetch_kb.py              # only needed for kb/sources/local/ on a fresh clone
../venv/bin/python scripts/ingest_kb.py
../venv/bin/python scripts/search_kb.py "Roth IRA vs 401k"
```

## Configuration (`app/config.py`, `.env.example`)

| Setting | Default | Meaning |
|---|---|---|
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model, read only by `agents/llm.py` |
| `KB_DIR` | `<repo>/kb` (resolved from `app/`) | Holds `sources.json`, `sources/`, `sources/local/` and the Chroma store `chroma/` |

`requirements.txt` gains `chromadb` and `trafilatura`. The root `.gitignore` gains `kb/chroma/` and `kb/sources/local/`.

## Sources (`kb/sources.json`)

All URLs below returned 200 on 2026-10-26 with a browser User-Agent.

| slug | topic | url |
|---|---|---|
| `cfpb-credit-card-apr` | credit | https://www.consumerfinance.gov/ask-cfpb/what-is-a-credit-card-interest-rate-what-does-apr-mean-en-44/ |
| `cfpb-credit-cards` | credit | https://www.consumerfinance.gov/consumer-tools/credit-cards/ |
| `cfpb-credit-reports-scores` | credit | https://www.consumerfinance.gov/consumer-tools/credit-reports-and-scores/ |
| `cfpb-reduce-debt` | debt | https://www.consumerfinance.gov/about-us/blog/how-reduce-your-debt/ (snowball vs highest-interest-first) |
| `cfpb-student-loans` | debt | https://www.consumerfinance.gov/consumer-tools/student-loans/ |
| `cfpb-emergency-fund` | saving | https://www.consumerfinance.gov/an-essential-guide-to-building-an-emergency-fund/ |
| `fdic-deposit-insurance` | saving | https://www.fdic.gov/resources/deposit-insurance/understanding-deposit-insurance |
| `irs-roth-comparison-chart` | retirement | https://www.irs.gov/retirement-plans/roth-comparison-chart |
| `irs-roth-iras` | retirement | https://www.irs.gov/retirement-plans/roth-iras |
| `irs-traditional-and-roth-iras` | retirement | https://www.irs.gov/retirement-plans/traditional-and-roth-iras |
| `irs-ira-contribution-limits` | retirement | https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-ira-contribution-limits |
| `irs-401k-contribution-limits` | retirement | https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-401k-and-profit-sharing-plan-contribution-limits |
| `investor-risk-tolerance` | investing | https://www.investor.gov/introduction-investing/getting-started/assessing-your-risk-tolerance |
| `investor-asset-allocation` | investing | https://www.investor.gov/introduction-investing/getting-started/asset-allocation |
| `investor-compound-interest` | investing | https://www.investor.gov/introduction-investing/investing-basics/glossary/compound-interest |
| `unfcu-50-30-20` | budgeting | https://www.unfcu.org/guides/the-50-30-20-rule (`"license": "copyrighted"`) |

Every entry except the last has `"license": "public-domain"`. An entry may carry a `title` to override the extracted one.

## `app/rag/fetch.py` and `scripts/fetch_kb.py`

```python
def fetch_source(entry: dict, client: httpx.Client, today: date) -> str   # returns the .md file text
def fetch_all(kb_dir: Path, client: httpx.Client, refresh: bool = False) -> list[str]   # returns failed slugs
```
- GET with a browser User-Agent (gov sites block some bots), following redirects, 30 s timeout.
- `trafilatura.extract(html, output_format="markdown", include_tables=True, include_links=False)` for the body (tables matter: the Roth comparison chart is one). Title from the entry's `title`, else trafilatura's page metadata, else the slug.
- Front matter, flat `key: value` lines between `---` fences: `title`, `source_url`, `topic`, `fetched_at` (ISO date).
- A non-200 response, or a body under 150 words (a JavaScript-only or block page), is a failure. `fetch_all` logs it, keeps going and returns the failed slugs.
- Existing files are skipped unless `refresh=True`. Public-domain entries are written to `kb/sources/`, copyrighted ones to `kb/sources/local/`.

The script is a thin CLI: `fetch_kb.py [--refresh]` prints one line per source and exits 1 if any failed.

## `app/rag/chunk.py` (pure functions)

```python
@dataclass(frozen=True)
class Chunk:
    id: str            # "<slug>-<n>", n from 0, stable for the same file
    text: str
    title: str
    section: str
    source_url: str
    topic: str
    fetched_at: str

def parse_source(path: Path) -> tuple[dict, str]   # front matter, body
def chunk_source(path: Path) -> list[Chunk]
```
- `parse_source` raises `ValueError("<filename>: missing <key>")` when any of the four front-matter keys is missing or empty, so a file can never produce chunks that can't be cited.
- The body is split at markdown heading lines (`#` to `######`). Text before the first heading belongs to a section named after the document title. Sections with no body text are dropped.
- Within a section, paragraphs (split on blank lines) are packed into chunks of at most `CHUNK_WORDS = 375` words. Each chunk after the first in a section starts with the last `OVERLAP_WORDS = 38` words of the previous one. A single paragraph longer than `CHUNK_WORDS` is split by words with the same window.
- Chunks never cross a section boundary, so each one keeps one `section` heading.

## `app/rag/store.py`

```python
class KnowledgeBase:
    def __init__(self, path: Optional[Path] = None, embeddings: Optional[Embeddings] = None)
    def rebuild(self, chunks: list[Chunk]) -> int     # returns the chunk count
    def search(self, query: str, k: int = 4) -> list[dict]
    def count(self) -> int
```
- `chromadb.PersistentClient(path=<KB_DIR>/chroma, settings=Settings(anonymized_telemetry=False))`, collection `fin_guidance`, cosine distance, no Chroma embedding function.
- `embeddings` defaults to `get_embeddings()`. Tests inject a fake one.
- `rebuild` deletes the collection if it exists, recreates it, embeds in batches of 32 and adds documents with metadata `{title, section, source_url, topic, fetched_at, chunk_index}`. The embedded text is `search_document: <title> - <section>\n\n<text>`. The heading helps a short chunk match; the stored document is the plain chunk text.
- `search` embeds `search_query: <query>` and returns `[{title, section, source_url, text}]`, best match first.

`app/agents/llm.py` gains `get_embeddings() -> Embeddings`, which returns `OllamaEmbeddings(model=OLLAMA_EMBED_MODEL, base_url=OLLAMA_BASE_URL)`. That keeps llm.py the only place that constructs a model client, so an embedding-model comparison in M5 is a change there plus a re-ingest.

`scripts/ingest_kb.py` chunks `kb/sources/*.md` and `kb/sources/local/*.md`, calls `rebuild` and prints `N chunks from M sources`. `scripts/search_kb.py "<query>"` prints the top 4 (title, section, URL, the first 200 characters of text) for checking by hand.

## `search_kb` in `app/agents/tools.py`

`make_tools(gateway, kb)` now takes a `KnowledgeBase` and returns four tools.

```python
def search_kb(query: str) -> str
```
Description: *"Search trusted public financial guidance (CFPB, IRS, SEC investor.gov, FDIC) for general rules and strategies: retirement accounts, debt payoff methods, budgeting rules, savings and deposit insurance. Returns passages with source URLs; cite the source_url in your answer. Not for the user's own balances or spending."*

Output, compact JSON in the gateway's style:
```json
{"purpose":"kb","results":[{"title":"...","source_url":"https://...","section":"...","text":"..."}]}
```

| Case | Behavior |
|---|---|
| Empty or whitespace query | `ToolException("query must not be empty")` |
| Query over 1,000 characters | Trimmed to 1,000 characters, no error |
| Collection empty or missing | `ToolException("knowledge base is empty")`; a local warning says to run `scripts/ingest_kb.py` |
| Ollama down or model not pulled | Not caught here. `_run_tool` already turns it into `search_kb failed (ConnectError)` |

## Tests

Every module gets `docs/tests/<module>.md` (architecture section 12). Offline tests use a fake embedder: deterministic hashed bag-of-words vectors, normalized, which also records every string it was given. A query that shares words with one chunk ranks that chunk first, and prefixes can be asserted on.

**`tests/test_chunk.py`**:
- front matter is parsed; each missing or empty required key raises with the filename;
- splitting at headings, including text before the first heading and dropping empty sections;
- no chunk exceeds `CHUNK_WORDS`; consecutive chunks in a section overlap by `OVERLAP_WORDS`; chunks never cross sections;
- one paragraph longer than `CHUNK_WORDS` is split;
- a short document gives one chunk; ids are `<slug>-<n>` and identical across two runs.

**`tests/test_fetch_kb.py`** (an `httpx.MockTransport` serving saved HTML; no network):
- a good page becomes a file with all four front-matter keys and the article text, without nav boilerplate;
- a table on the page survives into the markdown;
- a 404 and a page under 150 words are reported as failures, and the other sources are still written;
- an existing file is skipped without `refresh` and rewritten with it;
- a copyrighted entry is written under `local/`.

**`tests/test_kb_store.py`** (Chroma in `tmp_path`):
- `rebuild` then `search` returns the matching chunk first, with all four result keys;
- running `rebuild` twice leaves exactly one copy of each chunk;
- documents were embedded with `search_document: ` and the query with `search_query: `;
- `count` is 0 before `rebuild`;
- `test_live_kb_search` (done-when): ingest the real `kb/sources/` into `tmp_path` with real `nomic-embed-text`. `"Roth IRA vs 401k"` has an irs.gov retirement page in its top 4, and `"which debt should I pay off first"` has `cfpb-reduce-debt`. Skipped unless Ollama lists `OLLAMA_EMBED_MODEL` and `kb/sources/` has files (same pattern as `test_llm.py`).

**`tests/test_tools.py`** (updated; `tools` fixture passes a fake KB):
- four tools, all with descriptions; `search_kb` takes exactly `query`, and still no tool takes a user;
- output JSON shape; an empty query errors; a 5,000-character query reaches the KB trimmed to 1,000;
- an empty KB gives the "knowledge base is empty" error.

## Docs

- `architecture.md`:
  - section 3: the embeddings row becomes nomic-embed-text via Ollama;
  - section 7: a note that `search_kb` bypasses the gateway, and why;
  - section 8: prefixes, `sources.json`, the fetch script, the license split and telemetry off.
- Decision log rows (2026-10-26): nomic over MiniLM; fetched sources committed except copyrighted ones; the collection rebuilt each ingest; `search_kb` outside the gateway.
- README: a "Knowledge base" section with the setup commands above.
- `.env.example`: `OLLAMA_EMBED_MODEL`, `KB_DIR`.

## Verification

1. `cd backend && ../venv/bin/python -m pytest` passes. The live KB test runs, because nomic-embed-text is pulled here.
2. Read every committed `kb/sources/*.md` by eye: right article, no menus or footers, tables intact. Fix extraction problems before ingesting.
3. `scripts/ingest_kb.py`, then `scripts/search_kb.py` for "Roth IRA vs 401k", "which debt should I pay off first", "how much should I keep in an emergency fund" and "what is the 50/30/20 rule". Record the top-4 sources for each in the PR description.

## Risks

- **Extraction picks up boilerplate or misses content** (government sites change their templates). Verification step 2 catches it; `fetch_kb.py --refresh` plus a per-entry `title` override fix most cases.
- **Some figures expire.** IRS contribution limits change every year. `fetched_at` records when the text was true, and `--refresh` updates it.
- **The model may not cite, or may cite the wrong URL.** That is TPC-15's system prompt and TPC-16's eval to measure. `search_kb` only makes the URL available.
- **Context size.** Four 500-token chunks is about 2k tokens per call. TPC-15 should set `num_ctx` explicitly in `llm.py`, so Ollama doesn't silently truncate the conversation.

## Follow-ups (not this ticket)

- TPC-15: bind `search_kb` in the chat graph, a system prompt that requires citing `source_url`, and an explicit `num_ctx`.
- TPC-16: eval scenarios that check citations, and evidence for whether a relevance cutoff is needed.
- M5 (optional): an embedding-model comparison (nomic vs MiniLM vs bge-small) on the same scenarios.
