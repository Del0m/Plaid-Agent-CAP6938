import json
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import trafilatura

logger = logging.getLogger(__name__)

# downloads the pages listed in kb/sources.json and saves each one's article text as markdown
# with the front matter chunk.py needs to cite it (architecture section 8)

# some gov sites turn away clients that don't look like a browser
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# below this the extractor most likely got a block page or a page that renders in javascript
MIN_WORDS = 150


class FetchError(Exception):
    pass


# public-domain pages are committed; copyrighted ones stay in the git-ignored local/ folder
def source_path(kb_dir: Path, entry: dict) -> Path:
    folder = kb_dir / "sources"
    if entry.get("license") == "copyrighted":
        folder = folder / "local"
    return folder / f"{entry['slug']}.md"


def fetch_source(entry: dict, client: httpx.Client, today: date) -> str:
    resp = client.get(entry["url"], headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}")
    body = trafilatura.extract(resp.text, output_format="markdown", include_tables=True, include_links=False)
    words = len(body.split()) if body else 0
    if words < MIN_WORDS:
        raise FetchError(f"only {words} words extracted")

    meta = trafilatura.extract_metadata(resp.text)
    title = entry.get("title") or (meta.title if meta else None) or entry["slug"]
    # the url we listed, not where it redirected to: it's the one we chose to cite
    front = {"title": " ".join(title.split()), "source_url": entry["url"], "topic": entry["topic"],
             "fetched_at": today.isoformat()}
    return "---\n" + "".join(f"{k}: {v}\n" for k, v in front.items()) + "---\n\n" + body.strip() + "\n"


# keeps going past failures so one moved page doesn't block the rest; returns the failed slugs
def fetch_all(kb_dir: Path, client: httpx.Client, refresh: bool = False,
              today: Optional[date] = None) -> list[str]:
    entries = json.loads((kb_dir / "sources.json").read_text(encoding="utf-8"))
    failed = []
    for entry in entries:
        path = source_path(kb_dir, entry)
        if path.exists() and not refresh:
            logger.info("skip %s", entry["slug"])
            continue
        try:
            text = fetch_source(entry, client, today or date.today())
        except (FetchError, httpx.HTTPError) as exc:
            logger.warning("FAIL %s: %s", entry["slug"], exc)
            failed.append(entry["slug"])
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        logger.info("ok %s", entry["slug"])
    return failed
