"""Shared test helpers: fake papers, a fake Jev and a fake paper source."""

from datetime import date
from pathlib import Path

import pytest

from scanner.config import Profile, Stage
from scanner.db import Database
from scanner.jev_client import JevAnswer
from scanner.models import Paper
from scanner.sources.base import PaperSource


def make_paper(paper_id: str, title: str = "A paper", published: str = "2026-09-20") -> Paper:
    return Paper(
        paper_id=paper_id,
        source="fake",
        title=title,
        authors=["Ada Lovelace", "Alan Turing"],
        abstract=f"Abstract of {title}.",
        published=published,
        categories=["cs.CV"],
        url=f"https://arxiv.org/abs/{paper_id}",
        pdf_url=f"https://arxiv.org/pdf/{paper_id}",
    )


RELEVANCE = Stage(
    name="relevance",
    type="noul",
    instructions="Is this paper about event cameras?",
    accept_threshold=0.8,
    reject_threshold=0.2,
)
DOMAIN = Stage(
    name="domain",
    type="choice",
    instructions="Main application area?",
    criteria={"Robotics": None, "Vision": None, "Other": None},
    tag_threshold=0.25,
    min_confidence=0.5,
)


@pytest.fixture
def profile(tmp_path: Path) -> Profile:
    return Profile(topic="Event cameras", data_dir=tmp_path, categories=["cs.CV"], stages=[RELEVANCE, DOMAIN])


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "papers.db")
    yield database
    database.close()


class FakeJev:
    """Answers from a table: relevance probability per paper title."""

    def __init__(self, relevance_by_title: dict[str, float], domain=None):
        self.relevance_by_title = relevance_by_title
        self.domain = domain or {"Robotics": 0.7, "Vision": 0.3, "Other": 0.0}
        self.calls: list[tuple[str, str]] = []  # (stage name, paper title)

    def ask(self, stage: Stage, state: dict) -> JevAnswer:
        self.calls.append((stage.name, state["title"]))
        if stage.type == "noul":
            result = {"probability": self.relevance_by_title[state["title"]]}
        else:
            selected = max(self.domain, key=self.domain.get)
            result = {"selected": selected, "probabilities": self.domain, "confidence": 0.6}
        return JevAnswer(result=result, input_tokens=100, output_tokens=10, cost_usd=0.0000042, model="jev-test")


class FakeSource(PaperSource):
    name = "fake"

    def __init__(self, papers_by_day: dict[date, list[Paper]], fail_on: date | None = None):
        self.papers_by_day = papers_by_day
        self.fail_on = fail_on
        self.requested_days: list[date] = []

    def fetch_day(self, categories: list[str], day: date) -> list[Paper]:
        self.requested_days.append(day)
        if day == self.fail_on:
            self.fail_on = None  # fail only once
            raise ConnectionError("network down")
        return list(self.papers_by_day.get(day, []))
