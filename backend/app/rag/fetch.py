import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import lxml.html
import trafilatura

logger = logging.getLogger(__name__)

# downloads the pages listed in kb/sources.json and saves each one's article text as markdown
# with the front matter chunk.py needs to cite it (architecture section 8)

# some gov sites turn away clients that don't look like a browser
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
# below this the extractor most likely got a block page or a page that renders in javascript
MIN_WORDS = 150


# lines cfpb ends every article with: a newsletter box, a last-modified stamp and link stubs
_BOILERPLATE = re.compile(r"^(#+ Stay informed|Sign up for the latest financial tips.*|Page last modified\b.*|Read more)$")


class FetchError(Exception):
    pass


# inline markup joins with no gap ("<strong>dollars</strong>."); blocks and list items keep apart
def _text(el) -> str:
    for child in el.iter("p", "div", "br", "li"):
        child.tail = (", " if child.tag == "li" else " ") + (child.tail or "")
    return " ".join("".join(el.itertext()).split()).replace(" ,", ",").rstrip(", ")


# trafilatura's markdown tables lose headers with inline markup and lists inside cells (the irs
# roth chart loses its income limits), so each row becomes one line that names its row and columns
def _flatten_tables(html: str) -> str:
    doc = lxml.html.fromstring(html)
    for table in doc.xpath("//table"):
        rows = [[_text(c) for c in tr.xpath("./th|./td")] for tr in table.xpath(".//tr")]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        has_header = bool(table.xpath(".//thead") or table.xpath(".//tr[1]/th"))
        headers, body = (rows[0], rows[1:]) if has_header else ([], rows)
        div = lxml.html.Element("div")
        caption = table.xpath("./caption")
        if caption:
            lxml.html.etree.SubElement(div, "h2").text = _text(caption[0])
        for cells in body:
            labeled = [f"{h}: {c}" if h else c for h, c in zip(headers[1:], cells[1:]) if c] if headers \
                else cells[1:]
            line = f"{cells[0]} — {'; '.join(labeled)}" if labeled else cells[0]
            lxml.html.etree.SubElement(div, "p").text = line
        table.getparent().replace(table, div)
    return lxml.html.tostring(doc, encoding="unicode")


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
    body = trafilatura.extract(_flatten_tables(resp.text), output_format="markdown", include_links=False)
    body = "\n".join(line for line in (body or "").split("\n") if not _BOILERPLATE.match(line.strip()))
    words = len(body.split())
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
