import os
import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

SQL_DIR = Path(__file__).parent.parent / "sql"


def load_sql(filename: str) -> str:
    return (SQL_DIR / filename).read_text()


def get_out_of_term_data(engine=None) -> pd.DataFrame:
    if engine is None:
        engine = create_engine(os.getenv("DATABASE_URL"))
    sql = load_sql("out_of_term_collections.sql")
    return pd.read_sql(text(sql), engine)


# Additive columns summed across the product × MPM-band grain to reach a
# whole-book monthly total. Each loan maps to exactly one band row per month,
# so summing counts does not double-count.
_OOT_ADDITIVE = [
    "loan_count", "opening_balance", "total_collections",
    "instalment_collections", "effort_collections", "active_payers",
]


def get_oot_frame(engine=None) -> pd.DataFrame:
    """Whole-book Out-of-Term metrics per transaction_month.

    Aggregates the per-band query to one row per month, re-derives ratio
    metrics from the summed numerators/denominators, and adds 3-month rolling
    averages for the headline series.
    """
    df = get_out_of_term_data(engine)
    df["transaction_month"] = pd.to_datetime(df["transaction_month"])

    g = (df.groupby("transaction_month", as_index=False)[_OOT_ADDITIVE]
           .sum()
           .sort_values("transaction_month")
           .reset_index(drop=True))

    bal = g["opening_balance"].replace(0, np.nan)
    coll = g["total_collections"].replace(0, np.nan)
    loans = g["loan_count"].replace(0, np.nan)
    g["yield_pct"]      = g["total_collections"] / bal * 100
    g["payer_rate_pct"] = g["active_payers"] / loans * 100
    g["auto_pct"]       = g["instalment_collections"] / coll * 100
    g["effort_pct"]     = g["effort_collections"] / coll * 100

    g["collections_rm"]     = g["total_collections"] / 1e6
    g["collections_rm_3m"]  = g["collections_rm"].rolling(3, min_periods=1).mean()
    g["yield_pct_3m"]       = g["yield_pct"].rolling(3, min_periods=1).mean()
    g["payer_rate_pct_3m"]  = g["payer_rate_pct"].rolling(3, min_periods=1).mean()
    return g


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch out-of-term collections data (last 12 months)")
    parser.add_argument("--output", default=".tmp/out_of_term_collections.csv", help="Output CSV path")
    args = parser.parse_args()

    engine = create_engine(os.getenv("DATABASE_URL"))
    print("Fetching out-of-term collections data...")
    df = get_out_of_term_data(engine)
    print(df.head())
    print(f"\nRows: {len(df)}, Columns: {list(df.columns)}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"Saved to {args.output}")
