"""Print the top 4 knowledge-base results for a query, to check retrieval by hand.

Run after scripts/ingest_kb.py, from any directory:
    python backend/scripts/search_kb.py "Roth IRA vs 401k"
"""
import sys
from pathlib import Path

# make `app` importable however the script is launched (it lives in backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.store import KnowledgeBase


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    for rank, r in enumerate(KnowledgeBase().search(sys.argv[1]), start=1):
        print(f"{rank}. {r['title']} / {r['section']}\n   {r['source_url']}\n   {' '.join(r['text'].split())[:200]}\n")


if __name__ == "__main__":
    main()
