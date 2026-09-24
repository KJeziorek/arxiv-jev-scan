from datetime import date

from scanner.db import SCHEMA_VERSION, Database
from scanner.models import StageResult
from tests.conftest import make_paper


def test_schema_is_created(db):
    tables = {row[0] for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"papers", "stage_results", "scan_runs"} <= tables
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_opening_an_existing_database_keeps_data(tmp_path):
    first = Database(tmp_path / "papers.db")
    first.add_new_papers([make_paper("2609.00001")])
    first.close()

    second = Database(tmp_path / "papers.db")
    assert len(second.get_papers()) == 1
    second.close()


def test_add_new_papers_only_returns_unseen_papers(db):
    assert len(db.add_new_papers([make_paper("2609.00001"), make_paper("2609.00002")])) == 2

    again = db.add_new_papers([make_paper("2609.00001"), make_paper("2609.00003")])

    assert [paper.paper_id for paper in again] == ["2609.00003"]
    assert len(db.get_papers()) == 3


def test_duplicates_inside_one_batch_are_stored_once(db):
    new = db.add_new_papers([make_paper("2609.00001"), make_paper("2609.00001")])
    assert len(new) == 1


def test_classified_papers_are_no_longer_pending(db):
    db.add_new_papers([make_paper("2609.00001"), make_paper("2609.00002")])
    result = StageResult("relevance", "noul", {"probability": 0.9}, "accept", 100, 10, 0.0000042, "jev")

    db.save_classification("2609.00001", [result], "accepted", 0.9, {"Robotics": 0.7})

    assert [paper.paper_id for paper in db.pending_papers()] == ["2609.00002"]
    paper = db.get_papers(["accepted"])[0]
    assert paper["tags"] == {"Robotics": 0.7}
    assert paper["relevance"] == 0.9
    assert db.get_stage_results("2609.00001")[0]["result"] == {"probability": 0.9}


def test_totals_count_statuses_tokens_and_cost(db):
    db.add_new_papers([make_paper("2609.00001"), make_paper("2609.00002")])
    result = StageResult("relevance", "noul", {"probability": 0.1}, "reject", 300, 20, 0.0000126, "jev")
    db.save_classification("2609.00001", [result], "rejected", 0.1, {})

    totals = db.totals()

    assert totals["papers"] == 2
    assert totals["rejected"] == 1
    assert totals["pending"] == 1
    assert totals["input_tokens"] == 300
    assert totals["cost_usd"] == 0.0000126


def test_mark_downloaded_removes_paper_from_missing_list(db):
    db.add_new_papers([make_paper("2609.00001")])
    db.conn.execute("UPDATE papers SET final_status = 'accepted'")
    assert len(db.papers_missing_pdf(["accepted"])) == 1

    db.mark_downloaded("2609.00001", "data/pdfs/2609.00001.pdf")

    assert db.papers_missing_pdf(["accepted"]) == []


def test_unfinished_run_is_found_by_categories_and_start_date(db):
    run_id = db.start_run("arxiv", ["cs.RO", "cs.CV"], date(2026, 9, 1), date(2026, 9, 10))
    db.save_checkpoint(run_id, date(2026, 9, 4))
    db.set_run_status(run_id, "interrupted")

    run = db.find_unfinished_run("arxiv", ["cs.CV", "cs.RO"], date(2026, 9, 1))

    assert run["run_id"] == run_id
    assert run["checkpoint"] == "2026-09-04"
    assert db.find_unfinished_run("arxiv", ["cs.CV"], date(2026, 9, 1)) is None


def test_last_scanned_day_uses_completed_runs_only(db):
    done = db.start_run("arxiv", ["cs.CV"], date(2026, 9, 1), date(2026, 9, 5))
    db.save_checkpoint(done, date(2026, 9, 5))
    db.set_run_status(done, "completed")
    broken = db.start_run("arxiv", ["cs.CV"], date(2026, 9, 6), date(2026, 9, 20))
    db.save_checkpoint(broken, date(2026, 9, 10))
    db.set_run_status(broken, "interrupted")

    assert db.last_scanned_day("arxiv", ["cs.CV"]) == date(2026, 9, 5)
    assert db.last_scanned_day("arxiv", ["cs.RO"]) is None
