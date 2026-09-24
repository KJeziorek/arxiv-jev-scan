import yaml

from graph_view import build_graph, tag_similarity, to_html
from scanner.exporters.obsidian import export_accepted, obsidian_tag
from scanner.models import StageResult
from tests.conftest import make_paper


def graph_paper(paper_id, tags, authors=("Ada Lovelace",)):
    return {
        "paper_id": paper_id,
        "title": f"Paper {paper_id}",
        "authors": list(authors),
        "tags": tags,
        "relevance": 0.9,
        "final_status": "accepted",
    }


PAPERS = [
    graph_paper("1", {"Robotics": 0.8, "Vision": 0.2}, ["Ada Lovelace"]),
    graph_paper("2", {"Robotics": 0.6, "Vision": 0.4}, ["Alan Turing"]),
    graph_paper("3", {"Imaging": 1.0}, ["Ada Lovelace"]),
]


def test_tag_hub_mode_links_papers_to_tags_only():
    graph = build_graph(PAPERS, mode="tags")

    assert {"tag:Robotics", "tag:Vision", "tag:Imaging"} <= set(graph.nodes)
    assert graph.number_of_edges() == 5  # one edge per (paper, tag)
    assert not graph.has_edge("1", "3")


def test_similar_mode_links_papers_with_overlapping_tags():
    graph = build_graph(PAPERS, mode="similar", top_k=1)

    assert graph.has_edge("1", "2")
    assert not graph.has_edge("1", "3")  # nothing in common
    assert not any(node.startswith("tag:") for node in graph.nodes)


def test_author_mode_adds_shared_author_edges():
    graph = build_graph(PAPERS, mode="authors")
    assert graph.has_edge("1", "3")
    assert "Ada Lovelace" in graph["1"]["3"]["title"]


def test_tag_similarity():
    assert tag_similarity({"A": 1.0}, {"A": 1.0}) == 1.0
    assert tag_similarity({"A": 0.8, "B": 0.2}, {"A": 0.6, "B": 0.4}) == 0.8
    assert tag_similarity({"A": 1.0}, {"B": 1.0}) == 0


def test_graph_renders_to_html():
    html = to_html(build_graph(PAPERS, mode="tags"))
    assert "vis-network" in html and "Paper 1" in html


def test_obsidian_export_writes_frontmatter(db, tmp_path):
    db.add_new_papers([make_paper("2609.00001", "Event camera SLAM"), make_paper("2609.00002", "Rejected")])
    result = StageResult("relevance", "noul", {"probability": 0.97}, "accept", 100, 10, 0.0, "jev")
    db.save_classification("2609.00001", [result], "accepted", 0.97, {"Motion and 3D": 0.9})

    count = export_accepted(db, tmp_path / "notes")

    assert count == 1
    note = (tmp_path / "notes" / "2609.00001.md").read_text()
    frontmatter = yaml.safe_load(note.split("---")[1])
    assert frontmatter["title"] == "Event camera SLAM"
    assert frontmatter["tags"] == ["motion-and-3d"]
    assert frontmatter["url"] == "https://arxiv.org/abs/2609.00001"
    assert "## Abstract" in note


def test_obsidian_tag():
    assert obsidian_tag("Hardware and neuromorphic computing") == "hardware-and-neuromorphic-computing"
