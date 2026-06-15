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


def get_internal_collections_data(engine=None) -> pd.DataFrame:
    if engine is None:
        engine = create_engine(os.getenv("DATABASE_URL"))
    sql = load_sql("internal_collections.sql")
    return pd.read_sql(text(sql), engine)


# Additive columns that can be summed across products; ratio metrics are
# re-derived from these sums (never average the pre-computed *_pct columns).
_ADDITIVE_COLS = [
    "loan_count", "net_receipts", "payers", "opening_arrears", "arrears",
    "opening_balance", "closing_balance", "instalment_due",
    "instalment_collections", "effort_collections",
]


def get_segment_frame(engine=None) -> pd.DataFrame:
    """Per (transaction_month, delinquency_segment) frame for the dashboard.

    Keeps the A_SEGMENT result set, aggregates across products by summing the
    additive columns, and re-derives the ratio metrics from those sums:
      - collection_yield_pct = net_receipts / (opening_arrears + instalment_due) * 100
      - effort_yield_pct     = effort_collections / (opening_arrears + instalment_due) * 100
      - auto_pct             = instalment_collections / net_receipts * 100
      - effort_pct           = effort_collections / net_receipts * 100
      - payer_rate_pct       = payers / loan_count * 100
      - arrears_move         = arrears (closing) - opening_arrears
    """
    df = get_internal_collections_data(engine)
    a = df[df["result_set"] == "A_SEGMENT"].copy()
    a["transaction_month"] = pd.to_datetime(a["transaction_month"])

    g = a.groupby(["transaction_month", "delinquency_bucket", "delinquency_segment"],
                  as_index=False)[_ADDITIVE_COLS].sum()

    denom = (g["opening_arrears"] + g["instalment_due"]).replace(0, np.nan)
    net   = g["net_receipts"].replace(0, np.nan)
    loans = g["loan_count"].replace(0, np.nan)
    g["collection_yield_pct"] = g["net_receipts"] / denom * 100
    g["effort_yield_pct"]     = g["effort_collections"] / denom * 100
    g["auto_pct"]             = g["instalment_collections"] / net * 100
    g["effort_pct"]           = g["effort_collections"] / net * 100
    g["payer_rate_pct"]       = g["payers"] / loans * 100
    g["arrears_move"]         = g["arrears"] - g["opening_arrears"]
    return g


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch internal collections data (last 12 months)")
    parser.add_argument("--output", default=".tmp/internal_collections.csv", help="Output CSV path")
    args = parser.parse_args()

    engine = create_engine(os.getenv("DATABASE_URL"))
    print("Fetching internal collections data...")
    df = get_internal_collections_data(engine)
    print(df.head())
    print(f"\nRows: {len(df)}, Columns: {list(df.columns)}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"Saved to {args.output}")
