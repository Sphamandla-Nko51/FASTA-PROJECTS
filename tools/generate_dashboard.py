import os
import re
import sys
import json
import base64
import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent))
from collection_by_segment import get_segment_frame
from out_of_term_by_segment import get_oot_frame
from roll_rates_by_dpd import get_roll_rates_frame

load_dotenv()

ROOT = Path(__file__).parent.parent
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "output"

# SEGMENTS — opening segments plotted on the by-segment line charts.
# IN_TERM_BUCKETS — buckets folded into the summary cards (the whole in-term
# book; only Out of Term / MPM2 is excluded). NOTE: this is broader than the
# SQL handover's locked arrears-only KPI population (Early + Deep Arrears).
SEGMENTS = ["New Loan", "MP0", "MP1", "MP2", "MP3+"]
IN_TERM_BUCKETS = ["New Loan", "Current", "Early Arrears", "Deep Arrears"]

# Display order for the segment-detail table (any value not listed is appended
# alphabetically, so new buckets/segments still show up).
BUCKET_ORDER = ["Current", "Early Arrears", "Deep Arrears", "New Loan", "Out of Term"]
SEGMENT_ORDER = ["MP0", "MP1", "MP2", "MP3+", "MPM2", "New Loan"]


def _ordered(values, ref):
    known = [x for x in ref if x in values]
    rest = sorted(v for v in values if v not in ref)
    return known + rest


# Roll-rate (DPD migration) movement taxonomy, in display order.
# Matches the labels produced by roll_rates_by_dpd.classify_movement().
MOVEMENT_ORDER = ["New", "Current", "Cured", "Rolled Backward",
                  "Stable", "Rolled Forward", "Default"]


def _pretty_dpd(label) -> str:
    """Strip the sort prefix for display: '0. Current' -> 'Current',
    '6. 91DPD' -> '91DPD', '0. ANew' -> 'New'."""
    if label is None:
        return "—"
    txt = str(label).split(".", 1)[-1].strip()
    return "New" if txt == "ANew" else txt


# ── branding / formatting scaffolding ──────────────────────────────────────
def load_brand() -> dict:
    brand_path = ASSETS_DIR / "brand.json"
    if not brand_path.exists():
        print("Warning: assets/brand.json not found — using defaults")
        return {"company": "Company", "primary": "#1A1A2E", "secondary": "#E94560",
                "accent": "#0F3460", "font": "Inter", "logo": ""}
    with open(brand_path) as f:
        return json.load(f)


def load_targets() -> dict:
    targets_path = ASSETS_DIR / "targets.json"
    defaults = {"collection_rate": 0.0, "effort_yield": 0.0, "auto_collect": 0.0, "payer_rate": 0.0}
    if not targets_path.exists():
        print("Warning: assets/targets.json not found — cards will show no targets")
        return defaults
    with open(targets_path) as f:
        return {**defaults, **json.load(f)}


def encode_logo(logo_path: str) -> str | None:
    path = Path(logo_path)
    if not path.exists():
        path = ROOT / logo_path
    if not path.exists():
        print(f"Warning: logo not found at {logo_path} — skipping logo")
        return None
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def to_hex(color: str) -> str:
    m = re.match(r'rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)', color.strip())
    if m:
        return f"#{int(m.group(1)):02x}{int(m.group(2)):02x}{int(m.group(3)):02x}"
    return color


def normalize_brand_colors(brand: dict) -> dict:
    brand = brand.copy()
    for key in ("primary", "secondary", "accent"):
        if key in brand:
            brand[key] = to_hex(brand[key])
    return brand


# ── shared helpers over the aggregated segment frame ────────────────────────
def _months(frame: pd.DataFrame) -> list:
    return sorted(frame["transaction_month"].unique())


