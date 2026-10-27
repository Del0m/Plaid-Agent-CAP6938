# Tests: `backend/tests/test_kb_store.py`

These tests cover `app/rag/store.py`: the `KnowledgeBase` class behind `search_kb` (TPC-14). It stores chunks in the Chroma collection `fin_guidance` (cosine distance) and searches them. Our code computes every vector, using `nomic-embed-text` with its task prefixes; Chroma only stores and searches.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_kb_store.py -v
```

## Shared setup

- `HashEmbeddings` stands in for Ollama. It turns a text into a normalized 64-dimension bag-of-words vector, so texts that share words point the same way and search ranking is predictable. It also records every string it embeds, which lets tests check the prefixes.
- `chunk(slug, text)` builds a `Chunk` with a title, section and URL derived from the slug.
- `CHUNKS` holds three chunks on clearly different topics: a Roth IRA, a credit card APR and an emergency fund.
- `kb` is a `KnowledgeBase` stored under `tmp_path`, using `HashEmbeddings`. These tests never touch the real `kb/chroma/` or Ollama.

---

## `test_count_zero_before_rebuild`

**What it does:** Checks that a new knowledge base reports 0 chunks and that a search on it returns an empty list.

**Why it's there:** `search_kb` checks `count()` to tell the model the knowledge base is empty. Searching an empty collection must also return nothing rather than crash.

## `test_search_returns_best_match_first`

**What it does:** Ingests the three chunks and searches "credit card apr". It checks that the APR chunk comes first with exactly `title`, `section`, `source_url` and `text`, and that all three chunks are returned.

**Why it's there:** This is the core retrieval contract. The result keys are exactly what `search_kb` sends to the model, and there are no scores for it to misread.

## `test_rebuild_twice_no_duplicates`

**What it does:** Rebuilds with the same chunks twice and checks the count stays 3. Then it rebuilds with one chunk and checks the count drops to 1.

**Why it's there:** Every ingest drops the collection and refills it. Duplicates would fill the top 4 with copies of one chunk, and chunks left over from a deleted source would keep being cited.

## `test_prefixes`

**What it does:** Checks that every document is embedded as `search_document: <title> - <section>\n\n<text>` and every query as `search_query: <query>`.

**Why it's there:** nomic-embed-text was trained with these task prefixes, and retrieval gets clearly worse without them. A refactor that dropped them would pass every other test and quietly make search worse.

## `test_search_with_fewer_chunks_than_k`

**What it does:** Ingests two chunks, asks for four results, and checks that two come back without an error.

**Why it's there:** Older Chroma versions raise when asked for more results than the collection holds. A small or partly fetched knowledge base should still work.

## `test_persists_across_instances`

**What it does:** Rebuilds, then opens a second `KnowledgeBase` on the same directory and checks it sees the same 3 chunks.

**Why it's there:** Ingest runs as a script and chat runs in the API process. The collection has to survive on disk between the two.
