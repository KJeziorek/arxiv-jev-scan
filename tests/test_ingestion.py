"""arXiv OAI-PMH parsing and paging, with canned XML instead of the network."""

from datetime import date

import pytest

from scanner.sources.arxiv import ArxivSource, category_to_set, split_authors

PAGE_1 = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2512.04723</identifier><datestamp>2026-09-22</datestamp></header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2512.04723</id>
          <version version="v1"><date>Thu, 04 Dec 2025 12:06:48 GMT</date></version>
          <version version="v2"><date>Sun, 19 Jul 2026 20:13:00 GMT</date></version>
          <title>Event-based Optical Flow
            with Spiking Networks</title>
          <authors>Ada Lovelace (Analytical Engine Co., London), Alan Turing and Grace Hopper</authors>
          <categories>cs.CV cs.RO</categories>
          <abstract>  We estimate flow
            from events.  </abstract>
        </arXivRaw>
      </metadata>
    </record>
    <record>
      <header status="deleted"><identifier>oai:arXiv.org:2609.99999</identifier></header>
    </record>
    <resumptionToken cursor="0" completeListSize="2">token-page-2</resumptionToken>
  </ListRecords>
</OAI-PMH>"""

PAGE_2 = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <ListRecords>
    <record>
      <header><identifier>oai:arXiv.org:2609.00001</identifier></header>
      <metadata>
        <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
          <id>2609.00001</id>
          <version version="v1"><date>Mon, 21 Sep 2026 10:00:00 GMT</date></version>
          <title>DVS Dataset</title>
          <authors>Alan Turing</authors>
          <categories>cs.CV</categories>
          <abstract>A dataset.</abstract>
        </arXivRaw>
      </metadata>
    </record>
    <resumptionToken cursor="1" completeListSize="2"></resumptionToken>
  </ListRecords>
</OAI-PMH>"""

NO_RECORDS = """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <error code="noRecordsMatch">No records match</error>
</OAI-PMH>"""


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None):
        self.content = text.encode()
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = responses
        self.headers = {}
        self.requests: list[dict] = []

    def get(self, url, params, timeout):
        self.requests.append(params)
        return self.responses.pop(0)


def source_with(*responses: FakeResponse) -> ArxivSource:
    return ArxivSource(session=FakeSession(list(responses)), delay=0)


def test_parses_records_and_follows_resumption_token():
    source = source_with(FakeResponse(PAGE_1), FakeResponse(PAGE_2))

    papers = source.fetch_day(["cs.CV"], date(2026, 9, 22))

    assert [paper.paper_id for paper in papers] == ["2512.04723", "2609.00001"]
    first = papers[0]
    assert first.title == "Event-based Optical Flow with Spiking Networks"
    assert first.abstract == "We estimate flow from events."
    assert first.authors == ["Ada Lovelace", "Alan Turing", "Grace Hopper"]
    assert first.published == "2025-12-04"  # date of v1, not of the latest version
    assert first.categories == ["cs.CV", "cs.RO"]
    assert first.pdf_url == "https://arxiv.org/pdf/2512.04723"

    requests = source.session.requests
    assert requests[0]["set"] == "cs:cs:CV"
    assert requests[0]["from"] == requests[0]["until"] == "2026-09-22"
    assert requests[1] == {"verb": "ListRecords", "resumptionToken": "token-page-2"}


def test_cross_listed_paper_is_returned_once():
    source = source_with(FakeResponse(PAGE_2), FakeResponse(PAGE_2))
    papers = source.fetch_day(["cs.CV", "cs.RO"], date(2026, 9, 22))
    assert len(papers) == 1


def test_empty_day_returns_no_papers():
    assert source_with(FakeResponse(NO_RECORDS)).fetch_day(["cs.CV"], date(2026, 9, 20)) == []


def test_waits_and_retries_when_arxiv_is_busy(monkeypatch):
    waits = []
    monkeypatch.setattr("scanner.sources.arxiv.time.sleep", waits.append)
    source = source_with(FakeResponse("", 503, {"Retry-After": "7"}), FakeResponse(PAGE_2))

    papers = source.fetch_day(["cs.CV"], date(2026, 9, 22))

    assert len(papers) == 1
    assert 7 in waits


def test_other_oai_errors_are_raised():
    bad = NO_RECORDS.replace("noRecordsMatch", "badArgument")
    with pytest.raises(RuntimeError, match="badArgument"):
        source_with(FakeResponse(bad)).fetch_day(["cs.CV"], date(2026, 9, 22))


@pytest.mark.parametrize(
    "category, set_name",
    [
        ("cs.CV", "cs:cs:CV"),
        ("eess.IV", "eess:eess:IV"),
        ("math.OC", "math:math:OC"),
        ("astro-ph.GA", "physics:astro-ph:GA"),
        ("quant-ph", "physics:quant-ph"),
        ("cs", "cs:cs"),
        ("cs:cs:RO", "cs:cs:RO"),
    ],
)
def test_category_to_set(category, set_name):
    assert category_to_set(category) == set_name


def test_split_authors_keeps_names_with_and_inside():
    assert split_authors("Alexander Brandt and Andrea Sandoval") == ["Alexander Brandt", "Andrea Sandoval"]