def _label(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %y")


def _series(frame: pd.DataFrame, seg: str, col: str, months: list, scale: float = 1.0):
    s = frame[frame["delinquency_segment"] == seg].set_index("transaction_month")[col]
    out = []
    for m in months:
        if m in s.index and pd.notna(s.loc[m]):
            out.append(round(float(s.loc[m]) * scale, 4))
        else:
            out.append(None)
    return out


# ── summary cards ───────────────────────────────────────────────────────────
def compute_cards(frame: pd.DataFrame, targets: dict) -> list:
    months = _months(frame)
    latest = months[-1]
    sub = frame[(frame["delinquency_bucket"].isin(IN_TERM_BUCKETS)) &
                (frame["transaction_month"] == latest)]

    net   = sub["net_receipts"].sum()
    oa    = sub["opening_arrears"].sum()
    idue  = sub["instalment_due"].sum()
    eff   = sub["effort_collections"].sum()
    inst  = sub["instalment_collections"].sum()
    payrs = sub["payers"].sum()
    loans = sub["loan_count"].sum()
    denom = oa + idue

    values = {
        "collection_rate": net / denom * 100 if denom else 0.0,
        "effort_yield":    eff / denom * 100 if denom else 0.0,
        "auto_collect":    inst / net * 100 if net else 0.0,
        "payer_rate":      payrs / loans * 100 if loans else 0.0,
    }
    specs = [
        ("collection_rate", "Collection Rate"),
        ("effort_yield",    "Effort Yield"),
        ("auto_collect",    "Auto Collect %"),
        ("payer_rate",      "Payer Rate"),
    ]
    cards = []
    for key, label in specs:
        v = values[key]
        tgt = float(targets.get(key, 0.0))
        delta = v - tgt
        cards.append({
            "label":        label,
            "value":        fmt_pct(v),
            "target":       fmt_pct(tgt),
            "delta":        f"{'+' if delta >= 0 else ''}{delta:.1f}pp",
            "meets_target": bool(v >= tgt),
            "sub":          f"In-term book · {pd.Timestamp(latest).strftime('%b %Y')}",
        })
    return cards


# ── chart data ──────────────────────────────────────────────────────────────
def build_chart_data(frame: pd.DataFrame) -> dict:
    months = _months(frame)
    labels = [_label(m) for m in months]

    def seg_sets(col, scale=1.0):
        return [{"label": seg, "data": _series(frame, seg, col, months, scale)}
                for seg in SEGMENTS]

    return {
        "months": labels,
        # 1 — effort yield % by opening segment (line)
        "effort_yield_datasets":   seg_sets("effort_yield_pct"),
        # 2 — collection rate % by opening segment (line)
        "collection_rate_datasets": seg_sets("collection_yield_pct"),
        # 3 — auto vs effort composition for MP1 (% of collected, stacked bar)
        "mp1_auto":   _series(frame, "MP1", "auto_pct", months),
        "mp1_effort": _series(frame, "MP1", "effort_pct", months),
        # 4 — net arrears movement (Rm) by segment (line)
        "arrears_move_datasets": seg_sets("arrears_move", scale=1e-6),
    }


# ── segment table ─────────────────────────────────────────────────────────--
def _fmt_val(v, unit):
    if v is None:
        return "—"
    if unit == "rm":
        return f"R{v:.2f}m"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:.1f}%"


def _fmt_delta(d, unit):
    if d is None:
        return "—"
    if unit == "rm":
        return f"{'+' if d >= 0 else '-'}R{abs(d):.2f}m"
    if unit == "count":
        return f"{'+' if d >= 0 else '-'}{abs(d):,.0f}"
    return f"{'+' if d >= 0 else ''}{d:.1f}pp"


def _cls(d):
    if d is None:
        return "neutral"
    return "pos" if d >= 0 else "neg"


