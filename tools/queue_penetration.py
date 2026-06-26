import os
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

SQL_DIR = Path(__file__).parent.parent / "sql"

# The collections queue we headline in the cards/charts. Rows are split by the
# end-of-month department too; we sum across those so penetration is measured at
# the start-of-month queue level (where the loan entered the queue).
PRIMARY_QUEUE = "Internal Collections - in term"


def load_sql(filename: str) -> str:
    return (SQL_DIR / filename).read_text()


def get_queue_penetration_data(engine=None) -> pd.DataFrame:
    if engine is None:
        engine = create_engine(os.getenv("DATABASE_URL"))
    sql = load_sql("collections_queue_penetration.sql")
    return pd.read_sql(text(sql), engine)


# Additive columns that sum cleanly across end-departments; ratio metrics are
# re-derived from these sums (never average the pre-computed *_pct columns).
_ADDITIVE_COLS = [
    "number_of_loans", "total_queue_exposure", "total_recovered_volume",
    "number_of_loans_with_ptp_dos", "number_of_loans_with_ptp_dos_adj",
    "number_of_ptp_dos", "number_of_kept_dos",
    "number_of_ptp_dos_adj", "number_of_kept_dos_adj",
]


def _derive_rates(g: pd.DataFrame) -> pd.DataFrame:
    """Re-derive the penetration / fulfillment / recovery rates from the summed
    additive columns."""
    loans = g["number_of_loans"].replace(0, np.nan)
    expo  = g["total_queue_exposure"].replace(0, np.nan)
    ptp   = g["number_of_ptp_dos"].replace(0, np.nan)
    ptpa  = g["number_of_ptp_dos_adj"].replace(0, np.nan)
    g["penetration_rate_pct"]       = g["number_of_loans_with_ptp_dos"] / loans * 100
    g["penetration_rate_adj_pct"]   = g["number_of_loans_with_ptp_dos_adj"] / loans * 100
    g["ptp_fulfillment_rate_pct"]   = g["number_of_kept_dos"] / ptp * 100
    g["ptp_fulfillment_rate_adj_pct"] = g["number_of_kept_dos_adj"] / ptpa * 100
    g["recovery_rate_pct"]          = g["total_recovered_volume"] / expo * 100
    return g


def get_queue_penetration_frame(engine=None) -> pd.DataFrame:
    """Per (reporting_month, department_start_of_month) frame for the dashboard.

    Aggregates across the end-of-month department (summing additive counts and
    volumes) so each row is a queue's whole start-of-month population, and
    re-derives the rate metrics:
      - penetration_rate_pct       = loans_with_ptp / loans * 100
      - penetration_rate_adj_pct   = loans_with_ptp_adj / loans * 100  (PTP ≥ 80% of instalment)
      - ptp_fulfillment_rate_pct   = kept_dos / ptp_dos * 100  (kept rate of arrangements made)
      - recovery_rate_pct          = recovered_volume / queue_exposure * 100
    """
    df = get_queue_penetration_data(engine)
    df["reporting_month"] = pd.to_datetime(df["reporting_month"])

    g = df.groupby(["reporting_month", "department_start_of_month"],
                   as_index=False)[_ADDITIVE_COLS].sum()
    return _derive_rates(g)


def get_primary_queue_frame(engine=None, queue: str = PRIMARY_QUEUE) -> pd.DataFrame:
    """The headline queue's monthly time series (one row per reporting_month)."""
    g = get_queue_penetration_frame(engine)
    return g[g["department_start_of_month"] == queue].sort_values("reporting_month").reset_index(drop=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch collections queue penetration data")
    parser.add_argument("--output", default=".tmp/queue_penetration.csv", help="Output CSV path")
    args = parser.parse_args()

    engine = create_engine(os.getenv("DATABASE_URL"))
    print("Fetching queue penetration data...")
    df = get_queue_penetration_frame(engine)
    print(df.head(20).to_string())
    print(f"\nRows: {len(df)}, Columns: {list(df.columns)}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"Saved to {args.output}")
