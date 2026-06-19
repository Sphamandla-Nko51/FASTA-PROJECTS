"""Generate the collections newsletter (highlights & lowlights) from the
dashboard's numeric metrics bundle.

Hybrid narrative: deterministic rules select & rank the facts (exact numbers),
then Claude polishes them into prose. Falls back to the rule-based text if no
ANTHROPIC_API_KEY is set or the API call fails.

File-only: writes output/newsletter.html and output/newsletter.eml. Nothing is
sent. (A real send would need SMTP/API creds and explicit confirmation.)
"""
import os
import re
import sys
import json
import argparse
from pathlib import Path
from email.message import EmailMessage
from email.utils import formatdate

from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).parent))
from generate_dashboard import load_brand, normalize_brand_colors, encode_logo

load_dotenv()

ROOT = Path(__file__).parent.parent
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "output"
MODEL = "claude-sonnet-4-6"

# Minimum movement to be "notable" (in pp for %-metrics, or % change otherwise).
THRESHOLD = 1.0


# ── formatting ───────────────────────────────────────────────────────────────
def _fmt_value(value, unit):
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "Rm":
        return f"R{value:.2f}m"
    return f"{int(value):,}"


def _fmt_delta(delta, unit, arrow=True):
    if delta is None:
        return "—"
    sign = "+" if delta >= 0 else "−"
    a = abs(delta)
    if unit == "%":
        body = f"{sign}{a:.1f}pp"
    elif unit == "Rm":
        body = f"{sign}R{a:.2f}m"
    else:
        body = f"{sign}{int(round(a)):,}"
    tip = (" ▲" if delta > 0 else " ▼" if delta < 0 else "") if arrow else ""
    return body + tip


def _score(delta, value, unit):
    """Normalise a movement to comparable units: pp directly, else % change."""
    if delta is None:
        return 0.0
    if unit == "%":
        return abs(delta)
    base = abs(value) if value else 0.0
    return abs(delta) / base * 100 if base else 0.0


# ── deterministic fact selection ─────────────────────────────────────────────
def build_insights(m: dict) -> dict:
    facts = []  # {key, area, text, score, good}

    def add(key, area, text, score, good):
        facts.append({"key": key, "area": area, "text": text, "score": score, "good": good})

    # In-term cards: target beat/miss + MoM (all higher-is-better)
    for c in m["in_term"]["cards"]:
        v, u, lbl = c["value"], c["unit"], c["label"]
        dt = c["delta_target"]
        if abs(dt) >= 0.1:
            verb = "beat" if c["meets_target"] else "missed"
            add(f"it:{c['key']}:tgt", "In-Term",
                f"{lbl} {verb} target — {_fmt_value(v, u)} vs {_fmt_value(c['target'], u)} target ({_fmt_delta(dt, u, arrow=False)}).",
                abs(dt), c["meets_target"])
        if c["mom"] is not None and abs(c["mom"]) >= THRESHOLD:
            add(f"it:{c['key']}:mom", "In-Term",
                f"{lbl} {'rose' if c['mom'] > 0 else 'fell'} {_fmt_delta(c['mom'], u, arrow=False)} MoM to {_fmt_value(v, u)}.",
                abs(c["mom"]), c["mom"] > 0)

    # OOT cards (Collected/Yield/Payers higher-good; Accounts neutral)
    oot_dir = {"OOT Collected": True, "OOT Book Yield": True, "OOT Payers": True}
    for c in m["oot"]["cards"]:
        if c["label"] not in oot_dir or c["mom"] is None:
            continue
        sc = _score(c["mom"], c["value"], c["unit"])
        if sc >= THRESHOLD:
            add(f"oot:{c['label']}", "Out-of-Term",
                f"{c['label']} {'up' if c['mom'] > 0 else 'down'} {_fmt_delta(c['mom'], c['unit'], arrow=False)} MoM to {_fmt_value(c['value'], c['unit'])}.",
                sc, c["mom"] > 0)

    # Roll-rate cards (direction per higher_good)
    for c in m["roll"]["cards"]:
        if c["mom"] is None or abs(c["mom"]) < THRESHOLD:
            continue
        good = (c["mom"] > 0) if c["higher_good"] else (c["mom"] < 0)
        add(f"roll:{c['label']}", "Roll Rates",
            f"{c['label']} {'up' if c['mom'] > 0 else 'down'} {_fmt_delta(c['mom'], '%', arrow=False)} MoM to {_fmt_value(c['value'], '%')}.",
            abs(c["mom"]), good)

    # In-term segment movers: yield + payer-rate MoM (higher-good).
    # Skip MPM2 (sparse out-of-term remnant — a handful of loans yields garbage
    # rates) and guard against implausible values from tiny denominators.
    for s in m["in_term"]["segments"]:
        if s["segment"] == "MPM2":
            continue
        for metric, u, nice in [("yield_pct", "%", "yield"), ("payer_rate_pct", "%", "payer rate")]:
            mom, latest = s[metric]["mom"], s[metric]["latest"]
            if mom is None or latest is None:
                continue
            if abs(mom) < max(THRESHOLD, 1.5) or abs(latest) > 110 or abs(mom) > 80:
                continue
            add(f"seg:{s['segment']}:{metric}", "In-Term",
                f"{s['segment']} {nice} {'up' if mom > 0 else 'down'} {_fmt_delta(mom, u, arrow=False)} MoM to {_fmt_value(latest, u)}.",
                abs(mom), mom > 0)

    # Dedupe per key keeping the strongest signal, then split + rank.
    best = {}
    for f in facts:
        if f["key"] not in best or f["score"] > best[f["key"]]["score"]:
            best[f["key"]] = f
    ranked = sorted(best.values(), key=lambda f: f["score"], reverse=True)
    highlights = [f for f in ranked if f["good"]][:6]
    lowlights = [f for f in ranked if not f["good"]][:6]
    return {"highlights": highlights, "lowlights": lowlights}


