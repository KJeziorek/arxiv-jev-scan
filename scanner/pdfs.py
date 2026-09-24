"""PDF downloads, used by both the dashboard and the CLI."""

import logging
import time
from pathlib import Path

import requests

from scanner import USER_AGENT
from scanner.db import Database
from scanner.models import Progress

log = logging.getLogger(__name__)


def safe_name(paper_id: str) -> str:
    """File name for a paper. Old arXiv ids contain a slash, e.g. 'hep-th/9901001'."""
    return paper_id.replace("/", "_")


def pdf_filename(paper_id: str) -> str:
    return safe_name(paper_id) + ".pdf"


def download_pdf(
    db: Database,
    paper: dict,
    pdf_dir: Path,
    session: requests.Session | None = None,
    max_retries: int = 4,
) -> Path:
    """Download one paper's PDF (unless it is already on disk) and record it."""
    path = Path(pdf_dir) / pdf_filename(paper["paper_id"])
    if not path.exists():
        session = session or requests.Session()
        content = _fetch_pdf(session, paper["pdf_url"], max_retries)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")  # never leave half a PDF behind
        partial.write_bytes(content)
        partial.replace(path)
    db.mark_downloaded(paper["paper_id"], str(path))
    return path


def download_all(
    db: Database,
    pdf_dir: Path,
    statuses: list[str],
    delay: float = 3.0,
    progress: Progress | None = None,
) -> int:
    """Download every PDF we do not have yet for papers with these statuses.

    Papers that already have a PDF are skipped, so running it again only
    fetches what is missing. Waits `delay` seconds between downloads to be
    polite to arXiv. Returns the number of PDFs downloaded.
    """
    papers = db.papers_missing_pdf(statuses)
    session = requests.Session()
    downloaded = 0
    for index, paper in enumerate(papers):
        if progress:
            progress(index / len(papers), f"Downloading {paper['paper_id']} ({index + 1}/{len(papers)})")
        try:
            download_pdf(db, paper, pdf_dir, session)
            downloaded += 1
        except (requests.RequestException, ValueError) as error:
            log.warning("Could not download %s: %s", paper["paper_id"], error)
        time.sleep(delay)
    if progress:
        progress(1.0, f"Downloaded {downloaded} PDFs")
    return downloaded


def _fetch_pdf(session: requests.Session, url: str, max_retries: int) -> bytes:
    for attempt in range(max_retries):
        response = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
        if response.status_code in (429, 503):
            wait = 10 * 2**attempt  # 10, 20, 40, 80 seconds
            log.info("arXiv is rate limiting PDF downloads, waiting %d s", wait)
            time.sleep(wait)
            continue
        response.raise_for_status()
        if "pdf" not in response.headers.get("Content-Type", ""):
            raise ValueError(f"{url} did not return a PDF")
        return response.content
    raise requests.HTTPError(f"{url}: still rate limited after {max_retries} attempts")
