import json
from datetime import date

import httpx

from app.rag.chunk import parse_source
from app.rag.fetch import USER_AGENT, fetch_all

TODAY = date(2026, 10, 26)
URL = "https://www.example.gov/guide"

SENTENCE = ("Saving a little from every paycheck lets compound interest work for you over many years, "
            "and an emergency fund keeps a surprise bill from turning into credit card debt. ")


# a gov-style page: nav and footer around one article with headings, paragraphs and a table
def page(title="Saving Basics", paragraphs=6):
    body = "".join(f"<p>{SENTENCE * 2}</p>" for _ in range(paragraphs))
    return f"""<html><head><title>{title}</title></head><body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/about">About</a></li><li><a href="/contact">Contact Menu</a></li></ul></nav>
<main><article><h1>{title}</h1><h2>Why it matters</h2>{body}
<table><thead><tr><th>Plan</th><th>Limit</th></tr></thead>
<tbody><tr><td>Roth IRA</td><td>$7,000</td></tr><tr><td>401(k)</td><td>$23,000</td></tr></tbody></table>
</article></main><footer>Footer links and copyright</footer></body></html>"""


def entry(slug="good", url=URL, **extra):
    return {"slug": slug, "url": url, "topic": "saving", "license": "public-domain", **extra}


def make_kb(tmp_path, *entries):
    (tmp_path / "sources.json").write_text(json.dumps(list(entries)), encoding="utf-8")
    return tmp_path


# routes maps url -> response; anything else is a 404. seen collects every request
def client(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        resp = routes.get(str(request.url))
        return resp() if callable(resp) else (resp or httpx.Response(404))
    return httpx.Client(transport=httpx.MockTransport(handler))


def html(text):
    return lambda: httpx.Response(200, html=text)


def test_writes_front_matter_and_article(tmp_path):
    kb = make_kb(tmp_path, entry())

    assert fetch_all(kb, client({URL: html(page())}), today=TODAY) == []

    front, body = parse_source(kb / "sources" / "good.md")
    assert front == {"title": "Saving Basics", "source_url": URL, "topic": "saving", "fetched_at": "2026-10-26"}
    assert "compound interest" in body
    assert "Contact Menu" not in body
    assert "Footer links" not in body


def test_table_survives(tmp_path):
    kb = make_kb(tmp_path, entry())

    fetch_all(kb, client({URL: html(page())}), today=TODAY)

    _, body = parse_source(kb / "sources" / "good.md")
    rows = [line for line in body.splitlines() if line.startswith("|")]
    assert any("Roth IRA" in r and "$7,000" in r for r in rows)


def test_title_override_and_fallback(tmp_path):
    kb = make_kb(tmp_path, entry("named", title="Override Title"), entry("plain", url=URL + "/2"))

    fetch_all(kb, client({URL: html(page()), URL + "/2": html(page("From The Page"))}), today=TODAY)

    assert parse_source(kb / "sources" / "named.md")[0]["title"] == "Override Title"
    assert parse_source(kb / "sources" / "plain.md")[0]["title"] == "From The Page"


def test_title_with_colon_and_unicode_round_trips(tmp_path):
    title = "Retirement topics: IRA “limits”"
    kb = make_kb(tmp_path, entry())

    fetch_all(kb, client({URL: html(page(title))}), today=TODAY)

    assert parse_source(kb / "sources" / "good.md")[0]["title"] == title


def test_follows_redirect(tmp_path):
    kb = make_kb(tmp_path, entry())
    routes = {URL: lambda: httpx.Response(301, headers={"Location": URL + "/"}), URL + "/": html(page())}

    assert fetch_all(kb, client(routes), today=TODAY) == []

    assert parse_source(kb / "sources" / "good.md")[0]["source_url"] == URL


def test_failures_reported_others_written(tmp_path):
    kb = make_kb(tmp_path, entry("missing", url=URL + "/404"), entry("short", url=URL + "/short"), entry())
    routes = {URL + "/short": html(page(paragraphs=1).replace(SENTENCE * 2, SENTENCE)), URL: html(page())}

    failed = fetch_all(kb, client(routes), today=TODAY)

    assert failed == ["missing", "short"]
    assert sorted(p.name for p in (kb / "sources").glob("*.md")) == ["good.md"]


def test_skips_existing_unless_refresh(tmp_path):
    kb = make_kb(tmp_path, entry())
    existing = kb / "sources" / "good.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("old", encoding="utf-8")
    seen = []

    fetch_all(kb, client({URL: html(page())}, seen), today=TODAY)
    assert existing.read_text(encoding="utf-8") == "old"
    assert seen == []

    fetch_all(kb, client({URL: html(page())}, seen), refresh=True, today=TODAY)
    assert parse_source(existing)[0]["title"] == "Saving Basics"


def test_copyrighted_goes_to_local(tmp_path):
    kb = make_kb(tmp_path, entry(license="copyrighted"))

    fetch_all(kb, client({URL: html(page())}), today=TODAY)

    assert (kb / "sources" / "local" / "good.md").exists()
    assert not (kb / "sources" / "good.md").exists()


def test_sends_user_agent(tmp_path):
    kb = make_kb(tmp_path, entry())
    seen = []

    fetch_all(kb, client({URL: html(page())}, seen), today=TODAY)

    assert [r.headers["user-agent"] for r in seen] == [USER_AGENT]
