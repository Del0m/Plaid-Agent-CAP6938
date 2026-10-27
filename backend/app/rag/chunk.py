import re
from dataclasses import dataclass
from pathlib import Path

# turns one kb/sources/*.md file into citable chunks (architecture section 8).
# sizes are in words: ~0.75 words per token, so 375/38 words is the ~500/50 tokens the plan asks for.
# nomic-embed-text has no limit near 500 tokens, so an estimate is enough and needs no tokenizer.

REQUIRED_KEYS = ("title", "source_url", "topic", "fetched_at")
CHUNK_WORDS = 375
OVERLAP_WORDS = 38

_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BLANK_LINE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    title: str
    section: str
    source_url: str
    topic: str
    fetched_at: str


# flat `key: value` lines between `---` fences; split on the first colon only, titles have colons
def parse_source(path: Path) -> tuple[dict, str]:
    lines = path.read_text(encoding="utf-8").split("\n")
    front, body = {}, "\n".join(lines)
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
        else:
            front = {}   # no closing fence: not front matter
    # a chunk without these can't be cited, so refuse the file instead of ingesting it
    for key in REQUIRED_KEYS:
        if not front.get(key):
            raise ValueError(f"{path.name}: missing {key}")
    return front, body


# (heading, paragraphs) per section; text before the first heading belongs to the document title
def _sections(title: str, body: str) -> list[tuple[str, list[str]]]:
    sections, heading, lines = [], title, []

    def close():
        paragraphs = [p.strip() for p in _BLANK_LINE.split("\n".join(lines)) if p.strip()]
        if paragraphs:
            sections.append((heading, paragraphs))

    for line in body.split("\n"):
        m = _HEADING.match(line)
        if m:
            close()
            heading, lines = m.group(1), []
        else:
            lines.append(line)
    close()
    return sections


# packs whole paragraphs (tables stay intact) into chunks of at most CHUNK_WORDS, each chunk
# after the first starting with the previous one's last OVERLAP_WORDS words
def _pack(paragraphs: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = fresh = 0   # words in current, and how many of those weren't carried over

    def flush():
        nonlocal current, size, fresh
        text = "\n\n".join(current)
        chunks.append(text)
        tail = text.split()[-OVERLAP_WORDS:]
        current, size, fresh = [" ".join(tail)], len(tail), 0

    for p in paragraphs:
        n = len(p.split())
        if size + n > CHUNK_WORDS and fresh:
            flush()
        if size + n <= CHUNK_WORDS:
            current.append(p)
            size += n
            fresh += n
            continue
        # a paragraph too long for one chunk is split by words
        words = p.split()
        while words:
            take, words = words[:CHUNK_WORDS - size], words[CHUNK_WORDS - size:]
            current.append(" ".join(take))
            size += len(take)
            fresh += len(take)
            if words:
                flush()
    if fresh:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_source(path: Path) -> list[Chunk]:
    front, body = parse_source(path)
    texts = [(heading, text) for heading, paragraphs in _sections(front["title"], body)
             for text in _pack(paragraphs)]
    return [Chunk(id=f"{path.stem}-{i}", text=text, title=front["title"], section=heading,
                  source_url=front["source_url"], topic=front["topic"], fetched_at=front["fetched_at"])
            for i, (heading, text) in enumerate(texts)]
