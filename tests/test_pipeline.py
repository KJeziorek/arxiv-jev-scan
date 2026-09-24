from datetime import date

import httpx2
import pytest
from typesafe_sdk import TypeSafeAuthenticationError

from scanner.pipeline import Pipeline, decide, paper_status, tags_of
from tests.conftest import DOMAIN, RELEVANCE, FakeJev, FakeSource, make_paper

DAY1, DAY2, DAY3 = date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)


def test_rejected_paper_never_reaches_stage_two(profile, db):
    jev = FakeJev({"Social event detection": 0.05})
    pipeline = Pipeline(profile, db, jev, FakeSource({}))

    results = pipeline.classify(make_paper("1", "Social event detection"))

    assert [r.decision for r in results] == ["reject"]
    assert jev.calls == [("relevance", "Social event detection")]
    assert paper_status(results) == "rejected"


def test_accepted_paper_runs_all_stages(profile, db):
    jev = FakeJev({"Event camera SLAM": 0.95})
    results = Pipeline(profile, db, jev, FakeSource({})).classify(make_paper("1", "Event camera SLAM"))

    assert [r.stage_name for r in results] == ["relevance", "domain"]
    assert paper_status(results) == "accepted"


def test_maybe_paper_continues_and_is_marked_maybe(profile, db):
    jev = FakeJev({"Spiking networks": 0.5})
    results = Pipeline(profile, db, jev, FakeSource({})).classify(make_paper("1", "Spiking networks"))

    assert [r.decision for r in results] == ["maybe", "accept"]
    assert paper_status(results) == "maybe"


@pytest.mark.parametrize(
    "probability, expected",
    [(0.95, "accept"), (0.8, "accept"), (0.79, "maybe"), (0.21, "maybe"), (0.2, "reject"), (0.0, "reject")],
)
def test_noul_thresholds(probability, expected):
    assert decide(RELEVANCE, {"probability": probability}) == expected


def test_uncertain_choice_is_flagged_but_does_not_reject():
    assert decide(DOMAIN, {"confidence": 0.3}) == "maybe"
    assert decide(DOMAIN, {"confidence": 0.7}) == "accept"


def test_tags_include_likely_options_sorted(profile, db):
    jev = FakeJev({"Paper": 0.9}, domain={"Vision": 0.35, "Robotics": 0.6, "Other": 0.05})
    results = Pipeline(profile, db, jev, FakeSource({})).classify(make_paper("1", "Paper"))

    assert tags_of(profile.stages, results) == {"Robotics": 0.6, "Vision": 0.35}


def test_scan_stores_results_and_never_reclassifies(profile, db):
    source = FakeSource({
        DAY1: [make_paper("1", "Event camera SLAM"), make_paper("2", "Stock market events")],
        DAY2: [make_paper("3", "DVS optical flow")],
    })
    jev = FakeJev({"Event camera SLAM": 0.95, "Stock market events": 0.01, "DVS optical flow": 0.9})
    pipeline = Pipeline(profile, db, jev, source)

    summary = pipeline.scan(DAY1, DAY2)

    assert (summary.new, summary.accepted, summary.rejected) == (3, 2, 1)
    assert len(jev.calls) == 5  # 2 stages for each accepted paper, 1 for the rejected one
    assert db.get_papers(["accepted"])[0]["tags"]

    # Scanning the same days again finds nothing new and makes no Jev calls.
    again = Pipeline(profile, db, jev, source).scan(DAY1, DAY2)
    assert again.new == 0
    assert len(jev.calls) == 5


def test_interrupted_scan_resumes_after_last_finished_day(profile, db):
    source = FakeSource(
        {DAY1: [make_paper("1", "A")], DAY2: [make_paper("2", "B")], DAY3: [make_paper("3", "C")]},
        fail_on=DAY2,
    )
    jev = FakeJev({"A": 0.9, "B": 0.9, "C": 0.9})

    with pytest.raises(ConnectionError):
        Pipeline(profile, db, jev, source).scan(DAY1, DAY3)
    assert db.get_runs()[0]["status"] == "interrupted"
    assert db.get_runs()[0]["checkpoint"] == DAY1.isoformat()

    source.requested_days.clear()
    summary = Pipeline(profile, db, jev, source).scan(DAY1, DAY3)

    assert source.requested_days == [DAY2, DAY3]  # day 1 is not fetched again
    assert summary.new == 2
    assert db.get_runs()[0]["status"] == "completed"
    assert len(db.get_runs()) == 1  # the same run was continued


def test_progress_only_moves_forward_and_ends_at_one(profile, db):
    source = FakeSource({DAY1: [make_paper(str(n), f"P{n}") for n in range(4)], DAY3: [make_paper("9", "P9")]})
    jev = FakeJev({"P0": 0.9, "P1": 0.1, "P2": 0.5, "P3": 0.9, "P9": 0.9})
    updates = []

    Pipeline(profile, db, jev, source).scan(DAY1, DAY3, progress=lambda fraction, message: updates.append(fraction))

    assert updates == sorted(updates)
    assert updates[-1] == 1.0


def test_revisions_of_old_papers_are_skipped(profile, db):
    source = FakeSource({DAY1: [make_paper("1", "New", "2026-08-30"), make_paper("2", "Old", "2019-05-01")]})
    jev = FakeJev({"New": 0.9, "Old": 0.9})

    summary = Pipeline(profile, db, jev, source).scan(DAY1, DAY1)

    assert summary.new == 1
    assert db.get_papers()[0]["title"] == "New"


def test_max_papers_stops_early(profile, db):
    source = FakeSource({DAY1: [make_paper(str(n), f"P{n}") for n in range(5)], DAY2: [make_paper("9", "P9")]})
    jev = FakeJev({f"P{n}": 0.1 for n in range(10)})

    summary = Pipeline(profile, db, jev, source).scan(DAY1, DAY2, max_papers=3)

    assert summary.new == 3
    assert source.requested_days == [DAY1]


class FlakyJev(FakeJev):
    """Fails once for one title, like a network error that outlasted the retries."""

    def __init__(self, relevance_by_title, flaky_title):
        super().__init__(relevance_by_title)
        self.flaky_title = flaky_title

    def ask(self, stage, state):
        if state["title"] == self.flaky_title:
            self.flaky_title = None
            raise ConnectionError("timeout")
        return super().ask(stage, state)


def test_failed_paper_stays_pending_and_is_retried_next_scan(profile, db):
    source = FakeSource({DAY1: [make_paper("1", "A"), make_paper("2", "B")]})
    jev = FlakyJev({"A": 0.9, "B": 0.9}, flaky_title="B")

    first = Pipeline(profile, db, jev, source).scan(DAY1, DAY1)
    assert (first.accepted, first.failed) == (1, 1)
    assert [paper.title for paper in db.pending_papers()] == ["B"]

    second = Pipeline(profile, db, jev, FakeSource({})).scan(DAY2, DAY2)
    assert second.accepted == 1
    assert db.pending_papers() == []


def test_bad_api_key_stops_the_scan(profile, db):
    class BadKeyJev:
        def ask(self, stage, state):
            raise TypeSafeAuthenticationError(401, {"detail": "Invalid API key"}, httpx2.Headers())

    source = FakeSource({DAY1: [make_paper("1", "A")]})
    with pytest.raises(TypeSafeAuthenticationError):
        Pipeline(profile, db, BadKeyJev(), source).scan(DAY1, DAY1)
    assert db.get_runs()[0]["status"] == "interrupted"