def _metric_row(frame, seg, label, col, unit, months, scale=1.0):
    s = frame[frame["delinquency_segment"] == seg].set_index("transaction_month")[col]

    def val(m):
        return float(s.loc[m]) * scale if m in s.index and pd.notna(s.loc[m]) else None

    display = months[-7:]
    latest, prev = months[-1], months[-2] if len(months) >= 2 else None
    yoy_month = months[-13] if len(months) >= 13 else None
    last3 = [val(m) for m in months[-3:] if val(m) is not None]

    lv = val(latest)
    pv = val(prev) if prev is not None else None
    yv = val(yoy_month) if yoy_month is not None else None
    mom = (lv - pv) if lv is not None and pv is not None else None
    yoy = (lv - yv) if lv is not None and yv is not None else None
    avg3 = sum(last3) / len(last3) if last3 else None

    return {
        "label":  label,
        "cells":  [_fmt_val(val(m), unit) for m in display],
        "mom":    _fmt_delta(mom, unit),
        "mom_cls": _cls(mom),
        "avg3":   _fmt_val(avg3, unit),
        "yoy":    _fmt_delta(yoy, unit),
        "yoy_cls": _cls(yoy),
    }


def build_segment_table(frame: pd.DataFrame) -> dict:
    months = _months(frame)
    metric_specs = [
        ("Count of loans",   "loan_count",           "count", 1.0),
        ("Collections (Rm)", "net_receipts",         "rm",    1e-6),
        ("Yield %",          "collection_yield_pct", "pct",   1.0),
        ("Payer rate %",     "payer_rate_pct",       "pct",   1.0),
    ]

    def seg_block(seg):
        return {"segment": seg,
                "metrics": [_metric_row(frame, seg, lbl, col, unit, months, scale)
                            for lbl, col, unit, scale in metric_specs]}

    # Build one group per delinquency bucket present in the data, each listing
    # its segments — so every available segment (MP0, New Loan, MPM2, …) shows.
    pairs = frame[["delinquency_bucket", "delinquency_segment"]].dropna().drop_duplicates()
    groups = []
    for bucket in _ordered(pairs["delinquency_bucket"].unique().tolist(), BUCKET_ORDER):
        segs = _ordered(
            pairs.loc[pairs["delinquency_bucket"] == bucket, "delinquency_segment"].unique().tolist(),
            SEGMENT_ORDER,
        )
        if not segs:
            continue
        label = bucket if segs == [bucket] else f"{bucket} ({' · '.join(segs)})"
        groups.append({"bucket": label, "segments": [seg_block(s) for s in segs]})

    return {
        "month_headers": [_label(m) for m in months[-7:]],
        "groups": groups,
    }


# ── out-of-term recoveries ──────────────────────────────────────────────────
def _fmt_rm(v) -> str:
    return f"R{v / 1e6:.2f}m"


def _signed_rm(v) -> str:
    return f"{'+' if v >= 0 else '-'}R{abs(v) / 1e6:.2f}m"


def _signed_count(v) -> str:
    return f"{'+' if v >= 0 else '-'}{abs(v):,.0f}"


def compute_oot_cards(frame: pd.DataFrame, targets: dict) -> list:
    f = frame.sort_values("transaction_month").reset_index(drop=True)
    cur = f.iloc[-1]
    prev = f.iloc[-2] if len(f) >= 2 else None

    def mom(col):
        return (cur[col] - prev[col]) if prev is not None else None

    coll_d  = mom("total_collections")
    yield_d = mom("yield_pct")
    pay_d   = mom("active_payers")
    tgt     = float(targets.get("oot_book_yield", 0.0))

    return [
        {
            "label": "OOT Collected (FTTC)",
            "value": _fmt_rm(cur["total_collections"]),
            "avg3":  f"3m avg {_fmt_rm(cur['collections_rm_3m'] * 1e6)}",
            "delta": _signed_rm(coll_d) if coll_d is not None else None,
            "delta_cls": ("pos" if coll_d >= 0 else "neg") if coll_d is not None else "neutral",
        },
        {
            "label": "OOT Book Yield",
            "value": f"{cur['yield_pct']:.2f}%",
            "avg3":  f"3m avg {cur['yield_pct_3m']:.2f}%",
            "target": f"{tgt:.2f}%",
            "delta": (f"{'+' if yield_d >= 0 else ''}{yield_d:.2f}pp") if yield_d is not None else None,
            "delta_cls": ("pos" if yield_d >= 0 else "neg") if yield_d is not None else "neutral",
        },
        {
            "label": "OOT Payers",
            "value": f"{int(cur['active_payers']):,}",
            "delta": _signed_count(pay_d) if pay_d is not None else None,
            "delta_cls": ("pos" if pay_d >= 0 else "neg") if pay_d is not None else "neutral",
        },
        {
            "label": "OOT Accounts",
            "value": f"{int(cur['loan_count']):,}",
            "sub":   "active OOT book",
        },
    ]


