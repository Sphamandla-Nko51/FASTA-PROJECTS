# Collections Dashboard Generation

## Objective
Generate a self-contained branded HTML dashboard (`output/dashboard.html`) from live PostgreSQL collections data, alongside a raw KPI JSON export (`output/report.json`). The dashboard reports **Internal Collections (in-term)** performance across the in-term book (opening segments New Loan · MP0 · MP1 · MP2 · MP3+).

## Required Inputs
| Input | Where | Notes |
|---|---|---|
| `DATABASE_URL` | `.env` | PostgreSQL connection string — already configured |
| `assets/brand.json` | Project root | Company name, colors, font, logo path |
| `assets/targets.json` | Project root | Per-metric targets shown on the summary cards |
| `assets/logo.png` | Project root | Optional — dashboard renders without it |

## Steps

### 1. Install dependencies (first run only)
```bash
pip install -r requirements.txt
```

### 2. Generate the dashboard
```bash
python tools/generate_dashboard.py
```
The query self-manages its reporting window: it reports the **last full month** (`DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'`) and pulls a 13-month lookback (e.g. May 2025 → May 2026). There are **no date arguments** — to change the window, edit `sql/internal_collections.sql`.

### 3. Open the dashboard
```bash
open output/dashboard.html     # macOS
xdg-open output/dashboard.html # Linux
```

## Outputs
| File | Description |
|---|---|
| `output/dashboard.html` | Self-contained branded dashboard — open in any browser, no server needed |
| `output/report.json` | Latest-month summary card values + period metadata |

## Dashboard Sections

The dashboard has **four tabs** (switcher at the top of the page): In-Term, Out of Term, Roll Rates, and Queue Penetration.

### Tab 1 — In-Term (cards + charts cover the in-term book: New Loan + MP0 + MP1 + MP2 + MP3+; only Out of Term/MPM2 excluded)
1. **Summary cards** (latest full month):
   - **Collection Rate** = `net_receipts / (opening_arrears + instalment_due)`
   - **Effort Yield** = `effort_collections / (opening_arrears + instalment_due)`
   - **Auto Collect %** = `instalment_collections / net_receipts`
   - **Payer Rate** = `payers / loan_count`
   - Each card shows a target chip (from `assets/targets.json`) and a delta-vs-target badge (green when meeting/beating target).
2. **Charts** (monthly series across the window, by opening segment):
   - Effort yield % by opening segment (line — New Loan/MP0/MP1/MP2/MP3+)
   - Collection rate % by opening segment (line — New Loan/MP0/MP1/MP2/MP3+)
   - Auto vs Effort composition — MP1 (% of collected, stacked bar: Auto/DebiCheck vs Effort/agent)
   - Net arrears movement (Rm) — all segments (line — closing arrears − opening arrears)
3. **Segment table** covering **every delinquency bucket/segment present in the data** (Current/MP0, Early Arrears/MP1·MP2, Deep Arrears/MP3+, New Loan, Out of Term/MPM2 — built dynamically, so new segments appear automatically). Three metric rows per segment — Collections (Rm), Yield %, Payer rate % — with the last 7 months plus MoM Δ (latest vs prior), 3M Avg (mean of last 3 months), and YoY (latest month vs same month prior year). The summary **cards** cover the whole in-term book (all buckets except Out of Term), and the **charts** plot New Loan/MP0/MP1/MP2/MP3+. **Note:** this is broader than the SQL handover's locked arrears-only KPI population (Early + Deep Arrears), so card values/targets read differently from the original arrears-only definition.

### Tab 2 — Out of Term (recovery on the out-of-term book)
Driven by `sql/out_of_term_collections.sql` via `tools/out_of_term_collections.py` (`get_oot_total_frame` for the whole-book monthly series; `get_oot_band_frame` for the by-opening-MPM-band detail). Population is the out-of-term book (`opening_in_term_flag = FALSE`, `opening_is_active IN (1,2,3,9999)`), stratified by **opening** months-past-maturity band (0–3 / 4–6 / 7–12 / 13–24 / 24+).

**Definitions (per the operational view):** Collections = instalment + effort (FTTC), **not** `net_receipts`; Yield = FTTC collections / opening_balance; Effort % = effort_collections / FTTC collections; Payer rate = active payers / accounts.

1. **Hero banner** — headline OOT book yield %, its 3-month average, and a flat/up/down MoM tag.
2. **Summary cards** (latest full month): **OOT Collected (FTTC)** (Rm, MoM Δ), **OOT Book Yield** (%, MoM Δ in pp), **OOT Payers** (count of active payers, MoM Δ), **OOT Accounts** (active OOT book size). Each card shows its 3-month average as the sub-line.
3. **Charts**: OOT collections (Rm, FTTC) vs 3m avg (bars + dashed avg line); OOT book yield % vs 3m avg (line); OOT payer rate % vs 3m avg (line); OOT auto vs effort split % (stacked bar).
4. **Detail table** — grouped by opening MPM band (+ a Total OOT book group). Four metric rows per band (Collections Rm, Yield %, Payer rate %, Effort %) × last 7 months + MoM Δ / 3M Avg / YoY.

