import hashlib
import math

import pytest
from langchain_core.embeddings import Embeddings

from app.rag.chunk import Chunk
from app.rag.store import DOC_PREFIX, QUERY_PREFIX, KnowledgeBase


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
                 source_url=f"https://example.gov/{slug}", topic="t", fetched_at="2026-10-26")


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
