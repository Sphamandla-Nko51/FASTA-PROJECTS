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
    return f"R{v:.2f}m" if unit == "rm" else f"{v:.1f}%"


def _fmt_delta(d, unit):
    if d is None:
        return "—"
    if unit == "rm":
        return f"{'+' if d >= 0 else '-'}R{abs(d):.2f}m"
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
        ("Collections (Rm)", "net_receipts",         "rm",  1e-6),
        ("Yield %",          "collection_yield_pct", "pct", 1.0),
        ("Payer rate %",     "payer_rate_pct",       "pct", 1.0),
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


# ── render ────────────────────────────────────────────────────────────────--
def render_dashboard(brand, cards, chart_data, table, oot_cards, oot_chart_data,
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

    brand    = normalize_brand_colors(load_brand())
    logo_b64 = encode_logo(brand.get("logo", "")) if brand.get("logo") else None

    latest = pd.Timestamp(_months(frame)[-1])
    period_label = f"Latest reporting month: {latest.strftime('%B %Y')}"
    generated_at = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")

    print("Rendering dashboard...")
    html = render_dashboard(brand, cards, chart_data, table, oot_cards, oot_chart_data,
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
    }, indent=2), encoding="utf-8")

    print(f"\nDashboard written to: {dashboard_path}")
    print(f"Report JSON written to: {report_path}")


if __name__ == "__main__":
    main()
