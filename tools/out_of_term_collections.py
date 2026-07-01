import os
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

SQL_DIR = Path(__file__).parent.parent / "sql"

# Opening MPM band → short display label (screenshot: "0–3 mths" etc). The SQL
# emits numeric-prefixed labels ('01 — 0–3 months') so they sort correctly; we
# keep the raw value for ordering and add a compact label for the dashboard.
BAND_LABELS = {
    "01 — 0–3 months":   "0–3 mths",
    "02 — 4–6 months":   "4–6 mths",
    "03 — 7–12 months":  "7–12 mths",
    "04 — 13–24 months": "13–24 mths",
    "05 — 24+ months":   "24+ mths",
    "00 — Unknown":      "Unknown",
}


def load_sql(filename: str) -> str:
    return (SQL_DIR / filename).read_text()


def get_out_of_term_data(engine=None) -> pd.DataFrame:
    if engine is None:
        engine = create_engine(os.getenv("DATABASE_URL"))
    sql = load_sql("out_of_term_collections.sql")
    return pd.read_sql(text(sql), engine)


# Additive columns that sum cleanly across products and the closing MPM band
# (a loan sits in exactly one product × closing-band cell per opening-band month,
# so distinct-loan counts sum without double-counting). Ratio metrics are
# re-derived from these sums — never average the pre-computed *_pct columns.
_ADDITIVE_COLS = [
    "loan_count",
    "opening_balance", "closing_balance", "balance_movement",
    "total_collections", "instalment_collections", "effort_collections",
    "active_payers", "lapsed_payers", "never_paid",
    "provision_balance", "provision_coverage_gap",
]


def _derive_rates(g: pd.DataFrame) -> pd.DataFrame:
    """Re-derive OOT rate metrics from the summed additive columns.

    Per the operational definition: Collections = instalment + effort (FTTC),
    Yield = FTTC collections / opening_balance."""
    # FTTC-based collections (instalment + effort) — the headline "collected".
    g["fttc_collections"] = g["instalment_collections"] + g["effort_collections"]

    opening = g["opening_balance"].replace(0, np.nan)
    coll    = g["fttc_collections"].replace(0, np.nan)
    loans   = g["loan_count"].replace(0, np.nan)

    g["yield_pct"]      = g["fttc_collections"] / opening * 100
    g["payer_rate_pct"] = g["active_payers"] / loans * 100
    g["auto_pct"]       = g["instalment_collections"] / coll * 100
    g["effort_pct"]     = g["effort_collections"] / coll * 100
    return g


def get_oot_band_frame(engine=None, data: pd.DataFrame = None) -> pd.DataFrame:
    """Per (transaction_month, opening MPM band) frame for the OOT detail table.

    Aggregates across product and the closing MPM band so each row is a whole
    opening-band population for the month, then re-derives the rate metrics.
    Pass a pre-fetched raw `data` frame to avoid re-running the query.
    """
    df = data if data is not None else get_out_of_term_data(engine)
    df = df.copy()
    df["transaction_month"] = pd.to_datetime(df["transaction_month"])

    g = df.groupby(["transaction_month", "prev_mpm_band"],
                   as_index=False)[_ADDITIVE_COLS].sum()
    g = _derive_rates(g)
    g["band_label"] = g["prev_mpm_band"].map(BAND_LABELS).fillna(g["prev_mpm_band"])
    return g.sort_values(["transaction_month", "prev_mpm_band"]).reset_index(drop=True)


def get_oot_total_frame(engine=None, data: pd.DataFrame = None) -> pd.DataFrame:
    """Whole OOT-book monthly time series (one row per transaction_month).

    Powers the OOT cards, hero KPI and trend charts. Pass a pre-fetched raw
    `data` frame to avoid re-running the query."""
    df = data if data is not None else get_out_of_term_data(engine)
    df = df.copy()
    df["transaction_month"] = pd.to_datetime(df["transaction_month"])

    g = df.groupby(["transaction_month"], as_index=False)[_ADDITIVE_COLS].sum()
    g = _derive_rates(g)
    return g.sort_values("transaction_month").reset_index(drop=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch out-of-term collections data")
    parser.add_argument("--output", default=".tmp/out_of_term_collections.csv", help="Output CSV path")
    args = parser.parse_args()

    engine = create_engine(os.getenv("DATABASE_URL"))
    print("Fetching out-of-term collections data...")
    band = get_oot_band_frame(engine)
    total = get_oot_total_frame(engine)
    print(band.head(20).to_string())
    print(f"\nBand rows: {len(band)}, Total rows: {len(total)}")
    print(f"Columns: {list(band.columns)}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    band.to_csv(args.output, index=False)
    print(f"Saved band frame to {args.output}")
