"""Download the knowledge-base pages listed in kb/sources.json into kb/sources/.

Each page's article text is saved as markdown with front matter (title, source_url,
topic, fetched_at). Pages already on disk are skipped unless --refresh is given.
Copyrighted pages go to kb/sources/local/, which is git-ignored.

Run from any directory, then read the new files before ingesting them:
    python backend/scripts/fetch_kb.py [--refresh]
"""
import argparse
import logging
import sys
from pathlib import Path

import httpx

# make `app` importable however the script is launched (it lives in backend/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.rag.fetch import fetch_all


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="fetch again even if the file exists")
    args = parser.parse_args()

    # fetch_all logs one line per source: ok, skip or FAIL
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client() as client:
        failed = fetch_all(config.KB_DIR, client, refresh=args.refresh)
    if failed:
        print(f"{len(failed)} failed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