# ── Claude narrative (hybrid) ────────────────────────────────────────────────
def narrate(m: dict, insights: dict):
    """Return (exec_summary, highlight_lines, lowlight_lines) from Claude, or
    None on missing key / any failure (caller falls back to deterministic text)."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("No ANTHROPIC_API_KEY — using deterministic narrative.")
        return None
    try:
        import anthropic
        facts = {
            "month": m["latest_month"],
            "highlights": [f["text"] for f in insights["highlights"]],
            "lowlights": [f["text"] for f in insights["lowlights"]],
        }
        prompt = (
            "You are writing a monthly collections newsletter for colleagues. "
            "Using ONLY the facts below (do not invent or alter any number), return STRICT JSON with keys "
            "'exec_summary' (2-3 sentence plain-English overview), "
            "'highlights' (array, one polished sentence per input highlight, same order), and "
            "'lowlights' (array, one polished sentence per input lowlight, same order). "
            "Keep each bullet to one crisp sentence; preserve every figure exactly as given.\n\n"
            f"FACTS:\n{json.dumps(facts, indent=2)}"
        )
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        hi = data.get("highlights") or [f["text"] for f in insights["highlights"]]
        lo = data.get("lowlights") or [f["text"] for f in insights["lowlights"]]
        return data.get("exec_summary", ""), hi, lo
    except Exception as e:  # noqa: BLE001 — any failure → deterministic fallback
        print(f"Claude narrative failed ({e}); using deterministic narrative.")
        return None


def _deterministic_summary(m: dict, insights: dict) -> str:
    cr = next(x for x in m["in_term"]["cards"] if x["key"] == "collection_rate")
    return (f"In-term collection rate is {cr['value']:.1f}% for {m['latest_month']} "
            f"({_fmt_delta(cr['mom'], '%', arrow=False)} MoM). "
            f"{len(insights['highlights'])} highlight(s) and {len(insights['lowlights'])} lowlight(s) this month.")


# ── metric tables ─────────────────────────────────────────────────────────---
GREEN, RED, GREY = "#047857", "#b91c1c", "#6b7a99"


def _tables(m: dict) -> dict:
    in_term = []
    for c in m["in_term"]["cards"]:
        color = GREEN if c["meets_target"] else RED
        in_term.append({"label": c["label"], "value": _fmt_value(c["value"], c["unit"]),
                        "delta": f"tgt {_fmt_value(c['target'], c['unit'])} ({_fmt_delta(c['delta_target'], c['unit'])})",
                        "color": color})

    def mom_color(delta, higher_good=True):
        if delta is None or delta == 0:
            return GREY
        up = delta > 0
        return GREEN if (up == higher_good) else RED

    oot = []
    oot_dir = {"OOT Collected": True, "OOT Book Yield": True, "OOT Payers": True, "OOT Accounts": True}
    for c in m["oot"]["cards"]:
        oot.append({"label": c["label"], "value": _fmt_value(c["value"], c["unit"]),
                    "delta": _fmt_delta(c["mom"], c["unit"]),
                    "color": mom_color(c["mom"], oot_dir.get(c["label"], True))})

    roll = []
    for c in m["roll"]["cards"]:
        roll.append({"label": c["label"], "value": _fmt_value(c["value"], "%"),
                     "delta": _fmt_delta(c["mom"], "%"),
                     "color": mom_color(c["mom"], c["higher_good"])})
    return {"in_term": in_term, "oot": oot, "roll": roll}


# ── render + eml ─────────────────────────────────────────────────────────────
def render(brand, logo_b64, m, exec_summary, highlights, lowlights, ai, tables) -> str:
    env = Environment(loader=FileSystemLoader(str(ASSETS_DIR)), autoescape=False)
    return env.get_template("newsletter_template.html").render(
        brand=brand, logo_b64=logo_b64 or "", metrics=m,
        exec_summary=exec_summary, highlights=highlights, lowlights=lowlights,
        ai_narrative=ai, tbl_in_term=tables["in_term"], tbl_oot=tables["oot"], tbl_roll=tables["roll"],
    )


def write_eml(html, m, subject, recipients, dashboard_path: Path, out: Path):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("NEWSLETTER_FROM", "collections-dashboard@localhost")
    if recipients:
        msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("This newsletter is best viewed as HTML. The interactive dashboard is attached.")
    msg.add_alternative(html, subtype="html")
    if dashboard_path.exists():
        msg.add_attachment(dashboard_path.read_bytes(), maintype="text", subtype="html",
                           filename="dashboard.html")
    out.write_bytes(msg.as_bytes())


def main():
    argparse.ArgumentParser(description="Generate the collections newsletter (file-only)").parse_args()

    metrics_path = OUTPUT_DIR / "metrics.json"
    if not metrics_path.exists():
        print("Error: output/metrics.json not found — run `python tools/generate_dashboard.py` first.",
              file=sys.stderr)
        sys.exit(1)
    m = json.loads(metrics_path.read_text())

    insights = build_insights(m)
    narrated = narrate(m, insights)
    if narrated:
        exec_summary, highlights, lowlights = narrated
        ai = True
    else:
        exec_summary = _deterministic_summary(m, insights)
        highlights = [f["text"] for f in insights["highlights"]]
        lowlights = [f["text"] for f in insights["lowlights"]]
        ai = False

    brand = normalize_brand_colors(load_brand())
    logo_b64 = encode_logo(brand.get("logo", "")) if brand.get("logo") else None
    tables = _tables(m)
    html = render(brand, logo_b64, m, exec_summary, highlights, lowlights, ai, tables)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "newsletter.html").write_text(html, encoding="utf-8")

    # Optional recipients (only used to pre-fill the .eml To: header — never sent).
    rec_path = ASSETS_DIR / "recipients.json"
    recipients, subject = [], f"Collections Monthly — {m['latest_month']}"
    if rec_path.exists():
        cfg = json.loads(rec_path.read_text())
        recipients = cfg.get("to", []) or []
        subject = (cfg.get("subject") or subject).replace("{period}", m["latest_month"])
    write_eml(html, m, subject, recipients, OUTPUT_DIR / "dashboard.html", OUTPUT_DIR / "newsletter.eml")

    print(f"Newsletter written to: {OUTPUT_DIR / 'newsletter.html'}")
    print(f"Email file written to:  {OUTPUT_DIR / 'newsletter.eml'} ({'AI' if ai else 'rule-based'} narrative)")
    print(f"Highlights: {len(highlights)} · Lowlights: {len(lowlights)}"
          + (f" · recipients pre-filled: {len(recipients)}" if recipients else " · no recipients set"))


if __name__ == "__main__":
    main()
