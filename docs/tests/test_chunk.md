# Tests: `backend/tests/test_chunk.py`

These tests cover `app/rag/chunk.py`, which turns one `kb/sources/*.md` file into the chunks the knowledge base embeds (TPC-14). It does three jobs:
- reads the front matter that makes a chunk citable;
- splits the document at its headings;
- packs paragraphs into chunks of at most 375 words (~500 tokens), with a 38-word (~50-token) overlap.

Everything here is pure: no Chroma, no Ollama, no network.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_chunk.py -v
```

## Shared setup

- `write_source(tmp_path, body, slug="doc", **front)` writes `<slug>.md` with all four front-matter keys (`title`, `source_url`, `topic`, `fetched_at`). Passing `key=None` drops a key; `key=""` leaves it empty.
- `words(n, start=0)` makes distinct numbered words (`w0 w1 ...`), so a test can check exactly which words ended up in which chunk.

---

## `test_parse_front_matter`

**What it does:** Checks that `parse_source` returns the four front-matter values and a body without the `---` fences.

**Why it's there:** Every chunk's citation comes from these values. If the fences leaked into the body, they would be embedded and shown to the model as guidance text.

## `test_missing_or_empty_key_raises`

**What it does:** For each of the four keys, missing and then present but empty, checks that `parse_source` raises `ValueError` naming the file and the key.

**Why it's there:** A chunk without a `source_url` can't be cited, and one without `fetched_at` can't say when an IRS limit was true. Ingest should refuse the file loudly, so we never quietly build chunks with no citation.

## `test_title_with_colon_and_unicode_round_trips`

**What it does:** Checks that a title containing a colon, curly quotes, an en dash and `é` parses back exactly.

**Why it's there:** Government page titles often contain colons (`Retirement topics: ...`). Splitting on every colon, or reading the file in a non-UTF-8 encoding, would cut or mangle the title shown in citations.

## `test_splits_at_headings`

**What it does:** Checks that text before the first heading becomes a section named after the document, that `#` and `##` headings start new sections, and that a heading with no text under it produces no chunk.

**Why it's there:** Each chunk carries one section heading. The heading is embedded with the chunk text and shown in results, so the boundaries must follow the document's own structure.

## `test_chunks_respect_limit_and_overlap`

**What it does:** Packs nine 100-word paragraphs. It checks that every chunk has at most 375 words, that each chunk starts with the previous one's last 38 words, and that no word is lost.

**Why it's there:** These are the chunk size and overlap the plan asks for. The overlap means a sentence that falls across a chunk boundary can still be found.

## `test_long_paragraph_is_split`

**What it does:** Checks that a single 1,000-word paragraph is split into at least three chunks, with the same size limit, the same overlap, and no lost words.

**Why it's there:** Extracted pages sometimes come back as one huge paragraph. Without this split, that text would become one oversized chunk.

## `test_chunks_never_cross_sections`

**What it does:** Checks that two 300-word sections produce exactly two chunks, and that the second holds only section B's words.

**Why it's there:** A chunk mixing two sections would carry the wrong heading. A search would then show text under a section it doesn't belong to.

## `test_table_paragraph_stays_whole`

**What it does:** Checks that a five-row markdown table appears whole, with its line breaks, in exactly one chunk.

**Why it's there:** The IRS Roth comparison chart is a table. If its rows were split up or flattened onto one line, the agent would read a Roth 401(k) rule as a Roth IRA rule.

## `test_short_doc_one_chunk_stable_ids`

**What it does:** Checks that a short file gives one chunk with id `<slug>-0`, that chunking the same file twice gives identical chunks, and that the citation fields are copied from the front matter.

**Why it's there:** Stable ids make re-ingesting repeatable and let a result be traced back to its file. Copying the front matter onto each chunk is how a citation reaches the model.

## `test_local_source_ids_dont_collide`

**What it does:** Writes `foo.md` and `local/foo.md` and checks that their chunk ids are `foo-0` and `local-foo-0`.

**Why it's there:** Ingest reads both `sources/*.md` and `sources/local/*.md`. After a license change in `sources.json` (or a hand copy), both can exist with the same stem. With ids built from the stem alone, Chroma refused the batch or quietly skipped one file's chunks, so that source was missing from `search_kb`. The review on #8 found it. Committed ids are unchanged.

## `test_chunk_index_matches_id`

**What it does:** Chunks a two-chunk-long document and checks that each chunk's `index` equals the number its id ends in.

**Why it's there:** `chunk_index` is stored as metadata in Chroma, and it used to be the chunk's position across every source in the ingest. Chunk `cfpb-reduce-debt-0` could be stored with index 17. `index` is now set per document where the id is built.
