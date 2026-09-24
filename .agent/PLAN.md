# arxiv-jev-scan — Implementation Plan

## Context

The user wants a public, open-source Python tool that scans arXiv (extensible to
other sources later) for papers matching a natural-language topic description,
using TypeSafe's Jev model to classify relevance and then further categorize
accepted papers by domain (robotics/vision/multimodal/etc.). Results need a
dashboard (scan controls, token/cost tracking, browsable tables, PDF downloads,
a network graph of related papers), a way to never reprocess an already-seen
abstract, free/shareable storage (not locked to one machine), and a way to run
on a schedule with notifications.

This plan is the output of an extensive brainstorming session with the user
(architectural path), where every major decision below was proposed with
trade-offs and explicitly chosen by the user — not assumed. This file replaces
an earlier auto-generated version of `PLAN.md` that ignored the specified Jev
model, guessed at implementation choices without asking, and lacked per-stage
cost tracking, dedup detail, or the graph view.

This plan is self-contained and intended to be handed to a fresh Claude
session (or any other implementer) with no memory of the brainstorming
conversation that produced it.

## Key decisions (from brainstorming)

- **Jev integration**: sequential per-stage calls (one Jev API call per pipeline
  stage per paper), not a combined fan-out call — chosen so stages can later be
  conditioned on prior results, despite the extra cost/latency vs. a single
  combined call.
- **Storage**: local SQLite (`data/papers.db`) as source of truth, plus a
  Markdown/Obsidian export of accepted papers for human browsing and
  cross-device sync via git/Dropbox/Obsidian Sync — no cloud DB/account needed.
- **Pipeline**: config-driven ordered stages (`config/stages.yaml`), not an
  embedded visual workflow engine (n8n/Langflow) — avoids running a second
  service just to sequence 2-3 classification stages.
- **GUI**: Streamlit — runs as a local web server opened in the regular browser
  (not a native window), best fit for dataframe-heavy dashboards, free
  Community Cloud hosting available for anyone forking the repo.
- **Scheduling**: OS-level cron / Task Scheduler running `scanner/cli.py`,
  independent of whether the dashboard is open, plus a desktop notification on
  new relevant papers. Same command handles both large one-off backfills and
  small daily incremental runs via `--since last-run`.
- **Confidence handling**: two thresholds per relevance stage — accept / maybe
  (needs review) / reject — so uncertain papers are visible, not silently
  dropped or wrongly accepted.
- **Sources**: arXiv only for v1, behind a small `PaperSource` interface so a
  second source is a new file, not a rewrite.
- **PDF storage**: local folder (`data/pdfs/<arxiv_id>.pdf`), downloaded
  on-demand from the dashboard, tracked in the DB (not auto-downloaded for
  every accepted paper). A bulk "download all" option is also provided (GUI
  button and CLI command) for grabbing every accepted paper's PDF in one go.
- **Graph view**: NetworkX (build graph: nodes = accepted/maybe papers, edges =
  shared taxonomy tags) rendered interactively via Pyvis, embedded in the
  Streamlit page. Both libraries are free/MIT, no extra service.
- **Future work (not built now)**: a second-pass model reading full PDF text to
  build richer paper-to-paper connections (beyond shared tags). Noted so it
  isn't forgotten, explicitly out of scope for v1.
- **Scale**: must handle large one-off backfills (tens of thousands of papers,
  needs resumable checkpointing) as well as small fast daily incremental scans.

## Repository layout

