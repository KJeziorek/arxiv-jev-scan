"""Network graph of accepted/maybe papers, built with NetworkX and drawn
with Pyvis. Used by the Graph tab of the dashboard."""

from collections import defaultdict
from itertools import combinations

import networkx as nx
from pyvis.network import Network

# How papers can be connected. The dashboard lets the user pick one.
GRAPH_MODES = {
    "tags": "Tag hubs: papers link to their tags",
    "similar": "Similar papers: link papers with similar tags",
    "authors": "Tag hubs + shared authors",
}

PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]
NO_TAG_COLOR = "#999999"


def build_graph(papers: list[dict], mode: str = "tags", top_k: int = 5) -> nx.Graph:
    """Build the graph. Each paper dict needs paper_id, title, authors, tags
    ({tag: probability}), relevance and final_status."""
    graph = nx.Graph()
    colors = tag_colors(papers)

    for paper in papers:
        tags = list(paper["tags"])
        main_tag = tags[0] if tags else None
        graph.add_node(
            paper["paper_id"],
            label=paper["paper_id"],
            title=tooltip(paper),
            color=colors.get(main_tag, NO_TAG_COLOR),
            shape="dot" if paper["final_status"] == "accepted" else "diamond",
            size=10 + 10 * (paper["relevance"] or 0),
        )

    if mode in ("tags", "authors"):
        add_tag_hubs(graph, papers, colors)
    if mode == "similar":
        add_similarity_edges(graph, papers, top_k)
    if mode == "authors":
        add_author_edges(graph, papers)
    return graph


def add_tag_hubs(graph: nx.Graph, papers: list[dict], colors: dict[str, str]) -> None:
    """One node per tag; each paper links to its tags, thicker for likelier tags.

    (Pyvis draws an edge's `weight` as its line width.)
    """
    for paper in papers:
        for tag, probability in paper["tags"].items():
            hub = f"tag:{tag}"
            if hub not in graph:
                graph.add_node(hub, label=tag, title=tag, color=colors[tag], shape="box", size=25)
            graph.add_edge(paper["paper_id"], hub, weight=1 + 4 * probability, color=colors[tag])


def add_similarity_edges(graph: nx.Graph, papers: list[dict], top_k: int) -> None:
    """Link each paper to its `top_k` most similar papers by tag overlap.

    Keeping only the strongest links avoids one huge tangle when many papers
    share the same main tag.
    """
    for paper in papers:
        scores = []
        for other in papers:
            if other["paper_id"] != paper["paper_id"]:
                score = tag_similarity(paper["tags"], other["tags"])
                if score > 0:
                    scores.append((score, other["paper_id"]))
        scores.sort(reverse=True)
        for score, other_id in scores[:top_k]:
            graph.add_edge(paper["paper_id"], other_id, weight=1 + 4 * score, title=f"similarity {score:.2f}")


def add_author_edges(graph: nx.Graph, papers: list[dict]) -> None:
    """Dashed link between papers that share at least one author."""
    papers_by_author = defaultdict(list)
    for paper in papers:
        for author in paper["authors"]:
            papers_by_author[author].append(paper["paper_id"])

    for author, paper_ids in papers_by_author.items():
        for first, second in combinations(paper_ids, 2):
            if graph.has_edge(first, second):
                graph[first][second]["title"] += f", {author}"
            else:
                graph.add_edge(first, second, dashes=True, color="#555555", title=f"shared author: {author}")


def tag_similarity(first: dict[str, float], second: dict[str, float]) -> float:
    """Overlap of two tag profiles: 1.0 means identical, 0.0 nothing in common."""
    return sum(min(first[tag], second[tag]) for tag in first.keys() & second.keys())


def tag_colors(papers: list[dict]) -> dict[str, str]:
    tags = sorted({tag for paper in papers for tag in paper["tags"]})
    return {tag: PALETTE[index % len(PALETTE)] for index, tag in enumerate(tags)}


def tooltip(paper: dict) -> str:
    relevance = f"{paper['relevance']:.2f}" if paper["relevance"] is not None else "-"
    return (
        f"{paper['title']}\n"
        f"{paper['final_status']}, relevance {relevance}\n"
        f"tags: {', '.join(paper['tags']) or '-'}"
    )


def to_html(graph: nx.Graph, height: int = 700) -> str:
    """Render the graph as a standalone HTML page (Pyvis / vis-network)."""
    net = Network(height=f"{height}px", width="100%", cdn_resources="remote", select_menu=False)
    net.from_nx(graph)
    net.force_atlas_2based(gravity=-40, spring_length=120)
    return net.generate_html()
