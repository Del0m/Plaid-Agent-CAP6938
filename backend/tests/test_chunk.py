import pytest

from app.rag.chunk import CHUNK_WORDS, OVERLAP_WORDS, REQUIRED_KEYS, chunk_source, parse_source

FRONT = {"title": "Doc Title", "source_url": "https://example.gov/doc", "topic": "retirement",
         "fetched_at": "2026-10-26"}


# writes <slug>.md with all four front-matter keys unless a test drops one (None) or blanks it ("")
def write_source(tmp_path, body, slug="doc", **front):
    fields = {**FRONT, **front}
    lines = ["---"] + [f"{k}: {v}" for k, v in fields.items() if v is not None] + ["---", ""]
    path = tmp_path / f"{slug}.md"
    path.write_text("\n".join(lines) + body, encoding="utf-8")
    return path


# distinct numbered words, so overlap and section membership can be checked word by word
def words(n, start=0):
    return " ".join(f"w{i}" for i in range(start, start + n))


def test_parse_front_matter(tmp_path):
    front, body = parse_source(write_source(tmp_path, "Body text.\n"))

    assert front == FRONT
    assert body.strip() == "Body text."


@pytest.mark.parametrize("value", [None, ""], ids=["missing", "empty"])
@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_missing_or_empty_key_raises(tmp_path, key, value):
    path = write_source(tmp_path, "Body.\n", **{key: value})

    with pytest.raises(ValueError, match=f"doc.md: missing {key}"):
        parse_source(path)


def test_title_with_colon_and_unicode_round_trips(tmp_path):
    title = "Retirement topics: IRA “limits” – café"

    front, _ = parse_source(write_source(tmp_path, "Body.\n", title=title))

    assert front["title"] == title


def test_splits_at_headings(tmp_path):
    path = write_source(tmp_path, "intro\n\n# A\n\ntext a\n\n## B\n\ntext b\n\n# Empty\n")

    chunks = chunk_source(path)

    assert [c.section for c in chunks] == ["Doc Title", "A", "B"]
    assert [c.text for c in chunks] == ["intro", "text a", "text b"]


def test_chunks_respect_limit_and_overlap(tmp_path):
    paragraphs = [words(100, start=i * 100) for i in range(9)]
    path = write_source(tmp_path, "# A\n\n" + "\n\n".join(paragraphs) + "\n")

    chunks = [c.text.split() for c in chunk_source(path)]

    assert len(chunks) > 1
    assert all(len(c) <= CHUNK_WORDS for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[:OVERLAP_WORDS] == prev[-OVERLAP_WORDS:]
    assert set(w for c in chunks for w in c) == set(words(900).split())


def test_long_paragraph_is_split(tmp_path):
    path = write_source(tmp_path, words(1000) + "\n")

    chunks = [c.text.split() for c in chunk_source(path)]

    assert len(chunks) >= 3
    assert all(len(c) <= CHUNK_WORDS for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt[:OVERLAP_WORDS] == prev[-OVERLAP_WORDS:]
    assert set(w for c in chunks for w in c) == set(words(1000).split())


def test_chunks_never_cross_sections(tmp_path):
    path = write_source(tmp_path, f"# A\n\n{words(300)}\n\n# B\n\n{words(300, start=1000)}\n")

    chunks = chunk_source(path)

    assert [c.section for c in chunks] == ["A", "B"]
    assert set(chunks[1].text.split()) == set(words(300, start=1000).split())


def test_table_paragraph_stays_whole(tmp_path):
    table = "\n".join([
        "| Feature | Roth 401(k) | Roth IRA |",
        "| --- | --- | --- |",
        "| Contributions | Employee elective deferrals | After-tax |",
        "| Income limits | None | Phased out above a limit |",
        "| Required distributions | None | None for the owner |",
    ])
    path = write_source(tmp_path, f"# Comparison\n\nThe chart below.\n\n{table}\n\nAfter the table.\n")

    chunks = chunk_source(path)

    assert sum(table in c.text for c in chunks) == 1


def test_short_doc_one_chunk_stable_ids(tmp_path):
    path = write_source(tmp_path, "Some text.\n", slug="irs-roth-iras")

    chunks = chunk_source(path)

    assert [c.id for c in chunks] == ["irs-roth-iras-0"]
    assert chunk_source(path) == chunks
    c = chunks[0]
    assert (c.title, c.source_url, c.topic, c.fetched_at) == tuple(FRONT.values())
    assert c.section == "Doc Title"


# review on #8: sources/foo.md and sources/local/foo.md both exist after a license change, so ids must differ
def test_local_source_ids_dont_collide(tmp_path):
    (tmp_path / "local").mkdir()
    committed = write_source(tmp_path, "Some text.\n", slug="foo")
    local = write_source(tmp_path / "local", "Other text.\n", slug="foo")

    assert [c.id for c in chunk_source(committed)] == ["foo-0"]
    assert [c.id for c in chunk_source(local)] == ["local-foo-0"]


# a chunk's index is its position in its own document, the number its id ends in
def test_chunk_index_matches_id(tmp_path):
    path = write_source(tmp_path, words(CHUNK_WORDS * 2) + "\n")

    chunks = chunk_source(path)

    assert len(chunks) > 1
    assert [c.index for c in chunks] == [int(c.id.rsplit("-", 1)[1]) for c in chunks]
