import json
from datetime import date

import httpx
import pytest

from app.rag.chunk import parse_source
from app.rag.fetch import USER_AGENT, fetch_all

TODAY = date(2026, 10, 26)
URL = "https://www.example.gov/guide"

SENTENCE = ("Saving a little from every paycheck lets compound interest work for you over many years, "
            "and an emergency fund keeps a surprise bill from turning into credit card debt. ")


# shaped like the irs roth comparison chart: inline markup in a header, <p> and a list inside cells
TABLE = """<table><caption><h2>Retirement accounts</h2></caption>
<thead><tr><th><p>Issue</p></th><th><p><strong>&nbsp;</strong>Roth 401(k)&nbsp;</p></th><th><p>&nbsp;Roth IRA</p></th></tr></thead>
<tbody><tr><td><p>Contributions</p></td><td>Made with <strong>after-tax dollars</strong>.</td><td>Also after-tax.</td></tr>
<tr><td><p>Income limits</p></td><td>No income limitation.</td>
<td><p>Income limits:&nbsp;</p><ul><li>2024 – single $161,000</li><li><p>2023 – single $153,000</p></li></ul></td></tr>
</tbody></table>"""


# a gov-style page: nav and footer around one article with headings, paragraphs and a table
def page(title="Saving Basics", paragraphs=6, extra=""):
    # numbered so every paragraph is distinct: trafilatura drops repeated identical paragraphs
    body = "".join(f"<p>Paragraph {i}. {SENTENCE * 2}</p>" for i in range(paragraphs))
    return f"""<html><head><title>{title}</title></head><body>
<nav><ul><li><a href="/">Home</a></li><li><a href="/about">About</a></li><li><a href="/contact">Contact Menu</a></li></ul></nav>
<main><article><h1>{title}</h1><h2>Why it matters</h2>{body}
{TABLE}{extra}
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


# trafilatura's markdown tables drop such headers and lists, so each row becomes one labeled line
def test_table_rows_become_labeled_lines(tmp_path):
    kb = make_kb(tmp_path, entry())

    fetch_all(kb, client({URL: html(page())}), today=TODAY)

    lines = parse_source(kb / "sources" / "good.md")[1].splitlines()
    assert "Retirement accounts" in " ".join(lines)
    assert "Contributions — Roth 401(k): Made with after-tax dollars.; Roth IRA: Also after-tax." in lines
    assert ("Income limits — Roth 401(k): No income limitation.; "
            "Roth IRA: Income limits: 2024 – single $161,000, 2023 – single $153,000") in lines


# cfpb pages end every article with these; they'd be embedded as if they were guidance
def test_drops_site_boilerplate_lines(tmp_path):
    extra = ("<p>Read more</p><h2>Stay informed</h2>"
             "<p>Sign up for the latest financial tips and information right to your inbox.</p>"
             "<p>Page last modified @</p>")
    kb = make_kb(tmp_path, entry())

    fetch_all(kb, client({URL: html(page(extra=extra))}), today=TODAY)

    body = parse_source(kb / "sources" / "good.md")[1]
    assert "compound interest" in body
    for junk in ("Read more", "Stay informed", "Sign up for the latest", "Page last modified"):
        assert junk not in body


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


# one blank or xhtml-declared page must become a FAIL line, not a traceback that stops the run
@pytest.mark.parametrize("bad", [
    lambda: httpx.Response(200, content=b""),
    lambda: httpx.Response(200, text='<?xml version="1.0" encoding="UTF-8"?>' + page()),
], ids=["empty-body", "xml-declaration"])
def test_unparseable_page_reported_others_written(tmp_path, bad):
    kb = make_kb(tmp_path, entry("bad", url=URL + "/bad"), entry())

    failed = fetch_all(kb, client({URL + "/bad": bad, URL: html(page())}), today=TODAY)

    assert failed == ["bad"] or (kb / "sources" / "bad.md").exists()
    assert (kb / "sources" / "good.md").exists()


def flattened_lines(tmp_path, table):
    kb = make_kb(tmp_path, entry())
    fetch_all(kb, client({URL: html(page().replace(TABLE, table))}), today=TODAY)
    return parse_source(kb / "sources" / "good.md")[1].splitlines()


# irs limit tables label rows with <th>; that first row is data, not column headers
def test_row_headers_are_not_column_labels(tmp_path):
    lines = flattened_lines(tmp_path, "<table><tbody><tr><th>Age 50+</th><td>$8,000</td></tr>"
                                      "<tr><th>Under 50</th><td>$7,000</td></tr></tbody></table>")

    assert "Age 50+ — $8,000" in lines
    assert "Under 50 — $7,000" in lines


def test_blank_thead_keeps_first_data_row(tmp_path):
    lines = flattened_lines(tmp_path, "<table><thead><tr><th></th><th></th></tr></thead><tbody>"
                                      "<tr><td>Age 50+</td><td>$8,000</td></tr>"
                                      "<tr><td>Under 50</td><td>$7,000</td></tr></tbody></table>")

    assert "Age 50+ — $8,000" in lines
    assert "Under 50 — $7,000" in lines


# review on #8: a header row shorter than the data rows (colspan) must not drop the last values
def test_short_header_keeps_every_cell(tmp_path):
    lines = flattened_lines(tmp_path, "<table><thead><tr><th>Filing status</th><th>Full</th><th>Partial</th></tr>"
                                      "</thead><tbody><tr><td>Single</td><td>$146,000</td><td>$161,000</td>"
                                      "<td>$7,000</td><td>$8,000</td></tr></tbody></table>")

    assert "Single — Full: $146,000; Partial: $161,000; $7,000; $8,000" in lines


# with no header row, an empty cell is skipped instead of leaving "label — ; ; x"
def test_headerless_row_skips_empty_cells(tmp_path):
    lines = flattened_lines(tmp_path, "<table><tbody><tr><td>Age 50+</td><td></td><td>$8,000</td></tr>"
                                      "<tr><td>Under 50</td><td>$7,000</td><td></td></tr></tbody></table>")

    assert "Age 50+ — $8,000" in lines
    assert "Under 50 — $7,000" in lines


# any failure on one source, not just the expected two kinds, is reported and the run goes on
def test_unexpected_error_reported_others_written(tmp_path):
    broken = entry("broken", url=URL + "/broken")
    del broken["topic"]
    kb = make_kb(tmp_path, broken, entry())

    failed = fetch_all(kb, client({URL + "/broken": html(page()), URL: html(page())}), today=TODAY)

    assert failed == ["broken"]
    assert (kb / "sources" / "good.md").exists()
