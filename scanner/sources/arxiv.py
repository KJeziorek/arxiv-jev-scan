"""arXiv source based on OAI-PMH, arXiv's official interface for harvesting
metadata in bulk (https://info.arxiv.org/help/oa/index.html).

We use OAI-PMH instead of the search API (export.arxiv.org/api/query) because
it supports date ranges and per-category sets well, and because the search
API has been rejecting most automated date-range queries with HTTP 406.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import date
from email.utils import parsedate_to_datetime

import requests

from scanner import USER_AGENT
from scanner.models import Paper
from scanner.sources.base import PaperSource

log = logging.getLogger(__name__)

OAI_URL = "https://oaipmh.arxiv.org/oai"
NAMESPACES = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "raw": "http://arxiv.org/OAI/arXivRaw/",
}

# arXiv archives that live under the "physics" group in OAI set names.
PHYSICS_ARCHIVES = {
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph", "hep-th",
    "math-ph", "nlin", "nucl-ex", "nucl-th", "physics", "quant-ph",
}


def category_to_set(category: str) -> str:
    """Turn an arXiv category into its OAI set name.

    cs.CV -> cs:cs:CV, eess.IV -> eess:eess:IV, astro-ph.GA -> physics:astro-ph:GA
    """
    if ":" in category:  # already a set name
        return category
    archive, _, subject = category.partition(".")
    group = "physics" if archive in PHYSICS_ARCHIVES else archive
    parts = [group, archive, subject] if subject else [group, archive]
    return ":".join(parts)


class ArxivSource(PaperSource):
    name = "arxiv"

    def __init__(self, session: requests.Session | None = None, delay: float = 3.0, max_retries: int = 5):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.delay = delay  # arXiv asks for no more than one request every 3 seconds
        self.max_retries = max_retries

    def fetch_day(self, categories: list[str], day: date) -> list[Paper]:
        papers: dict[str, Paper] = {}
        for category in categories:
            for paper in self._harvest(category_to_set(category), day):
                papers[paper.paper_id] = paper  # a cross-listed paper shows up in several sets
        log.info("arXiv %s: %d records in %s", day, len(papers), ", ".join(categories))
        return list(papers.values())

    def _harvest(self, set_name: str, day: date) -> Iterator[Paper]:
        params = {
            "verb": "ListRecords",
            "metadataPrefix": "arXivRaw",  # has the date of the first version
            "set": set_name,
            "from": day.isoformat(),
            "until": day.isoformat(),
        }
        while True:
            root = self._get(params)

            error = root.find("oai:error", NAMESPACES)
            if error is not None:
                if error.get("code") == "noRecordsMatch":
                    return
                raise RuntimeError(f"arXiv OAI error {error.get('code')}: {error.text}")

            for record in root.iterfind(".//oai:record", NAMESPACES):
                paper = parse_record(record)
                if paper:
                    yield paper

            # Long lists come in pages; the resumption token points to the next one.
            token = root.findtext(".//oai:resumptionToken", default="", namespaces=NAMESPACES).strip()
            if not token:
                return
            params = {"verb": "ListRecords", "resumptionToken": token}

    def _get(self, params: dict) -> ET.Element:
        for _ in range(self.max_retries):
            time.sleep(self.delay)
            response = self.session.get(OAI_URL, params=params, timeout=60)
            if response.status_code in (429, 503):
                wait = _retry_after(response, default=30)
                log.info("arXiv asked us to slow down, waiting %d s", wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return ET.fromstring(response.content)
        raise RuntimeError("arXiv OAI kept refusing requests, try again later")


def parse_record(record: ET.Element) -> Paper | None:
    """Turn one OAI record into a Paper. Deleted records return None."""
    header = record.find("oai:header", NAMESPACES)
    if header is not None and header.get("status") == "deleted":
        return None
    meta = record.find("oai:metadata/raw:arXivRaw", NAMESPACES)
    if meta is None:
        return None

    def text(tag: str) -> str:
        return clean(meta.findtext(f"raw:{tag}", default="", namespaces=NAMESPACES))

    paper_id = text("id")
    return Paper(
        paper_id=paper_id,
        source="arxiv",
        title=text("title"),
        authors=split_authors(text("authors")),
        abstract=text("abstract"),
        published=first_version_date(meta),
        categories=text("categories").split(),
        url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=f"https://arxiv.org/pdf/{paper_id}",
    )


def first_version_date(meta: ET.Element) -> str:
    """Date of v1 as YYYY-MM-DD (the arXivRaw dates look like 'Thu, 04 Dec 2025 12:06:48 GMT')."""
    version = meta.find("raw:version[@version='v1']", NAMESPACES)
    if version is None:
        version = meta.find("raw:version", NAMESPACES)
    raw_date = version.findtext("raw:date", namespaces=NAMESPACES)
    return parsedate_to_datetime(raw_date).date().isoformat()


def split_authors(authors: str) -> list[str]:
    """'A. Smith (MIT), B. Jones and C. Lee' -> ['A. Smith', 'B. Jones', 'C. Lee']"""
    authors = re.sub(r"\([^)]*\)", "", authors)  # drop affiliations in parentheses
    names = re.split(r",|\band\b", authors)
    return [name.strip() for name in names if name.strip()]


def clean(text: str) -> str:
    """Collapse the line breaks and indentation arXiv keeps in titles and abstracts."""
    return " ".join(text.split())


def _retry_after(response: requests.Response, default: int) -> int:
    value = response.headers.get("Retry-After", "")
    return int(value) if value.isdigit() else default