> **Aggregation:** raw rows are split by `product × opening band × closing band × month`. `get_oot_band_frame()` sums the additive counts/balances/collections across product and the **closing** band so each row is a whole opening-band population, then re-derives the rate metrics; `get_oot_total_frame()` does the same across all bands for the whole-book series. Never average the pre-computed `*_pct` columns — re-derive from the summed additive columns.

### Tab 3 — Roll Rates (DPD migration, whole book)
Driven by `sql/roll_rates_by_days_past_due.sql` via `tools/roll_rates_by_dpd.py::get_roll_rates_frame`.
1. **Summary cards** (latest full month, % of all accounts, MoM Δ):
   - **Cure rate** = returned to Current (higher = better)
   - **Forward-roll rate** = worsened a band + stayed in 91+ DPD (higher = worse)
   - **Default rate** = stayed in 91+ DPD (higher = worse)
   - **Stable/Current** = held their band or stayed Current (higher = better)
2. **Transition matrix** — 13-month pooled, rows = DPD at start, cols = DPD at end; each cell shows the row-normalised % (each start row sums to 100%) + pooled loan count, heatmap-shaded, diagonal outlined.
3. **Charts** — DPD movement composition (stacked % by movement class) and key roll-rate trend lines, both over the 13 months.

> **Movement reclassification:** the query's own `movement_type` column is unreliable — its CASE compares against mis-typed literals (`'0.Current'`, `'7.91DPD'`) that don't match the actual band labels (`'0. Current'`, `'6. 91DPD'`), so it only ever emits Stable/Rolled Forward/Rolled Backward. `roll_rates_by_dpd.classify_movement()` re-derives the intended 7-way taxonomy from the start/end bands. The fix belongs in the SQL eventually.

### Tab 4 — Queue Penetration (PTP coverage, fulfillment & recovery, in-term queue)
Driven by `sql/collections_queue_penetration.sql` via `tools/queue_penetration.py::get_primary_queue_frame`. Measures how much of the collections queue gets a Promise-to-Pay (PTP) arrangement, how many of those arrangements are kept, how much money is recovered, and the overall collections yield. Scoped to the `Internal Collections - in term` **start-of-month** queue (the headline queue); the raw query also covers other start queues (e.g. `Current`).

Every PTP/recovery metric carries a **timing split** — the arrangement can be due *in the reporting month* (`_in_month`) or forward-booked into the *next month* (`_in_next_month`); the combined figure is the headline. Volumes and counts sum cleanly across timing (`in_month + next_month = total`); penetration is loan-level (a loan can have a PTP in both months, so the combined is an OR, not a sum).

1. **Summary cards** (latest full month, MoM Δ; all higher = better) — the collections funnel:
   - **Penetration rate** = loans with any PTP / loans in queue
   - **PTP fulfillment** = kept arrangements / arrangements made (kept rate)
   - **Recovery yield** = recovered volume / queue exposure (original instalment)
   - **Collections yield** = net receipts / total due (arrears + instalment due)
2. **Charts** (monthly series across the window): penetration % by PTP timing (line — this month / next month / any); PTP fulfillment % by timing (line); recovery yield % this-month vs next-month (stacked bar); collections vs recovery yield (line); rand funnel — exposure → promised → recovered (Rm, grouped bar); queue size — loans vs loans with a PTP (grouped bar).
3. **Monthly-detail table** — 10 metric rows (loans in queue, loans with PTP, queue exposure Rm, promised Rm, recovered Rm, net receipts Rm, penetration %, PTP fulfillment %, recovery yield %, collections yield %) × last 7 months + MoM Δ / 3M Avg / YoY.

> **Aggregation:** raw rows are split by `(reporting_month, start dept, end dept)`. `get_queue_penetration_frame()` sums the additive counts/volumes/financials across the **end** department so each metric is measured at the start-of-month queue level, then re-derives every rate (per timing split). The SQL exposes `number_of_attempted_ptp_dos`/`number_of_settled_ptp_do` (+ in-month/next-month) for exactly this — never average the pre-computed `*_pct` columns; re-derive from the count/volume sums.

> **PTP join fan-out (fixed):** the `master` CTE left-joins the attempted-PTP and settled-PTP tables on `loannumber`. `ptp_orders` was originally one row *per debit order* while `settled_ptp_orders` is one row *per loan-month*, so a loan with N attempted DOs multiplied the settled counts/volumes by N — inflating fulfillment (>100%) and recovery. `ptp_orders` is now pre-aggregated to one row per `(loannumber, scheduled_month)` so the joins are 1:1. Penetration (loan-level flags) and financial columns (GROUP BY keys) were unaffected by the bug.