```
arxiv-jev-scan/
├── README.md
├── requirements.txt
├── .env.example              # JEV_API_KEY, JEV_COST_PER_1K_TOKENS (user must fill in — no public Jev pricing page found)
├── config/
│   └── stages.yaml           # ordered list of pipeline stages (see below)
├── scanner/
│   ├── __init__.py
│   ├── sources/
│   │   ├── base.py           # PaperSource interface: fetch(query, date_range) -> Iterator[Paper]
│   │   └── arxiv.py          # arXiv API client (category, date range, pagination, rate-limit backoff)
│   ├── db.py                 # SQLite schema + access layer (papers, stage_results, scan_runs)
│   ├── jev_client.py         # Jev HTTP API wrapper: auth, retry/backoff on 429, token+cost accounting
│   ├── pipeline.py           # sequential per-stage runner, dedup gate, early-stop on reject, checkpointing
│   ├── exporters/
│   │   └── obsidian.py       # Markdown + YAML frontmatter export of accepted papers
│   ├── notify.py             # desktop notification (plyer) when new relevant papers found
│   └── cli.py                 # `python -m scanner.cli scan --category cs.CV --since last-run|<date>`
├── app.py                     # Streamlit dashboard (entrypoint: `streamlit run app.py`)
├── graph_view.py               # NetworkX + Pyvis network graph component, imported by app.py
├── data/                       # gitignored: papers.db, pdfs/, obsidian/
└── tests/
    ├── test_ingestion.py
    ├── test_pipeline.py
    ├── test_jev_client.py
    └── test_db.py
```

## Data model (`scanner/db.py`)

```sql
papers (
  arxiv_id TEXT PRIMARY KEY,
  title, authors, abstract, published_date,
  first_seen_at, pdf_path NULL, pdf_downloaded_at NULL,
  final_status TEXT   -- 'pending' | 'accepted' | 'maybe' | 'rejected'
)

stage_results (
  paper_id TEXT REFERENCES papers(arxiv_id),
  stage_name TEXT,
  question_type TEXT,   -- 'noul' | 'choice'
  result JSON,          -- noul: {probability}; choice: {selected, probabilities, confidence}
  decision TEXT,        -- 'accept' | 'maybe' | 'reject'
  input_tokens, output_tokens, cost_usd,
  ran_at,
  PRIMARY KEY (paper_id, stage_name)
)

scan_runs (
  run_id, started_at, finished_at, source, query_params JSON,
  checkpoint TEXT,       -- last processed page/cursor, for resumable backfills
  status                 -- 'running' | 'completed' | 'interrupted'
)
```

**Dedup**: before any Jev call, filter out `arxiv_id`s already present in
`papers`. A paper is never reprocessed once it exists in the DB, regardless of
scan size or how many times a scan is re-run.

## Pipeline execution (`scanner/pipeline.py`)

- Stages are defined in `config/stages.yaml`, in order. Each stage: `name`,
  `question_type` (`noul`|`choice`), `instructions`, `criteria`, and for noul
  stages `accept_threshold`/`reject_threshold` (the band between them = "maybe").
- Example stage 1 (mandatory relevance filter): noul question — "Is this paper
  about event cameras (not events in the general/incident sense)?"
- Example stage 2 (taxonomy): choice question over categories (Robotics,
  Vision, Multimodal, Hardware, ...), only run for papers that passed stage 1.
- For each pending paper: run stage 1; if rejected, stop immediately (no
  further Jev spend, `final_status='rejected'`); if accepted or maybe, continue
  to later stages. Every stage's result is recorded in `stage_results`
  regardless of outcome, so the dashboard can show the full per-paper history.
- Large backfills persist `scan_runs.checkpoint` so an interrupted scan resumes
  without re-fetching already-processed pages. Concurrency: bounded worker pool
  (~10–20 concurrent Jev calls) with exponential backoff on HTTP 429.

## Jev integration (`scanner/jev_client.py`)

- `POST https://api.typesafe.ai/v1/systemone`, `model: "jev-latest"`,
  `Authorization: Bearer <JEV_API_KEY>`.
- One `noul` or `choice` question per call (per the sequential-stage decision),
  `state` = `{title, abstract}`.
- Every response's `usage.input_tokens`/`output_tokens` recorded per call;
  cost computed from a user-supplied `$/1k tokens` rate in `.env`, since no
  public Jev pricing page was found — this is called out explicitly in the
  README/`.env.example` rather than guessed.