def _num_list(frame, col, scale=1.0, nd=4):
    return [round(float(v) * scale, nd) if pd.notna(v) else None for v in frame[col]]


def build_oot_chart_data(frame: pd.DataFrame) -> dict:
    f = frame.sort_values("transaction_month").reset_index(drop=True)
    return {
        "months":          [_label(m) for m in f["transaction_month"]],
        "collections":     _num_list(f, "collections_rm", nd=3),
        "collections_3m":  _num_list(f, "collections_rm_3m", nd=3),
        "yield":           _num_list(f, "yield_pct", nd=3),
        "yield_3m":        _num_list(f, "yield_pct_3m", nd=3),
        "payer_rate":      _num_list(f, "payer_rate_pct", nd=3),
        "payer_rate_3m":   _num_list(f, "payer_rate_pct_3m", nd=3),
        "auto":            _num_list(f, "auto_pct", nd=2),
        "effort":          _num_list(f, "effort_pct", nd=2),
    }


# ── roll rates (DPD migration) ──────────────────────────────────────────────
def _heat(pct):
    """Heatmap alpha for a row-% value (sub-linear so small cells stay visible)."""
    if not pct or pct <= 0:
        return 0.0
    return round(min(1.0, (pct / 100) ** 0.55), 3)


def _roll_months(frame):
    m = frame[["month_date", "month"]].dropna().drop_duplicates().sort_values("month_date")
    return m["month_date"].tolist(), m["month"].tolist()


def build_roll_rate_matrix(frame: pd.DataFrame) -> dict:
    """13-month pooled transition matrix: rows = DPD at start, cols = DPD at end.
    Each cell carries the row-normalised % and the pooled loan count."""
    pooled = frame.groupby(["dpd_at_start_of_month", "dpd_at_end_of_month"], as_index=False)["loan_count"].sum()
    row_labels = sorted(pooled["dpd_at_start_of_month"].unique())   # numeric prefix sorts correctly
    col_labels = sorted(pooled["dpd_at_end_of_month"].unique())
    lookup = {(r.dpd_at_start_of_month, r.dpd_at_end_of_month): int(r.loan_count) for r in pooled.itertuples()}

    rows = []
    for rl in row_labels:
        opening = sum(lookup.get((rl, cl), 0) for cl in col_labels)
        cells = []
        for cl in col_labels:
            cnt = lookup.get((rl, cl), 0)
            pct = (cnt / opening * 100) if opening else 0.0
            a = _heat(pct)
            cells.append({
                "pct":   f"{pct:.1f}%" if cnt else "·",
                "count": f"{cnt:,}" if cnt else "",
                "bg":    f"rgba(1,169,230,{a})",
                "dark":  a >= 0.55,
                "diag":  rl == cl,
            })
        rows.append({"label": _pretty_dpd(rl), "opening": f"{opening:,}", "cells": cells})

    return {"col_labels": [_pretty_dpd(c) for c in col_labels], "rows": rows}


def _move_share(month_df, movements):
    total = month_df["loan_count"].sum()
    if not total:
        return 0.0
    return month_df[month_df["movement"].isin(movements)]["loan_count"].sum() / total * 100


