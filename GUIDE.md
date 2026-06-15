# Collections Dashboard — End-to-End Guide

This guide walks through everything required to produce the Fasta Collections Performance Dashboard, from environment setup through to interpreting the output. Follow it sequentially the first time; subsequent runs only require Step 5.

---

## Table of Contents
1. [What the Dashboard Produces](#1-what-the-dashboard-produces)
2. [Prerequisites](#2-prerequisites)
3. [First-Time Setup](#3-first-time-setup)
4. [Configure the Database Connection](#4-configure-the-database-connection)
5. [Configure Brand Assets](#5-configure-brand-assets)
6. [Run the Dashboard](#6-run-the-dashboard)
7. [Understanding the Output](#7-understanding-the-output)
8. [Customising the Date Range](#8-customising-the-date-range)
9. [Updating Brand Assets](#9-updating-brand-assets)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What the Dashboard Produces

Running the workflow generates two files inside `output/`:

| File | Description |
|---|---|
| `output/dashboard.html` | Self-contained branded HTML dashboard — open in any browser, no server needed |
| `output/report.json` | Raw KPI data (totals, collection rates, aging summary) for downstream use |

The dashboard contains five analytical sections:
- **Summary KPIs** — Total collected, collection rate, outstanding balance, active loans
- **Collections by Department** — Monthly net receipts per collection team (grouped bar chart)
- **Collection Rate by Department** — Recovery efficiency per team over time (line chart)
- **Portfolio Trend** — Opening balance vs net receipts over the full period (dual-line chart)
- **Aging Buckets** — Outstanding balance and loan count by time in collections (bar + doughnut)

Each chart section is followed by a data-driven **Intelligence Panel** that automatically surfaces key findings, outliers, and trends from the live data.

---

## 2. Prerequisites

| Requirement | Minimum version | Check |
|---|---|---|
| Python | 3.10+ | `python --version` |
| pip | any | `pip --version` |
| Network access | — | Must be able to reach AWS RDS on port 5432 |
| Database credentials | — | Supplied separately (see Step 4) |

No local database installation is required — the dashboard queries a remote PostgreSQL instance.

---

## 3. First-Time Setup

### 3.1 Navigate to the project directory
```bash
cd /path/to/claude_projects
```

### 3.2 Install Python dependencies
```bash
pip install -r requirements.txt
```

This installs:

| Package | Purpose |
|---|---|
| `pandas` | Data manipulation and SQL result handling |
| `sqlalchemy` | Database connection abstraction |
| `psycopg2-binary` | PostgreSQL driver |
| `Jinja2` | HTML template rendering |
| `python-dotenv` | Loading credentials from `.env` |
| `requests` | Available for future tools |

You only need to run this once (or again after `requirements.txt` changes).

---

## 4. Configure the Database Connection

All credentials live exclusively in `.env` — never hardcode them in scripts.

### 4.1 Create or edit `.env` in the project root
```
DATABASE_URL=postgresql://username:password@host:5432/database?sslmode=require
```

The full `DATABASE_URL` for this project targets the Fasta production RDS instance. Obtain it from the team if you do not already have it.

### 4.2 Verify the connection
```bash
python tools/collection_by_segment.py --help
```

If credentials are correct, the script will print its argument help. If the connection fails, you will see an error — see [Troubleshooting](#10-troubleshooting).

> **Security:** `.env` is gitignored. Never commit credentials to version control.

---

## 5. Configure Brand Assets

Brand configuration controls the dashboard's visual identity. On first run, the dashboard will render with the default placeholder config.

### 5.1 Edit `assets/brand.json`
```json
{
  "company": "Fasta",
  "primary": "#01a9e6",
  "secondary": "#69be21",
  "accent":   "#6abd45",
  "font":     "Inter",
  "logo":     "./assets/logo.png"
}
```

| Field | Description |
|---|---|
| `company` | Appears in the dashboard header and footer |
| `primary` | Header background, KPI value text — must be a hex colour (`#rrggbb`) |
| `secondary` | Accent borders, chart highlights, insight panel labels |
| `accent` | Section labels, secondary chart series |
| `font` | Any [Google Font](https://fonts.google.com/) name (e.g. `Inter`, `Poppins`, `DM Sans`) |
| `logo` | Path to the logo file — relative to the project root |

> **Important:** All colour values must be hex format (`#rrggbb`). The generator normalises `rgb()` values automatically, but hex is preferred.

### 5.2 Add the company logo
Place your logo file at:
```
assets/logo.png
```
Recommended specs: PNG with transparent background, minimum 300px wide. The dashboard inverts the logo to white automatically for the dark header. If no logo is found, the header renders without one — no error is thrown.

---

## 6. Run the Dashboard

```bash
python tools/generate_dashboard.py
```

The query automatically covers the last 12 months relative to `current_date` — no date arguments needed. You will see:

```
Connecting to database...
Fetching internal collections data (last 12 months)...
Computing KPIs...
Building chart data...
Generating insights...
Rendering dashboard...

Dashboard written to: /path/to/claude_projects/output/dashboard.html
Report JSON written to: /path/to/claude_projects/output/report.json
```

### Open the dashboard
```bash
open output/dashboard.html        # macOS
xdg-open output/dashboard.html   # Linux
start output/dashboard.html       # Windows
```

Or drag `output/dashboard.html` into any browser window. No internet connection is needed to view the file (Chart.js loads from CDN — charts require internet on first open, but data and layout render regardless).

---

## 7. Understanding the Output

### 7.1 Summary KPI Cards

| Card | Formula | What it means |
|---|---|---|
| Total Collected (12M) | `SUM(total_collected_in_month)` | Gross cash recovered over the last 12 months |
| Collection Yield | `SUM(collected) / SUM(total_due) × 100` | % of what was owed (arrears + instalment due) that was collected |
| Payer Rate | `SUM(payers) / SUM(cnt_loans) × 100` (latest month) | % of accounts that made any payment in the most recent month |
| Active Loans | `SUM(cnt_loans_in_month)` (latest month) | Loans in scope at the end of the most recent reporting month |

**Key metric: Collection Yield vs Collection Rate**
Collection Yield (`collected / total_due`) is a stricter measure than a simple collection rate. `total_due` = opening arrears + instalment due for the month, meaning the team is measured against the full obligation, not just what was billed.

### 7.2 Section 1 — Yield & Portfolio Mix

**Chart: Collection Yield by Delinquency Segment (%)**
Shows recovery efficiency per delinquency segment over 12 months. Each line is a segment (e.g. Current, 1–30 DPD, 31–60 DPD). Higher yield = more of what was owed was collected. Use this to identify which segments the team is recovering most efficiently from.

**Chart: Loan Count by Delinquency Segment (stacked bar)**
Shows how the portfolio is distributed across delinquency segments each month. A growing proportion of higher-DPD segments signals portfolio deterioration; shrinking higher-DPD segments indicate recovery.

**Intelligence panel highlights:**
- Best and worst yield segments, vs overall portfolio yield
- Whether yield is improving, stable, or declining
- Which segment delivers the highest return on collection effort

### 7.3 Section 2 — Payment Behaviour

**Chart: Payer Rate by Segment (%)**
What percentage of loans in each segment made at least one payment that month. Current accounts should show a high payer rate; 90+ DPD accounts will show very low rates. Trends here reveal whether delinquent accounts are being re-engaged.

**Chart: Receipt Mix — Instalment vs Effort**
Stacked bar showing total instalment receipts vs other receipts (effort-driven collections) each month. A rising effort share means the team is actively collecting beyond regular payments — a positive indicator of activity.

**Intelligence panel highlights:**
- Latest-month payer rate and best-performing segment
- Instalment vs effort receipt split across the 12-month period
- Interpretation of the effort share

### 7.4 Section 3 — Balance & Arrears

**Chart: Portfolio Trend — Total Due vs Collected**
Two lines: what the team was responsible for collecting (total due = arrears + instalment due) vs what was actually collected. A narrowing gap indicates improving recovery; a widening gap signals growing shortfall.

**Chart: Opening Arrears Trend**
Area line showing total opening arrears each month. Arrears trending up = accounts are accumulating unpaid balances faster than the team can recover. Arrears trending down = the team is making net progress against the backlog.

**Intelligence panel highlights:**
- Peak collection month and amount
- Whether total due is growing or contracting
- Whether arrears are improving or deteriorating

### 7.5 Delinquency Segments

The query filters accounts through `mv_delinquency_segments`, which classifies each loan by its days-past-due status:

| Segment | Meaning |
|---|---|
| Current | No arrears — paying on time |
| 1–30 DPD | 1 to 30 days past due |
| 31–60 DPD | 31 to 60 days past due |
| 61–90 DPD | 61 to 90 days past due |
| 91+ DPD | More than 90 days past due — highest risk |

### 7.6 `output/report.json`

Machine-readable export containing:
```json
{
  "period": "12 months to May 2026",
  "generated_at": "2026-06-12 09:00",
  "kpis": {
    "total_collected": "R208.5M",
    "collection_yield": "65.2%",
    "payer_rate": "74.1%",
    "active_loans": "12,345"
  }
}
```

Use this file to feed KPIs into slides, other tools, or reporting pipelines without re-querying the database.

---

## 8. Changing the Date Range

The date window is controlled by `sql/internal_collections.sql`, not by command-line arguments. The query uses:

```sql
with params as (select date_trunc('month', current_date) - interval '1 month' as reporting_month)
```

and looks back 12 months from that anchor. To change the window, edit that line directly in the SQL file. For example, to look back 6 months instead of 12, change the `12 months` filter in the final `WHERE` clause.

> No Python code changes are needed — only the SQL file.

---

## 9. Updating Brand Assets

### Change colours or font
1. Edit `assets/brand.json` with the new values
2. Re-run: `python tools/generate_dashboard.py`
3. Refresh the browser

### Replace the logo
1. Drop the new file at `assets/logo.png` (overwrite the old one)
2. Re-run: `python tools/generate_dashboard.py`

### Change the company name
Update the `"company"` field in `assets/brand.json` — it appears in the header title and footer.

---

## 10. Troubleshooting

### `Error: DATABASE_URL not set in .env`
The `.env` file is missing or the `DATABASE_URL` key is not set. Create or edit `.env` in the project root and add the full connection string.

### `connection refused` / `could not connect to server`
- Check network access to the RDS host (port 5432 must be reachable)
- Confirm the credentials in `.env` are correct
- Ensure `?sslmode=require` is included in the connection string if the server requires SSL

### `ModuleNotFoundError: No module named 'jinja2'` (or `psycopg2`, `sqlalchemy`, etc.)
Run `pip install -r requirements.txt` to install all dependencies.

### Charts appear blank after opening the dashboard
Chart.js loads from a CDN. Open browser developer tools (F12) → Console to check for network errors. If offline, the chart canvases will be empty but all data and layout are intact.

### `Warning: logo not found at ./assets/logo.png — skipping logo`
Place your logo PNG at `assets/logo.png`. The dashboard renders fine without it — this is informational only.

### Stale data in the dashboard
The dashboard always queries live from the database at run time. Re-run `python tools/generate_dashboard.py` to get the latest data. The old `output/dashboard.html` is overwritten.

### Intelligence panel shows unexpected department names
Department names come directly from the `departments` table in the database. If names appear truncated or unusual, they reflect how the department is registered in the system.

---

## Quick Reference

```bash
# First-time setup
pip install -r requirements.txt

# Run dashboard (last 12 months, auto-determined by SQL)
python tools/generate_dashboard.py

# Open the result
open output/dashboard.html

# Check raw KPIs
cat output/report.json

# Change the date window — edit the SQL directly
nano sql/internal_collections.sql
```

---

*Last updated: 2026-06-12 | Data source: redblade (AWS RDS) | Framework: WAT*
