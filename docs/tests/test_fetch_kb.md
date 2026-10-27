# Tests: `backend/tests/test_fetch_kb.py`

These tests cover `app/rag/fetch.py`, which downloads the pages listed in `kb/sources.json` and saves each one's article text as markdown with front matter (TPC-14). `scripts/fetch_kb.py` is a thin CLI over `fetch_all`.

No test touches the network. An `httpx.MockTransport` serves saved HTML; any URL without a route gets a 404.

Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_fetch_kb.py -v
```

## Shared setup

- `TABLE` is shaped like the IRS Roth comparison chart. It has a caption, a header cell with inline `<strong>&nbsp;</strong>` markup, `<p>` inside cells, and a cell holding a list of income limits.
- `page(title, paragraphs, extra)` builds a page shaped like a government one: a `<nav>` menu (including `Contact Menu`) and a footer around one `<article>`. The article has headings, about 360 words of paragraphs, `TABLE`, and any `extra` HTML.
- `entry(slug, url, **extra)` is one `sources.json` entry (topic `saving`, license `public-domain` unless overridden). `make_kb` writes the entries to a temp `sources.json`.
- `client(routes, seen)` is the mock HTTP client. `seen` collects every request so tests can check headers and whether anything was fetched at all.
- `TODAY` is 2026-10-26.

---

## `test_writes_front_matter_and_article`

**What it does:** Fetches one page and checks that the saved file has exactly the four front-matter values (title, the listed URL, topic, today's date). It also checks that the body has the article text and none of the nav or footer text.

**Why it's there:** These four values are what make every chunk citable. Menu and footer text in the body would be embedded as if it were guidance, and could come back as a search result.

## `test_table_rows_become_labeled_lines`

**What it does:** Checks that each table row becomes one line naming its row and every column, for example `Contributions — Roth 401(k): …; Roth IRA: …`. It also checks that the list inside a cell survives (`Income limits: 2024 – single $161,000, 2023 – single $153,000`) and that the caption is kept as a heading.

**Why it's there:** Trafilatura's own markdown tables mangled the real IRS chart. They dropped the "Designated Roth 401(k)" header, split cells across lines, and lost the Roth IRA income limits entirely. A line that carries its own labels reads correctly even when a chunk holds only part of the table, which matters for a "Roth IRA vs 401k" question.

## `test_drops_site_boilerplate_lines`

**What it does:** Adds CFPB's end-of-article lines (`Read more`, `Stay informed`, the newsletter sign-up, `Page last modified @`). It checks they're all removed while the article text stays.

**Why it's there:** Every CFPB page in the corpus ends with these lines. If they stayed, they'd be embedded as guidance, and the newsletter line would sit inside chunks of real content.

## `test_title_override_and_fallback`

**What it does:** Checks that an entry's `title` in `sources.json` wins over the page's own title, and that the page's title is used when there's no override.

**Why it's there:** Some pages have titles that are useless as citations (site names, "Home"). The override fixes those without editing the fetched file by hand.

## `test_title_with_colon_and_unicode_round_trips`

**What it does:** Fetches a page titled `Retirement topics: IRA “limits”` and checks that `parse_source` reads exactly that title back.

**Why it's there:** The front matter is written by `fetch.py` and read by `chunk.py`. A colon or curly quote in a title must survive both, or the citation is wrong.

## `test_follows_redirect`

**What it does:** Serves a 301 from the listed URL to the same URL with a trailing slash. It checks that the page is still saved and that `source_url` is the listed URL, not the redirect target.

**Why it's there:** Government sites redirect often. Without following redirects, a moved page would be a failure. The URL we cite should be the one we chose to list.

## `test_failures_reported_others_written`

**What it does:** Runs three entries: one 404, one page with too few words, and one good page. It checks that the two bad slugs are returned in order and that only the good file is written.

**Why it's there:** One moved or blocked page must not stop the rest. A page under 150 words is almost always a block page or one that only renders in JavaScript, and should never be saved as guidance.

## `test_skips_existing_unless_refresh`

**What it does:** Checks that an existing file is left alone with no request made, and that it's fetched again and rewritten with `refresh=True`.

**Why it's there:** The committed files are the exact text we cite, so a plain run must not quietly change them. `--refresh` is the deliberate way to update them, for example when IRS limits change.

## `test_copyrighted_goes_to_local`

**What it does:** Checks that an entry with `"license": "copyrighted"` is written to `sources/local/` and not to `sources/`.

**Why it's there:** `sources/local/` is git-ignored, so pages we can't redistribute are never committed to the repo.

## `test_sends_user_agent`

**What it does:** Checks that the request carries the browser `User-Agent`.

**Why it's there:** Some government sites reject clients that don't look like a browser. Without the header, fetching would fail on the real sites even though every other test passes.

## `test_unparseable_page_reported_others_written`

**What it does:** Runs two entries: a page that can't be parsed, then a good one. It's run twice, once with an empty 200 response and once with a page that starts with an `<?xml … encoding="UTF-8"?>` declaration. Either way, the run must finish, report at most the bad slug, and still write the good page.

**Why it's there:** lxml raises on both inputs. Before this fix, that exception escaped `fetch_all` and stopped the whole `--refresh` run. A government CDN returning one blank page would then leave every later source un-refreshed, with no list of failures.

## `test_row_headers_are_not_column_labels`

**What it does:** Flattens a table whose rows start with `<th>` row labels (`Age 50+ | $8,000`, `Under 50 | $7,000`) and has no header row. It checks that each row keeps its own label and figure.

**Why it's there:** The old check treated any first row containing a `<th>` as column headers. That turned "Age 50+" and "$8,000" into labels and produced `Under 50 — $8,000: $7,000`, attaching the wrong figure to a row. IRS limit tables have exactly this shape, and the agent cites these figures as fact.

## `test_blank_thead_keeps_first_data_row`

**What it does:** Flattens a table with an empty `<thead>` and checks that the first data row is still a data row.

**Why it's there:** The header has to be chosen before empty rows are filtered out. Otherwise a blank header row is skipped and the first real row becomes the column labels.