# label → (movement set, higher-is-better)
_ROLL_KPIS = [
    ("Cure rate",         ["Cured"],                   True),
    ("Forward-roll rate", ["Rolled Forward", "Default"], False),
    ("Default rate",      ["Default"],                 False),
    ("Stable/Current",    ["Stable", "Current"],       True),
]


def compute_roll_rate_cards(frame: pd.DataFrame) -> list:
    dates, _ = _roll_months(frame)
    latest = dates[-1]
    prev = dates[-2] if len(dates) >= 2 else None
    cur_df = frame[frame["month_date"] == latest]
    prev_df = frame[frame["month_date"] == prev] if prev is not None else None
    sub = f"% of accounts · {pd.Timestamp(latest).strftime('%b %Y')}"

    cards = []
    for label, moves, higher_good in _ROLL_KPIS:
        v = _move_share(cur_df, moves)
        d = (v - _move_share(prev_df, moves)) if prev_df is not None else None
        if d is None:
            cls = "neutral"
        else:
            good = d >= 0 if higher_good else d <= 0
            cls = "neutral" if abs(d) < 0.05 else ("pos" if good else "neg")
        cards.append({
            "label": label,
            "value": f"{v:.1f}%",
            "delta": (f"{'+' if d >= 0 else ''}{d:.1f}pp") if d is not None else None,
            "delta_cls": cls,
            "sub": sub,
        })
    return cards


def build_roll_rate_charts(frame: pd.DataFrame) -> dict:
    dates, labels = _roll_months(frame)
    movements = _ordered(frame["movement"].dropna().unique().tolist(), MOVEMENT_ORDER)

    def share_series(moves):
        out = []
        for d in dates:
            mdf = frame[frame["month_date"] == d]
            out.append(round(_move_share(mdf, moves), 2))
        return out

    composition = [{"label": mv, "data": share_series([mv])} for mv in movements]
    return {
        "months": labels,
        "composition": composition,
        "cure":    share_series(["Cured"]),
        "forward": share_series(["Rolled Forward", "Default"]),
        "default": share_series(["Default"]),
        "stable":  share_series(["Stable", "Current"]),
    }


# ── numeric metrics bundle (for the newsletter) ─────────────────────────────
def _in_term_values(frame, month):
    sub = frame[(frame["delinquency_bucket"].isin(IN_TERM_BUCKETS)) &
                (frame["transaction_month"] == month)]
    net, oa, idue = sub["net_receipts"].sum(), sub["opening_arrears"].sum(), sub["instalment_due"].sum()
    eff, inst = sub["effort_collections"].sum(), sub["instalment_collections"].sum()
    pay, loans, den = sub["payers"].sum(), sub["loan_count"].sum(), (oa + idue)
    return {
        "collection_rate": net / den * 100 if den else 0.0,
        "effort_yield":    eff / den * 100 if den else 0.0,
        "auto_collect":    inst / net * 100 if net else 0.0,
        "payer_rate":      pay / loans * 100 if loans else 0.0,
    }


def _seg_metric(frame, seg, col, scale=1.0):
    s = frame[frame["delinquency_segment"] == seg].set_index("transaction_month")[col].sort_index()
    def at(i):
        try:
            v = s.iloc[i]
            return round(float(v) * scale, 4) if pd.notna(v) else None
        except IndexError:
            return None
    latest, prev = at(-1), at(-2)
    yoy = at(-13) if len(s) >= 13 else None
    return {
        "latest": latest,
        "mom": round(latest - prev, 4) if latest is not None and prev is not None else None,
        "yoy": round(latest - yoy, 4) if latest is not None and yoy is not None else None,
    }