## Updating Targets
Edit `assets/targets.json` (percentages, not fractions):
```json
{ "collection_rate": 27.7, "effort_yield": 10.7, "auto_collect": 74.1, "payer_rate": 42.0 }
```
Re-run `python tools/generate_dashboard.py` after editing.

## Updating Brand Assets
1. Replace `assets/logo.png` with your company logo (PNG recommended, transparent background)
2. Edit `assets/brand.json` — update `primary`, `secondary`, `accent` hex values and `font` (any Google Font name)
3. Re-run `python tools/generate_dashboard.py`

## Edge Cases
| Situation | Behaviour |
|---|---|
| DB connection fails | Script prints error and exits with code 1 |
| Query returns no data | Script warns and exits with code 1 |
| `targets.json` missing | Cards render with `tgt 0.0%` (delta vs zero); warning printed |
| `logo.png` missing | Warning printed; dashboard renders without logo |
| `brand.json` missing | Warning printed; dashboard uses default theme |

## Architecture
- `tools/collection_by_segment.py` — runs `sql/internal_collections.sql`; `get_segment_frame()` keeps the `A_SEGMENT` result set, aggregates across products, and derives the ratio metrics. (In-Term tab.)
- `tools/roll_rates_by_dpd.py` — runs `sql/roll_rates_by_days_past_due.sql`; `get_roll_rates_frame()` cleans the prefixed month strings, drops join-miss nulls, and recomputes the `movement` class via `classify_movement()`. (Roll Rates tab.)
- `tools/insights.py` — deterministic insight engine used by the **newsletter**: `candidate_facts(metrics)` selects/scores/phrases notable movements and `split()` produces the global highlights/lowlights. Pure function of `metrics.json` — figures are never invented.
- `tools/queue_penetration.py` — runs `sql/collections_queue_penetration.sql`; `get_queue_penetration_frame()` aggregates across the end department and re-derives penetration/fulfillment/recovery rates; `get_primary_queue_frame()` returns the in-term queue's monthly series. (Queue Penetration tab.)
- `tools/out_of_term_collections.py` — runs `sql/out_of_term_collections.sql`; `get_oot_band_frame()` aggregates across product + closing band to the opening-MPM-band level; `get_oot_total_frame()` returns the whole-book monthly series. Both re-derive the FTTC-based rate metrics. (Out of Term tab.)
- `tools/generate_dashboard.py` — orchestrator: queries all four datasets → cards / hero / chart data / table / matrix → Jinja2 render → file output.
- `assets/dashboard_template.html` — Jinja2 HTML template with Chart.js; four tab views (`#view-interm`, `#view-oot`, `#view-roll`, `#view-pen`).

## Notes / Constraints
- **In-term SQL schema:** `internal_collections.sql` returns a `UNION ALL` of `A_SEGMENT` (per `product × delinquency_segment × month`) and `B_TOTAL` (per `product × month`), distinguished by the `result_set` column. The dashboard consumes `A_SEGMENT` only.
- **In-term population:** the dashboard cards/charts cover `delinquency_bucket IN ('New Loan','Current','Early Arrears','Deep Arrears')` = segments New Loan/MP0/MP1/MP2/MP3+; only Out of Term (MPM2) is excluded. This is **deliberately broader** than the SQL handover's locked KPI population (`Early Arrears` + `Deep Arrears` only) — the segment-detail table additionally shows MPM2.
- **Roll-rate SQL schema:** `roll_rates_by_days_past_due.sql` is whole-book, grouped by `snap_date`/`reporting_month_end`/`dpd_at_start_of_month`/`dpd_at_end_of_month`/`movement_type` with `loan_count`. `snap_date` is a prefixed string (e.g. `10.2025-08-31`); the month is taken from `reporting_month_end`. The matrix/cards are portfolio-wide (no product/segment/in-term split available without editing the SQL). Movement classes are recomputed in Python (see Tab 2 note).
- **Cost analysis** (`sql/internal_collections2.sql`, Q08b) is not yet wired into the dashboard.

## Troubleshooting
- **`ModuleNotFoundError: jinja2`** → run `pip install -r requirements.txt`
- **`psycopg2` SSL error** → ensure `DATABASE_URL` includes `?sslmode=require` or equivalent
- **Charts blank after opening** → check browser console; CDN assets (Chart.js, Google Fonts) require internet access
- **Auto + Effort don't sum to exactly 100%** → expected; the FTTC instalment/effort split is derived independently of `net_receipts`, so the stacked bar can sit slightly above/below 100%.
