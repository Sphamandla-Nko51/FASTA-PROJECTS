# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases

**Layer 2: Agents (The Decision-Maker)**
- This is your role — intelligent coordination between intent and execution
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, ask clarifying questions when needed
- Example: to pull data from a database, read `workflows/database_connection.md`, identify inputs, then execute `tools/connect_to_database.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` — API calls, data transformations, file operations, database queries
- Credentials and API keys live in `.env` only

**Why this matters:** Multi-step AI-only pipelines compound errors fast (90% accuracy per step → 59% after five steps). Offloading execution to deterministic scripts keeps accuracy high.

## How to Operate

**1. Check for existing tools first**
Before writing anything new, scan `tools/` for what you need. Only create new scripts when nothing exists for that task.

**2. Running tools**
```bash
python tools/<script>.py          # run a tool
python tools/<script>.py --help   # check expected arguments
```
If a script has external dependencies not yet installed: `pip install <package>` or check if a `requirements.txt` exists.

**3. Learn and adapt when things fail**
- Read the full error and trace
- Fix the script and retest — if it makes paid API calls, confirm with the user before re-running
- Update the workflow with what you learned (rate limits, timing quirks, better endpoints)

**4. Keep workflows current**
Update workflows when you find better methods or encounter new constraints. Do not create or overwrite workflow files without asking unless explicitly told to — these are the system's long-term instructions.

## Writing New Tools

When creating a new script in `tools/`:
- Accept inputs via `argparse` (not hardcoded values)
- Load credentials from `.env` via `python-dotenv`
- Print structured output (JSON preferred) so the agent can parse results
- Keep each script focused on one task

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach

## File Structure

```
assets/         # Brand config, HTML template, and logo
  brand.json    # Company name, hex colors, font, logo path
  targets.json  # Per-metric targets for the summary cards (percentages)
  logo.png      # Company logo (user-supplied; PNG recommended)
  dashboard_template.html  # Jinja2 template rendered by generate_dashboard.py
output/         # Generated deliverables (gitignored)
  dashboard.html  # Self-contained branded HTML dashboard
  report.json     # Raw KPI data for downstream use
sql/            # Raw SQL query files — edit here to change what data is pulled
  internal_collections.sql  # 12-month Internal Collections data with delinquency segmentation
.tmp/           # Temporary files (scraped data, intermediate exports) — disposable
tools/          # Python scripts for deterministic execution
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and credentials (never store secrets elsewhere)
```

---

## Database

PostgreSQL credentials live in `.env` as `DATABASE_URL` (AWS RDS, read-only). This is the only required env var for the dashboard workflow. Never mutate data — all queries are SELECT-only.

---

## Current Tools

### `tools/collection_by_segment.py`
Importable module:
- `get_internal_collections_data(engine)` — raw result of `sql/internal_collections.sql` (both result sets: `A_SEGMENT` + `B_TOTAL`).
- `get_segment_frame(engine)` — the dashboard's consumption frame: keeps `A_SEGMENT`, aggregates across products by `(transaction_month, delinquency_segment)`, and derives the ratio metrics (`collection_yield_pct`, `effort_yield_pct`, `auto_pct`, `effort_pct`, `payer_rate_pct`, `arrears_move`).

Returns a pandas DataFrame. SQL is loaded from `sql/internal_collections.sql` — edit that file to change query logic or the reporting window. No date parameters; the query manages its own window (last full month + 13-month lookback) via `current_date`. Can also be run standalone to dump a CSV:
```bash
python tools/collection_by_segment.py --output .tmp/internal_collections.csv
```

### `tools/out_of_term_by_segment.py`
Importable module:
- `get_out_of_term_data(engine)` — raw result of `sql/out_of_term_collections.sql` (one row per `product × prev_mpm_band × mpm_band × month`).
- `get_oot_frame(engine)` — whole-book monthly frame for the OOT tab: aggregates across the band grain per `transaction_month`, derives ratio metrics (`yield_pct`, `payer_rate_pct`, `auto_pct`, `effort_pct`) and 3-month rolling averages (`collections_rm_3m`, `yield_pct_3m`, `payer_rate_pct_3m`).

```bash
python tools/out_of_term_by_segment.py --output .tmp/oot.csv
```

### `tools/generate_dashboard.py`
Main entry point for the collections dashboard. Orchestrates: query → cards → chart data → (in-term) segment table → Jinja2 render → file output, for **two tabs**. **No CLI arguments** — the SQL self-manages the reporting window.

```bash
python tools/generate_dashboard.py
```

Outputs `output/dashboard.html` (open in any browser) and `output/report.json`. The dashboard has two tabs:
- **In-Term Arrears** — 4 cards (Collection Rate, Effort Yield, Auto Collect %, Payer Rate, each with target chip + delta badge) and 4 charts scoped to the arrears population (MP1/MP2/MP3+), plus a segment-detail table covering **all** buckets/segments in the data (Current/MP0, Early Arrears, Deep Arrears, New Loan, Out of Term/MPM2 — built dynamically) with 7 months + MoM Δ + 3M Avg + YoY.
- **Out-of-Term Recoveries** (whole OOT book) — 4 cards (Collected, Book Yield, Payers, Accounts) and 4 charts (collections vs 3m avg, book yield vs 3m avg, payer rate vs 3m avg, auto-vs-effort split).

See `workflows/dashboard_generation.md` for details.

> Not yet wired in: `sql/internal_collections2.sql` (Q08b effort-cost vs outsourcing analysis), and the richer OOT columns (activation lag, payer lifecycle, MPM-band cohorts, provision coverage).

---

## Current Workflows

### `workflows/dashboard_generation.md`
SOP for the collections dashboard — read this first before running any dashboard-related tool. Covers: setup steps, date range usage, edge cases (missing logo, empty data, DB failure), and troubleshooting.

---

## Brand Configuration

Edit `assets/brand.json` to update branding:
```json
{
  "company": "Company Name",
  "primary": "#rrggbb",
  "secondary": "#rrggbb",
  "accent": "#rrggbb",
  "font": "Inter",
  "logo": "./assets/logo.png"
}
```
Colors **must be hex** (`#rrggbb`). Drop the logo file at `assets/logo.png`. Re-run `generate_dashboard.py` after any brand change.

## Card Targets

Edit `assets/targets.json` to set the target chip + delta badge on the summary cards (values are **percentages**, not fractions):
```json
{ "collection_rate": 27.7, "effort_yield": 10.7, "auto_collect": 74.1, "payer_rate": 42.0 }
```
A card's badge is green when the latest-month value meets/beats its target, red otherwise. If the file is missing, cards fall back to a 0.0% target.