def build_metrics_bundle(frame, oot_frame, roll_frame, targets, period_label, generated_at) -> dict:
    months = _months(frame)
    latest, prev = months[-1], (months[-2] if len(months) >= 2 else None)
    cur_v = _in_term_values(frame, latest)
    prev_v = _in_term_values(frame, prev) if prev is not None else None

    it_specs = [("collection_rate", "Collection Rate"), ("effort_yield", "Effort Yield"),
                ("auto_collect", "Auto Collect %"), ("payer_rate", "Payer Rate")]
    it_cards = []
    for key, label in it_specs:
        v = cur_v[key]
        tgt = float(targets.get(key, 0.0))
        it_cards.append({
            "key": key, "label": label, "unit": "%", "value": round(v, 1),
            "target": round(tgt, 1), "delta_target": round(v - tgt, 1),
            "meets_target": bool(v >= tgt),
            "mom": round(v - prev_v[key], 1) if prev_v is not None else None,
        })

    seg_present = _ordered(frame["delinquency_segment"].dropna().unique().tolist(), SEGMENT_ORDER)
    it_segments = [{
        "segment": seg,
        "yield_pct":      _seg_metric(frame, seg, "collection_yield_pct"),
        "collections_rm": _seg_metric(frame, seg, "net_receipts", scale=1e-6),
        "payer_rate_pct": _seg_metric(frame, seg, "payer_rate_pct"),
    } for seg in seg_present]

    # OOT cards (numeric)
    of = oot_frame.sort_values("transaction_month").reset_index(drop=True)
    ocur, oprev = of.iloc[-1], (of.iloc[-2] if len(of) >= 2 else None)
    def omom(col):
        return round(float(ocur[col] - oprev[col]), 4) if oprev is not None else None
    oot_cards = [
        {"label": "OOT Collected", "unit": "Rm", "value": round(ocur["total_collections"] / 1e6, 2),
         "mom": round(omom("total_collections") / 1e6, 2) if omom("total_collections") is not None else None,
         "avg3": round(float(ocur["collections_rm_3m"]), 2)},
        {"label": "OOT Book Yield", "unit": "%", "value": round(float(ocur["yield_pct"]), 2),
         "mom": round(float(ocur["yield_pct"] - oprev["yield_pct"]), 2) if oprev is not None else None,
         "avg3": round(float(ocur["yield_pct_3m"]), 2)},
        {"label": "OOT Payers", "unit": "n", "value": int(ocur["active_payers"]),
         "mom": int(omom("active_payers")) if omom("active_payers") is not None else None, "avg3": None},
        {"label": "OOT Accounts", "unit": "n", "value": int(ocur["loan_count"]),
         "mom": int(omom("loan_count")) if omom("loan_count") is not None else None, "avg3": None},
    ]

    # Roll-rate cards (numeric) + matrix-derived facts
    rdates, _ = _roll_months(roll_frame)
    r_latest, r_prev = rdates[-1], (rdates[-2] if len(rdates) >= 2 else None)
    rcur = roll_frame[roll_frame["month_date"] == r_latest]
    rprev = roll_frame[roll_frame["month_date"] == r_prev] if r_prev is not None else None
    roll_cards = []
    for label, moves, higher_good in _ROLL_KPIS:
        v = _move_share(rcur, moves)
        mom = (v - _move_share(rprev, moves)) if rprev is not None else None
        roll_cards.append({"label": label, "unit": "%", "value": round(v, 1),
                           "mom": round(mom, 1) if mom is not None else None,
                           "higher_good": higher_good})

    worst = max(roll_frame["dpd_at_start_of_month"].unique(), key=lambda l: int(str(l).split(".", 1)[0]))
    wsub = roll_frame[roll_frame["dpd_at_start_of_month"] == worst]
    wopen = wsub["loan_count"].sum()
    wstay = wsub[wsub["dpd_at_end_of_month"] == worst]["loan_count"].sum()
    roll_facts = {
        "worst_band": _pretty_dpd(worst),
        "default_stickiness_pct": round(wstay / wopen * 100, 1) if wopen else None,
    }

    return {
        "period": period_label,
        "generated_at": generated_at,
        "latest_month": pd.Timestamp(latest).strftime("%B %Y"),
        "in_term": {"cards": it_cards, "segments": it_segments},
        "oot": {"cards": oot_cards},
        "roll": {"cards": roll_cards, "facts": roll_facts},
    }


