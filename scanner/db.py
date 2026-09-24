"""SQLite storage, the source of truth: every paper we have seen, every Jev
answer and every scan run (so an interrupted scan can resume)."""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from scanner.models import Paper, StageResult

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id          TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    title             TEXT NOT NULL,
    authors           TEXT NOT NULL,     -- JSON list
    abstract          TEXT NOT NULL,
    published_date    TEXT,              -- first version, YYYY-MM-DD
    categories        TEXT,              -- JSON list
    url               TEXT,
    pdf_url           TEXT,
    first_seen_at     TEXT NOT NULL,
    final_status      TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | maybe | rejected
    relevance         REAL,              -- probability from the first noul stage
    tags              TEXT,              -- JSON {tag: probability} from choice stages
    pdf_path          TEXT,
    pdf_downloaded_at TEXT
);

CREATE TABLE IF NOT EXISTS stage_results (
    paper_id      TEXT NOT NULL REFERENCES papers(paper_id),
    stage_name    TEXT NOT NULL,
    question_type TEXT NOT NULL,         -- noul | choice
    result        TEXT NOT NULL,         -- JSON answer
    decision      TEXT NOT NULL,         -- accept | maybe | reject
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    model         TEXT,
    ran_at        TEXT NOT NULL,
    PRIMARY KEY (paper_id, stage_name)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    source       TEXT NOT NULL,
    query_params TEXT NOT NULL,          -- JSON: categories, since, until
    checkpoint   TEXT,                   -- last fully processed day
    status       TEXT NOT NULL           -- running | completed | interrupted
);

