import os
import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

load_dotenv()

SQL_DIR = Path(__file__).parent.parent / "sql"


def load_sql(filename: str) -> str:
    return (SQL_DIR / filename).read_text()


def get_roll_rates_data(engine=None) -> pd.DataFrame:
    if engine is None:
        engine = create_engine(os.getenv("DATABASE_URL"))
    sql = load_sql("roll_rates_by_days_past_due.sql")
    return pd.read_sql(text(sql), engine)


def _to_month(series: pd.Series) -> pd.Series:
    """Parse a month-end column that may carry a 'monthindex.' prefix
    (e.g. '10.2025-08-31') into a Timestamp."""
    cleaned = series.astype(str).str.replace(r"^\d+\.", "", regex=True)
    return pd.to_datetime(cleaned, errors="coerce")


def _band_rank(label) -> int:
    """Numeric prefix of a DPD band label, e.g. '6. 91DPD' -> 6."""
    return int(str(label).split(".", 1)[0])


def classify_movement(start, end, worst_rank: int) -> str:
    """Re-derive the DPD movement class from start/end bands.

    The query's own `movement_type` CASE compares against mis-typed string
    literals ('0.Current', '7.91DPD') that don't match the actual labels
    ('0. Current', '6. 91DPD'), so it only ever emits Stable/Rolled Forward/
    Rolled Backward. This reproduces the *intended* taxonomy from the bands."""
    s, e = str(start), str(end)
    if "ANew" in s:
        return "New"
    sr, er = _band_rank(s), _band_rank(e)
    if er == 0 and sr == 0:
        return "Current"
    if er == 0 and sr != 0:
        return "Cured"
    if sr == worst_rank and er == worst_rank:
        return "Default"
    if er == sr:
        return "Stable"
    return "Rolled Forward" if er > sr else "Rolled Backward"


def get_roll_rates_frame(engine=None) -> pd.DataFrame:
    """Tidy DPD-migration frame: one row per month × start-DPD × end-DPD, with
    loan_count and a recomputed `movement` class. Whole book, 13-month window.

    `snap_date`/`reporting_month_end` arrive as prefixed strings, so the month
    key is derived defensively into `month_date` (+ a 'Mon yy' `month` label).
    Rows with missing month/start/end (left-join misses) are dropped."""
    df = get_roll_rates_data(engine)
    df["month_date"] = _to_month(df["reporting_month_end"])
    df = df.dropna(subset=["month_date", "dpd_at_start_of_month", "dpd_at_end_of_month"]).copy()
    df["month"] = df["month_date"].dt.strftime("%b %y")
    df["loan_count"] = df["loan_count"].astype(int)

    worst = max(_band_rank(b) for b in df["dpd_at_end_of_month"].unique())
    df["movement"] = [classify_movement(s, e, worst)
                      for s, e in zip(df["dpd_at_start_of_month"], df["dpd_at_end_of_month"])]
    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch roll-rate (DPD migration) data")
    parser.add_argument("--output", default=".tmp/roll_rates.csv", help="Output CSV path")
    args = parser.parse_args()

    engine = create_engine(os.getenv("DATABASE_URL"))
    print("Fetching roll-rate data...")
    df = get_roll_rates_frame(engine)
    print(df.head())
    print(f"\nRows: {len(df)}, Columns: {list(df.columns)}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    df.to_csv(args.output, index=False)
    print(f"Saved to {args.output}")