## GUI (`app.py`, Streamlit)

- **Scan controls**: source category, date range, topic description (feeds
  stage 1's `instructions`), "Run Scan" button, live progress bar.
- **Metrics row**: total scanned, accepted, maybe, tokens used, cumulative
  cost.
- **Tabs**: Relevant | Maybe | Rejected | Downloaded | Graph.
  - Relevant/Maybe tables: one row per paper with colored stage-decision
    badges + taxonomy tag chip, a 📄 icon if already downloaded, and an
    expandable detail view showing per-stage confidence (bar per Choice
    category).
  - Downloaded tab: papers with a non-null `pdf_path` — path, size, download
    date, cross-linked to the paper's row in Relevant/Maybe.
  - Graph tab: the NetworkX/Pyvis network (nodes = accepted/maybe papers,
    edges = shared taxonomy tags, node color = dominant category), with
    sidebar filters for date range and minimum confidence.
- PDF download button (per row) fetches from arXiv on click, saves to
  `data/pdfs/<arxiv_id>.pdf`, updates `pdf_path`/`pdf_downloaded_at`.
- **Download all** button (in the Relevant/Maybe/Downloaded tabs): queues a
  background download of every accepted (optionally: accepted+maybe) paper
  that doesn't already have a `pdf_path`, with a progress bar and basic
  rate-limiting/backoff against arXiv's servers so a bulk pull of hundreds of
  PDFs doesn't get the client rate-limited or blocked. Skips papers already
  downloaded (checked via `pdf_path`), so re-clicking is always incremental.

## Scheduling (`scanner/cli.py`)

- `python -m scanner.cli scan --category cs.CV --since last-run` (or an
  explicit date for backfills) — headless, reuses `pipeline.py` directly.
- User registers this once via cron (Linux/Mac) or Task Scheduler (Windows);
  README documents both.
- On completion, if new accepted papers were found, fires a desktop
  notification (via `plyer`) summarizing the count.
- `python -m scanner.cli download-all [--status accepted|accepted,maybe]` —
  the same bulk-download logic used by the GUI's "Download all" button,
  available headlessly (e.g. to run overnight for a large backfill's worth of
  PDFs without the dashboard open).

## Export (`scanner/exporters/obsidian.py`)

- Each accepted paper → `data/obsidian/<arxiv_id>.md` with YAML frontmatter
  (title, authors, tags, confidence, arXiv link) plus the abstract as body —
  syncable to other devices via git/Dropbox/Obsidian Sync, satisfying the
  "free, shareable, not just local" requirement without any cloud account.

## Files to create

All files listed under **Repository layout** above are new (greenfield
project).

## Verification

1. `scanner/db.py` unit tests: dedup (re-inserting a seen `arxiv_id` never
   triggers reprocessing), schema creation/migrations.
2. `scanner/jev_client.py` tests: mocked HTTP responses for noul/choice,
   retry/backoff on 429, correct token/cost accounting.
3. `scanner/pipeline.py` tests: a paper rejected at stage 1 never triggers a
   stage 2 Jev call (verifies the cost-saving early-stop behavior); maybe-band
   threshold logic.
4. Manual end-to-end run: `python -m scanner.cli scan --category cs.CV --since
   2026-09-01 --max 20` against the real arXiv + Jev APIs with a small limit;
   confirm `papers.db`/`stage_results` populate correctly, then
   `streamlit run app.py` and check all five tabs render, a PDF downloads
   successfully, and the Obsidian export file appears in `data/obsidian/`.
5. Register the cron/Task Scheduler entry per the README and confirm it runs
   `cli.py` on schedule and fires a desktop notification when new papers are
   found.
6. Run `python -m scanner.cli download-all` (and the GUI's "Download all"
   button) against a set of accepted papers with no `pdf_path` yet; confirm
   every PDF lands in `data/pdfs/`, `pdf_path`/`pdf_downloaded_at` are set, and
   re-running is a no-op for papers already downloaded.
