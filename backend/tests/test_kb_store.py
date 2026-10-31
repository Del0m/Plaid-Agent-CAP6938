import hashlib
import math

import httpx
import pytest
from langchain_core.embeddings import Embeddings

from app import config
from app.rag.chunk import Chunk
from app.rag.store import DOC_PREFIX, QUERY_PREFIX, KnowledgeBase, ingest


# deterministic bag-of-words vectors: texts sharing words point the same way. records every
# string it embeds, so tests can check the nomic prefixes. stands in for ollama
class HashEmbeddings(Embeddings):
    DIM = 64

    def __init__(self):
        self.seen = []

    def _vector(self, text):
        v = [0.0] * self.DIM
        for word in text.lower().split():
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts):
        self.seen.extend(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        self.seen.append(text)
        return self._vector(text)


def chunk(slug, text, n=0, section="Overview"):
    return Chunk(id=f"{slug}-{n}", text=text, title=f"{slug} title", section=section,
                 source_url=f"https://example.gov/{slug}", topic="t", fetched_at="2026-10-26", index=n)


CHUNKS = [
    chunk("roth", "a roth ira grows tax free and qualified withdrawals in retirement are tax free"),
    chunk("apr", "a credit card apr is the yearly interest rate charged on a card balance"),
    chunk("fund", "an emergency fund covers surprise expenses like a car repair or a medical bill"),
]


@pytest.fixture
def embeddings():
    return HashEmbeddings()


@pytest.fixture
def kb(tmp_path, embeddings):
    return KnowledgeBase(tmp_path / "chroma", embeddings=embeddings)


def test_count_zero_before_rebuild(kb):
    assert kb.count() == 0
    assert kb.search("anything") == []


def test_search_returns_best_match_first(kb):
    kb.rebuild(CHUNKS)

    results = kb.search("credit card apr")

    assert results[0] == {"title": "apr title", "section": "Overview", "source_url": "https://example.gov/apr",
                          "text": CHUNKS[1].text}
    assert len(results) == 3


def test_rebuild_twice_no_duplicates(kb):
    assert kb.rebuild(CHUNKS) == 3
    kb.rebuild(CHUNKS)
    assert kb.count() == 3

    kb.rebuild(CHUNKS[:1])
    assert kb.count() == 1


def test_prefixes(kb, embeddings):
    kb.rebuild(CHUNKS)
    docs = list(embeddings.seen)
    kb.search("credit card apr")

    assert docs == [f"{DOC_PREFIX}{c.title} - {c.section}\n\n{c.text}" for c in CHUNKS]
    assert DOC_PREFIX == "search_document: "
    assert embeddings.seen[-1] == f"{QUERY_PREFIX}credit card apr"
    assert QUERY_PREFIX == "search_query: "


def test_search_with_fewer_chunks_than_k(kb):
    kb.rebuild(CHUNKS[:2])

    assert len(kb.search("roth ira", k=4)) == 2


def test_persists_across_instances(tmp_path, kb):
    kb.rebuild(CHUNKS)

    assert KnowledgeBase(tmp_path / "chroma", embeddings=HashEmbeddings()).count() == 3


# ollama dropping out mid-ingest must leave the last good knowledge base, not a partial one
def test_failed_rebuild_keeps_previous_kb(tmp_path):
    embeddings = HashEmbeddings()
    kb = KnowledgeBase(tmp_path / "chroma", embeddings=embeddings)
    kb.rebuild(CHUNKS)
    calls = []

    def flaky(texts):
        calls.append(texts)
        if len(calls) == 2:
            raise ConnectionError("ollama went away")
        return HashEmbeddings.embed_documents(embeddings, texts)

    embeddings.embed_documents = flaky
    many = [chunk(f"doc{i}", f"text number {i}") for i in range(40)]   # two batches of 32

    with pytest.raises(ConnectionError):
        kb.rebuild(many)
    assert kb.count() == 3
    assert kb.search("credit card apr")[0]["source_url"] == "https://example.gov/apr"


# review on #8: a failure while adding (not embedding) must also keep the last good knowledge base
def test_failed_add_keeps_previous_kb(kb):
    kb.rebuild(CHUNKS)
    many = [chunk(f"doc{i}", f"text number {i}") for i in range(40)]
    many[35] = chunk("doc34", "a duplicate id inside the second batch")   # chroma refuses the batch

    with pytest.raises(Exception):
        kb.rebuild(many)
    assert kb.count() == 3
    assert kb.search("credit card apr")[0]["source_url"] == "https://example.gov/apr"
    assert kb.rebuild(CHUNKS) == 3     # the half-built collection doesn't block the next rebuild


# chunk_index is the chunk's place in its document, not in the whole ingest
def test_chunk_index_is_per_document(kb):
    kb.rebuild([chunk("a", "first a", 0), chunk("a", "second a", 1), chunk("b", "first b", 0)])

    got = kb._collection().get(include=["metadatas"])
    assert dict(zip(got["ids"], (m["chunk_index"] for m in got["metadatas"]))) == {"a-0": 0, "a-1": 1, "b-0": 0}


# one source with bad front matter is skipped with a warning; the rest still get indexed
def test_ingest_skips_a_bad_source(tmp_path, kb, caplog):
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "good.md").write_text("---\ntitle: Good\nsource_url: https://example.gov/g\ntopic: t\n"
                                     "fetched_at: 2026-10-26\n---\n\nSome guidance.\n", encoding="utf-8")
    (sources / "bad.md").write_text("---\ntitle:  \n---\n\nNo citation.\n", encoding="utf-8")

    assert ingest(tmp_path, kb) == (1, 1)
    assert "bad.md" in caplog.text

def test_ingest_without_sources_raises(tmp_path, kb):
    with pytest.raises(FileNotFoundError, match="run scripts/fetch_kb.py"):
        ingest(tmp_path, kb)


# ollama lists pulled models with a tag ("nomic-embed-text:latest"); the bare name means :latest
def _ollama_has_embed_model() -> bool:
    model = config.OLLAMA_EMBED_MODEL
    try:
        names = [m["name"] for m in httpx.get(f"{config.OLLAMA_BASE_URL}/api/tags", timeout=2).json()["models"]]
    except Exception:
        return False
    return model in names or f"{model}:latest" in names


live = pytest.mark.skipif(
    not (_ollama_has_embed_model() and any((config.KB_DIR / "sources").glob("*.md"))),
    reason="needs ollama with OLLAMA_EMBED_MODEL pulled and fetched kb/sources",
)


# the real corpus and the real embedding model, ingested once for every live case
@pytest.fixture(scope="module")
def real_kb(tmp_path_factory):
    kb = KnowledgeBase(tmp_path_factory.mktemp("chroma"))
    ingest(config.KB_DIR, kb)
    return kb


# the ticket's done-when, plus the M2 question it exists for
@live
@pytest.mark.parametrize("query, cites", [
    ("Roth IRA vs 401k", lambda url: url.startswith("https://www.irs.gov/retirement-plans")),
    ("which debt should I pay off first", lambda url: "how-reduce-your-debt" in url),
], ids=["roth-vs-401k", "which-debt-first"])
def test_live_kb_search(real_kb, query, cites):
    urls = [r["source_url"] for r in real_kb.search(query)]

    assert any(cites(u) for u in urls), urls
