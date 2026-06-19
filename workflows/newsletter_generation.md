# Collections Newsletter Generation

## Objective
Produce a branded, email-ready **newsletter** (`output/newsletter.html` + `output/newsletter.eml`) summarising the dashboard's insights as **Highlights** and **Lowlights**, for distribution to colleagues.

> **File-only.** The tool never sends email. It generates files you forward (or open in a mail client and send). Enabling real send (SMTP/API) requires credentials and a separate explicit go-ahead.

## Prerequisites
1. A dashboard run must have produced **`output/metrics.json`** (the numeric metrics bundle) and `output/dashboard.html`:
   ```bash
   python tools/generate_dashboard.py
   ```
   Always regenerate the dashboard first when the data refreshes — the newsletter reads `metrics.json`, not the live DB.
2. **Hybrid narrative (optional but recommended):** set `ANTHROPIC_API_KEY` in `.env`. With it, Claude (`claude-sonnet-4-6`) writes the prose from the deterministically-selected facts. Without it (or on any API error), the tool falls back to rule-based sentences — still a complete newsletter.

## Generate
```bash
python tools/generate_newsletter.py
```
Outputs:
| File | Description |
|---|---|
| `output/newsletter.html` | Branded, inline-styled HTML — open in a browser or paste into an email |
| `output/newsletter.eml` | MIME message (HTML body + `dashboard.html` attached) — open in a mail client and send/forward |

## How insights are chosen (deterministic → then narrated)
- **Facts & ranking are deterministic** (`build_insights`): candidate movements are drawn from in-term cards (target beat/miss + MoM), OOT cards (MoM), roll-rate cards (MoM, direction-aware), and in-term segment movers. Each is scored by magnitude (pp for %-metrics, % change otherwise), de-duplicated per metric, and split into Highlights (good) / Lowlights (bad), top ~6 each. **Numbers come only from `metrics.json` — never invented.**
- **Claude only rewrites wording** (`narrate`): it receives the selected fact sentences and returns polished one-liners + a 2–3 sentence exec summary, with strict instructions to preserve every figure. If the key is absent or the call fails, the deterministic sentences are used verbatim.
- The metric tables (In-Term vs target, OOT vs prior, Roll vs prior) are always rendered straight from `metrics.json`, so the figures are model-independent.

## Recipients (optional)
Edit `assets/recipients.json` to pre-fill the `.eml` `To:`/subject (used only to populate the file — still not sent):
```json
{ "to": ["alice@fasta.co.za", "bob@fasta.co.za"], "subject": "Collections Monthly — {period}" }
```
`{period}` is replaced with the latest reporting month. `From:` can be set via `NEWSLETTER_FROM` in `.env`.

## Email-rendering constraints
Mail clients strip `<script>`/`<style>` and can't run Chart.js, so the newsletter is **static, table-based, inline-styled** (numbers + ▲▼ arrows + coloured Highlight/Lowlight bullets). The interactive charts live in `dashboard.html`, which is **attached** to the `.eml`.

## Edge cases
| Situation | Behaviour |
|---|---|
| `output/metrics.json` missing | Error + exit 1 ("run generate_dashboard first") |
| `ANTHROPIC_API_KEY` missing / API error | Deterministic rule-based narrative (no failure) |
| `recipients.json` missing / empty `to` | `.eml` generated with a blank `To:` |
| A month has no notable moves | Highlights/Lowlights show a "nothing material" line |

## Architecture
- `tools/generate_dashboard.py::build_metrics_bundle` — writes `output/metrics.json` (numeric in-term/OOT/roll metrics with MoM/YoY/target deltas).
- `tools/generate_newsletter.py` — `build_insights` (deterministic) → `narrate` (Claude, optional) → `render` (Jinja2) → `write_eml`.
- `assets/newsletter_template.html` — email-safe Jinja2 template.
- `assets/recipients.json` — optional recipient/subject config (pre-fill only).
