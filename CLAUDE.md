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
  recipients.json          # Optional newsletter recipients/subject (pre-fill only)
  logo.png      # Company logo (user-supplied; PNG recommended)
  dashboard_template.html  # Jinja2 dashboard template rendered by generate_dashboard.py
  newsletter_template.html # Email-safe Jinja2 newsletter template
output/         # Generated deliverables (gitignored)
  dashboard.html  # Self-contained branded HTML dashboard
  report.json     # Headline KPI values
  metrics.json    # Numeric metrics bundle consumed by the newsletter
  newsletter.html # Branded email-ready newsletter (highlights/lowlights)
  newsletter.eml  # MIME newsletter (HTML body + dashboard.html attached)
sql/            # Raw SQL query files — edit here to change what data is pulled
  internal_collections.sql  # 12-month Internal Collections data with delinquency segmentation
  collections_queue_penetration.sql  # 13-month PTP penetration/fulfillment/recovery per queue (start dept x end dept)
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

### `tools/roll_rates_by_dpd.py`
Importable module:
- `get_roll_rates_data(engine)` — raw result of `sql/roll_rates_by_days_past_due.sql` (whole-book DPD migration; one row per month × start-DPD × end-DPD × movement_type).
- `get_roll_rates_frame(engine)` — tidy frame: derives the month from `reporting_month_end` (the raw `snap_date` is a prefixed string), drops join-miss nulls, and adds a recomputed `movement` class via `classify_movement()` (the query's own `movement_type` is unreliable — see workflow doc).

```bash
python tools/roll_rates_by_dpd.py --output .tmp/roll_rates.csv
```

### `tools/queue_penetration.py`
Importable module over `sql/collections_queue_penetration.sql` (PTP penetration & fulfillment):
- `get_queue_penetration_data(engine)` — raw rows, one per `(reporting_month, start dept, end dept)`.
- `get_queue_penetration_frame(engine)` — aggregates **across the end-of-month department** (sums the additive count/volume/financial columns) so each row is a queue's whole start-of-month population, and re-derives every rate metric — each with an in-month / in-next-month / combined timing split: `penetration_rate_pct`, `ptp_fulfillment_rate_pct` (kept rate), `recovery_yield_pct` (recovered / exposure), `collections_yield_pct` (net receipts / total due). Never average the pre-computed `*_pct` columns — re-derive from the count/volume sums (the SQL exposes `number_of_attempted_ptp_dos`/`number_of_settled_ptp_do` + in-month/next-month for exactly this). Note: `ptp_orders` is pre-aggregated per loan-month in the SQL to avoid a join fan-out that previously inflated fulfillment/recovery.
- `get_primary_queue_frame(engine)` — the headline `Internal Collections - in term` queue's monthly time series (one row per month) used by the dashboard tab.

```bash
python tools/queue_penetration.py --output .tmp/queue_penetration.csv
```

### `tools/out_of_term_collections.py`
Importable module over `sql/out_of_term_collections.sql` (recovery on the **out-of-term** book — `opening_in_term_flag = FALSE`):
- `get_out_of_term_data(engine)` — raw rows, one per `product × opening MPM band × closing MPM band × month`.
- `get_oot_band_frame(engine)` — aggregates across product and the **closing** MPM band so each row is a whole **opening**-band population per month, and re-derives the FTTC-based rate metrics: `fttc_collections` (= instalment + effort), `yield_pct` (= FTTC / opening_balance), `payer_rate_pct` (= active payers / accounts), `auto_pct`/`effort_pct` (share of FTTC collections). Adds a compact `band_label` (0–3 / 4–6 / 7–12 / 13–24 / 24+ mths).
- `get_oot_total_frame(engine)` — same derivations aggregated across all bands → the whole OOT-book monthly series (cards, hero, trend charts).

Never average the pre-computed `*_pct` columns — re-derive from the summed additive columns.
```bash
python tools/out_of_term_collections.py --output .tmp/out_of_term_collections.csv
```

### `tools/generate_dashboard.py`
Main entry point for the collections dashboard. Orchestrates: query → cards → chart data → (in-term) segment table → Jinja2 render → file output, for **four tabs**. **No CLI arguments** — the SQL self-manages the reporting window.

```bash
python tools/generate_dashboard.py
```

Outputs `output/dashboard.html` (open in any browser), `output/report.json`, and `output/metrics.json` (numeric metrics bundle for the newsletter, via `build_metrics_bundle`). The dashboard has four tabs:
- **In-Term Arrears** — 4 cards (Collection Rate, Effort Yield, Auto Collect %, Payer Rate, each with target chip + delta badge) and 4 charts scoped to the arrears population (MP1/MP2/MP3+), plus a segment-detail table covering **all** buckets/segments in the data (Current/MP0, Early Arrears, Deep Arrears, New Loan, Out of Term/MPM2 — built dynamically) with 7 months + MoM Δ + 3M Avg + YoY.
- **Out of Term Arrears** — recovery on the out-of-term book (`opening_in_term_flag = FALSE`). A hero yield banner, 4 cards (OOT Collected FTTC, OOT Book Yield, OOT Payers, OOT Accounts — each with 3m-avg sub + MoM badge), 4 charts (collections Rm vs 3m avg, book yield % vs 3m avg, payer rate % vs 3m avg, auto vs effort split), and a detail table grouped by opening MPM band (0–3 / 4–6 / 7–12 / 13–24 / 24+) plus a Total, with Collections/Yield %/Payer rate %/Effort % rows × 7 months + MoM Δ + 3M Avg + YoY. Collections = instalment + effort (FTTC); Yield = FTTC / opening_balance.
- **Roll Rates** (whole book, DPD migration) — 4 cards (Cure, Forward-roll, Default, Stable/Current), a 13-month pooled DPD transition matrix (row % + counts, heatmap), and 2 charts (movement composition, roll-rate trends). Movement classes are recomputed in Python because the query's `movement_type` is unreliable.
- **Queue Penetration** (PTP coverage, fulfillment & recovery, in-term queue) — 4 cards (Penetration rate, PTP fulfillment, Recovery yield, Collections yield, each with MoM badge), 6 charts (penetration % by PTP timing, fulfillment % by timing, recovery yield this-month vs next-month stacked, collections vs recovery yield, rand funnel exposure→promised→recovered, loans vs loans-with-PTP), and a 10-row monthly-detail table (+ MoM Δ / 3M Avg / YoY). Every PTP/recovery metric carries an in-month / next-month / combined timing split. Scoped to the `Internal Collections - in term` start-of-month queue.

See `workflows/dashboard_generation.md` for details.

> Not yet wired in: `sql/internal_collections2.sql` (Q08b effort-cost vs outsourcing analysis).

### `tools/insights.py`
Deterministic insight engine over `output/metrics.json`, used by the newsletter. `candidate_facts()` selects, scores and phrases notable month-on-month / vs-target movements; `split()` produces the global highlights/lowlights. No DB, no API — figures come only from the metrics bundle.

### `tools/generate_newsletter.py`
Generates the email-ready collections newsletter from `output/metrics.json` (run `generate_dashboard.py` first). Deterministic rules select & rank Highlights/Lowlights (exact figures), then Claude (`claude-sonnet-4-6`) polishes the prose — falling back to rule-based text if `ANTHROPIC_API_KEY` is absent or the call fails.

```bash
python tools/generate_newsletter.py
```

Writes `output/newsletter.html` and `output/newsletter.eml` (HTML body + `dashboard.html` attached). **File-only — never sends email.** Recipients/subject can be pre-filled via `assets/recipients.json` (used only to populate the `.eml`). See `workflows/newsletter_generation.md`.

---

## Current Workflows

### `workflows/dashboard_generation.md`
SOP for the collections dashboard — read this first before running any dashboard-related tool. Covers: setup steps, date range usage, edge cases (missing logo, empty data, DB failure), and troubleshooting.

### `workflows/newsletter_generation.md`
SOP for the email newsletter — hybrid (deterministic facts + Claude prose) Highlights/Lowlights from `metrics.json`. File-only; documents `ANTHROPIC_API_KEY` setup, recipients config, and that nothing is auto-sent.

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
