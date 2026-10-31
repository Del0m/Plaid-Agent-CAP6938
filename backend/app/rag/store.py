import logging
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from langchain_core.embeddings import Embeddings

from .. import config
from ..agents.llm import get_embeddings
from .chunk import Chunk, chunk_source

# the knowledge base behind search_kb (architecture section 8). our code computes every vector,
# so chroma only stores and searches them and never downloads its own default model

logger = logging.getLogger(__name__)

COLLECTION = "fin_guidance"
BUILDING = COLLECTION + "_building"
# nomic-embed-text is trained with these task prefixes; leaving them off hurts retrieval
DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
BATCH = 32


class KnowledgeBase:
    def __init__(self, path: Optional[Path] = None, embeddings: Optional[Embeddings] = None):
        self._client = chromadb.PersistentClient(path=str(path or config.KB_DIR / "chroma"),
                                                 settings=Settings(anonymized_telemetry=False))
        self._embeddings = embeddings or get_embeddings()

    def _collection(self, name: str = COLLECTION):
        return self._client.get_or_create_collection(
            name, embedding_function=None, configuration={"hnsw": {"space": "cosine"}})

    def _drop(self, name: str) -> None:
        try:
            self._client.delete_collection(name)
        except NotFoundError:   # first ingest: nothing to drop
            pass

    # build a new collection and swap it in: with ~16 docs this takes seconds and can't leave stale or
    # duplicate chunks. everything is embedded and added before the old one is dropped, so a failure
    # anywhere keeps the last good knowledge base
    def rebuild(self, chunks: list[Chunk]) -> int:
        vectors = []
        for start in range(0, len(chunks), BATCH):
            # the heading helps a short chunk match; the stored document stays the plain text
            vectors += self._embeddings.embed_documents(
                [f"{DOC_PREFIX}{c.title} - {c.section}\n\n{c.text}" for c in chunks[start:start + BATCH]])
        self._drop(BUILDING)    # left over from a rebuild that failed
        coll = self._collection(BUILDING)
        for start in range(0, len(chunks), BATCH):
            batch = chunks[start:start + BATCH]
            coll.add(
                ids=[c.id for c in batch], documents=[c.text for c in batch],
                embeddings=vectors[start:start + BATCH],
                metadatas=[{"title": c.title, "section": c.section, "source_url": c.source_url,
                            "topic": c.topic, "fetched_at": c.fetched_at, "chunk_index": c.index}
                           for c in batch],
            )
        self._drop(COLLECTION)
        coll.modify(name=COLLECTION)
        return coll.count()

    def count(self) -> int:
        return self._collection().count()

    def search(self, query: str, k: int = 4) -> list[dict]:
        coll = self._collection()
        n = min(k, coll.count())   # older chroma raises when asked for more results than it holds
        if n == 0:
            return []
        res = coll.query(query_embeddings=[self._embeddings.embed_query(f"{QUERY_PREFIX}{query}")], n_results=n)
        return [{"title": m["title"], "section": m["section"], "source_url": m["source_url"], "text": doc}
                for doc, m in zip(res["documents"][0], res["metadatas"][0])]


# committed sources first, then the git-ignored local ones; sorted so chunk order is repeatable
def ingest(kb_dir: Path, kb: KnowledgeBase) -> tuple[int, int]:
    sources = kb_dir / "sources"
    paths = sorted(sources.glob("*.md")) + sorted((sources / "local").glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no sources in {sources}; run scripts/fetch_kb.py")
    chunks, used = [], 0
    for p in paths:
        # a source that can't be cited is refused on its own; the others still get indexed
        try:
            chunks += chunk_source(p)
            used += 1
        except ValueError as exc:
            logger.warning("skipped %s", exc)
    return kb.rebuild(chunks), used
