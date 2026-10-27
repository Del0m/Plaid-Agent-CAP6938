"""Chunk and embed kb/sources/ into the Chroma collection behind search_kb.

Rebuilds the collection from scratch every run. Needs Ollama running with
OLLAMA_EMBED_MODEL pulled (`ollama pull nomic-embed-text`).

Run from any directory:
    python backend/scripts/ingest_kb.py
"""
import sys
from pathlib import Path

# make `app` importable however the script is launched (it lives in backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.rag.store import KnowledgeBase, ingest


def main() -> None:
    chunks, sources = ingest(config.KB_DIR, KnowledgeBase())
    print(f"{chunks} chunks from {sources} sources")


if __name__ == "__main__":
    main()