# ── render ────────────────────────────────────────────────────────────────--
def render_dashboard(brand, cards, chart_data, table, oot_cards, oot_chart_data,
                     roll_cards, roll_matrix, roll_chart_data,
                     logo_b64, period_label, generated_at) -> str:
    env = Environment(loader=FileSystemLoader(str(ASSETS_DIR)), autoescape=False)
    template = env.get_template("dashboard_template.html")
    return template.render(
        brand=brand,
        cards=cards,
        chart_data_json=json.dumps(chart_data),
        table=table,
        oot_cards=oot_cards,
        oot_chart_data_json=json.dumps(oot_chart_data),
        roll_cards=roll_cards,
        roll_matrix=roll_matrix,
        roll_chart_data_json=json.dumps(roll_chart_data),
        logo_b64=logo_b64 or "",
        period_label=period_label,
        generated_at=generated_at,
    )


def main():
    argparse.ArgumentParser(description="Generate Internal Collections dashboard").parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("Error: DATABASE_URL not set in .env", file=sys.stderr)
        sys.exit(1)

    print("Connecting to database...")
    engine = create_engine(db_url)

    print("Fetching internal collections data...")
    frame = get_segment_frame(engine)
    if frame.empty:
        print("Warning: query returned no data")
        sys.exit(1)

    targets = load_targets()

    print("Computing summary cards...")
    cards = compute_cards(frame, targets)

    print("Building chart data...")
    chart_data = build_chart_data(frame)

    print("Building segment table...")
    table = build_segment_table(frame)

    print("Fetching out-of-term recoveries data...")
    oot_frame = get_oot_frame(engine)
    if oot_frame.empty:
        print("Warning: out-of-term query returned no data")
        sys.exit(1)

    print("Computing OOT cards and charts...")
    oot_cards = compute_oot_cards(oot_frame, targets)
    oot_chart_data = build_oot_chart_data(oot_frame)

    print("Fetching roll-rate (DPD migration) data...")
    roll_frame = get_roll_rates_frame(engine)
    if roll_frame.empty:
        print("Warning: roll-rate query returned no data")
        sys.exit(1)

    print("Computing roll-rate matrix, cards and charts...")
    roll_cards = compute_roll_rate_cards(roll_frame)
    roll_matrix = build_roll_rate_matrix(roll_frame)
    roll_chart_data = build_roll_rate_charts(roll_frame)

    brand    = normalize_brand_colors(load_brand())
    logo_b64 = encode_logo(brand.get("logo", "")) if brand.get("logo") else None

    latest = pd.Timestamp(_months(frame)[-1])
    period_label = f"Latest reporting month: {latest.strftime('%B %Y')}"
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")

    print("Rendering dashboard...")
    html = render_dashboard(brand, cards, chart_data, table, oot_cards, oot_chart_data,
                            roll_cards, roll_matrix, roll_chart_data,
                            logo_b64, period_label, generated_at)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_path = OUTPUT_DIR / "dashboard.html"
    report_path    = OUTPUT_DIR / "report.json"

    dashboard_path.write_text(html, encoding="utf-8")
    report_path.write_text(json.dumps({
        "period":       period_label,
        "generated_at": generated_at,
        "in_term_cards": {c["label"]: c["value"] for c in cards},
        "oot_cards":     {c["label"]: c["value"] for c in oot_cards},
        "roll_cards":    {c["label"]: c["value"] for c in roll_cards},
    }, indent=2), encoding="utf-8")

    # Numeric metrics bundle consumed by the newsletter generator.
    metrics = build_metrics_bundle(frame, oot_frame, roll_frame, targets, period_label, generated_at)
    (OUTPUT_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    print(f"\nDashboard written to: {dashboard_path}")
    print(f"Report JSON written to: {report_path}")


if __name__ == "__main__":
    main()