CREATE INDEX IF NOT EXISTS papers_by_status ON papers(final_status);
"""

STATUSES = ("pending", "accepted", "maybe", "rejected")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- papers ---------------------------------------------------------

    def add_new_papers(self, papers: list[Paper]) -> list[Paper]:
        """Store papers we have never seen and return only those.

        This is the dedup gate: a paper that is already in the database is
        ignored here, so it is never sent to Jev a second time.
        """
        new = []
        with self.conn:
            for paper in papers:
                cursor = self.conn.execute(
                    """INSERT OR IGNORE INTO papers
                       (paper_id, source, title, authors, abstract, published_date,
                        categories, url, pdf_url, first_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        paper.paper_id, paper.source, paper.title, json.dumps(paper.authors),
                        paper.abstract, paper.published, json.dumps(paper.categories),
                        paper.url, paper.pdf_url, now(),
                    ),
                )
                if cursor.rowcount == 1:
                    new.append(paper)
        return new

    def pending_papers(self) -> list[Paper]:
        """Papers stored but not classified yet (e.g. after a crash or API error)."""
        rows = self.conn.execute(
            "SELECT * FROM papers WHERE final_status = 'pending' ORDER BY paper_id"
        )
        return [
            Paper(
                paper_id=row["paper_id"],
                source=row["source"],
                title=row["title"],
                authors=json.loads(row["authors"]),
                abstract=row["abstract"],
                published=row["published_date"],
                categories=json.loads(row["categories"]),
                url=row["url"],
                pdf_url=row["pdf_url"],
            )
            for row in rows
        ]

    def save_classification(
        self,
        paper_id: str,
        results: list[StageResult],
        status: str,
        relevance: float | None,
        tags: dict[str, float],
    ) -> None:
        """Store every stage result of one paper and its final status together."""
        with self.conn:
            for result in results:
                self.conn.execute(
                    """INSERT OR REPLACE INTO stage_results
                       (paper_id, stage_name, question_type, result, decision,
                        input_tokens, output_tokens, cost_usd, model, ran_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        paper_id, result.stage_name, result.question_type,
                        json.dumps(result.result), result.decision, result.input_tokens,
                        result.output_tokens, result.cost_usd, result.model, now(),
                    ),
                )
            self.conn.execute(
                "UPDATE papers SET final_status = ?, relevance = ?, tags = ? WHERE paper_id = ?",
                (status, relevance, json.dumps(tags), paper_id),
            )

    def get_papers(self, statuses: list[str] | None = None) -> list[dict]:
        """Papers as dicts (JSON columns decoded), newest first."""
        query = "SELECT * FROM papers"
        params: list[str] = []
        if statuses:
            placeholders = ", ".join(["?"] * len(statuses))
            query += f" WHERE final_status IN ({placeholders})"
            params = list(statuses)
        query += " ORDER BY published_date DESC, paper_id DESC"
        return [_decode_paper(row) for row in self.conn.execute(query, params)]

    def get_stage_results(self, paper_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM stage_results WHERE paper_id = ? ORDER BY rowid", (paper_id,)
        )
        return [dict(row) | {"result": json.loads(row["result"])} for row in rows]

    def mark_downloaded(self, paper_id: str, pdf_path: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE papers SET pdf_path = ?, pdf_downloaded_at = ? WHERE paper_id = ?",
                (pdf_path, now(), paper_id),
            )

    def papers_missing_pdf(self, statuses: list[str]) -> list[dict]:
        return [paper for paper in self.get_papers(statuses) if not paper["pdf_path"]]

    def totals(self) -> dict:
        """Paper counts per status plus total token usage and cost."""
        totals = {status: 0 for status in STATUSES}
        for row in self.conn.execute(
            "SELECT final_status, COUNT(*) AS n FROM papers GROUP BY final_status"
        ):
            totals[row["final_status"]] = row["n"]
        totals["papers"] = sum(totals.values())

        usage = self.conn.execute(
            """SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                      COALESCE(SUM(cost_usd), 0), COUNT(*)
               FROM stage_results"""
        ).fetchone()
        totals["input_tokens"], totals["output_tokens"], totals["cost_usd"], totals["jev_calls"] = usage
        return totals

    # --- scan runs --------------------------------------------------------

    def start_run(self, source: str, categories: list[str], since: date, until: date) -> int:
        params = {
            "categories": sorted(categories),
            "since": since.isoformat(),
            "until": until.isoformat(),
        }
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO scan_runs (started_at, source, query_params, status)
                   VALUES (?, ?, ?, 'running')""",
                (now(), source, json.dumps(params)),
            )
        return cursor.lastrowid

    def find_unfinished_run(self, source: str, categories: list[str], since: date) -> dict | None:
        """The newest scan with the same source, categories and start date that
        did not complete, so it can be resumed from its checkpoint."""
        rows = self.conn.execute(
            """SELECT * FROM scan_runs
               WHERE source = ? AND status != 'completed' ORDER BY run_id DESC""",
            (source,),
        )
        for row in rows:
            params = json.loads(row["query_params"])
            if params["categories"] == sorted(categories) and params["since"] == since.isoformat():
                return dict(row)
        return None

    def set_run_status(self, run_id: int, status: str) -> None:
        finished_at = None if status == "running" else now()
        with self.conn:
            self.conn.execute(
                "UPDATE scan_runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, finished_at, run_id),
            )

    def save_checkpoint(self, run_id: int, day: date) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scan_runs SET checkpoint = ? WHERE run_id = ?", (day.isoformat(), run_id)
            )

    def last_scanned_day(self, source: str, categories: list[str]) -> date | None:
        """Latest day fully covered by a completed scan of these categories."""
        rows = self.conn.execute(
            """SELECT query_params, checkpoint FROM scan_runs
               WHERE source = ? AND status = 'completed' AND checkpoint IS NOT NULL""",
            (source,),
        )
        days = [
            date.fromisoformat(row["checkpoint"])
            for row in rows
            if json.loads(row["query_params"])["categories"] == sorted(categories)
        ]
        return max(days, default=None)

    def get_runs(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT ?", (limit,))
        return [dict(row) | {"query_params": json.loads(row["query_params"])} for row in rows]


def _decode_paper(row: sqlite3.Row) -> dict:
    paper = dict(row)
    paper["authors"] = json.loads(paper["authors"])
    paper["categories"] = json.loads(paper["categories"] or "[]")
    paper["tags"] = json.loads(paper["tags"] or "{}")
    return paper
