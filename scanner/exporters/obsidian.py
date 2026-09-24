"""Export accepted papers as Markdown notes with YAML frontmatter.

Point Obsidian (or any Markdown editor) at the output folder, and sync it
with git, Dropbox or Obsidian Sync to read your papers on other devices.
"""

import re
from pathlib import Path

import yaml

from scanner.db import Database
from scanner.pdfs import safe_name


def export_accepted(db: Database, out_dir: Path) -> int:
    """Write one note per accepted paper. Returns the number of notes written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    papers = db.get_papers(["accepted"])
    for paper in papers:
        note_path = out_dir / f"{safe_name(paper['paper_id'])}.md"
        note_path.write_text(render_note(paper), encoding="utf-8")
    return len(papers)


def render_note(paper: dict) -> str:
    frontmatter = {
        "title": paper["title"],
        "authors": paper["authors"],
        "published": paper["published_date"],
        "arxiv_id": paper["paper_id"],
        "url": paper["url"],
        "pdf": paper["pdf_url"],
        "relevance": round(paper["relevance"], 3) if paper["relevance"] is not None else None,
        "tags": [obsidian_tag(tag) for tag in paper["tags"]],
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=1000)
    return (
        f"---\n{header}---\n\n"
        f"# {paper['title']}\n\n"
        f"{', '.join(paper['authors'])}\n\n"
        f"[arXiv]({paper['url']}) | [PDF]({paper['pdf_url']})\n\n"
        f"## Abstract\n\n{paper['abstract']}\n"
    )


def obsidian_tag(name: str) -> str:
    """Obsidian tags cannot contain spaces: 'Motion and 3D' -> 'motion-and-3d'."""
    return re.sub(r"[^a-z0-9_/-]+", "-", name.lower()).strip("-")
