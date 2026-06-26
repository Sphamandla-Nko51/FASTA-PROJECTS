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
from insights import candidate_facts, split, fmt_value, fmt_delta

load_dotenv()

ROOT = Path(__file__).parent.parent
ASSETS_DIR = ROOT / "assets"
OUTPUT_DIR = ROOT / "output"
MODEL = "claude-sonnet-4-6"


def build_insights(m: dict) -> dict:
    """Global highlights/lowlights for the newsletter (shared engine)."""
    highlights, lowlights = split(candidate_facts(m), n=6)
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
            f"({fmt_delta(cr['mom'], '%', arrow=False)} MoM). "
            f"{len(insights['highlights'])} highlight(s) and {len(insights['lowlights'])} lowlight(s) this month.")


# ── metric tables ─────────────────────────────────────────────────────────---
GREEN, RED, GREY = "#047857", "#b91c1c", "#6b7a99"


def _tables(m: dict) -> dict:
    in_term = []
    for c in m["in_term"]["cards"]:
        color = GREEN if c["meets_target"] else RED
        in_term.append({"label": c["label"], "value": fmt_value(c["value"], c["unit"]),
                        "delta": f"tgt {fmt_value(c['target'], c['unit'])} ({fmt_delta(c['delta_target'], c['unit'])})",
                        "color": color})

    def mom_color(delta, higher_good=True):
        if delta is None or delta == 0:
            return GREY
        up = delta > 0
        return GREEN if (up == higher_good) else RED

    roll = []
    for c in m["roll"]["cards"]:
        roll.append({"label": c["label"], "value": fmt_value(c["value"], "%"),
                     "delta": fmt_delta(c["mom"], "%"),
                     "color": mom_color(c["mom"], c["higher_good"])})
    return {"in_term": in_term, "roll": roll}


# ── render + eml ─────────────────────────────────────────────────────────────
def render(brand, logo_b64, m, exec_summary, highlights, lowlights, ai, tables) -> str:
    env = Environment(loader=FileSystemLoader(str(ASSETS_DIR)), autoescape=False)
    return env.get_template("newsletter_template.html").render(
        brand=brand, logo_b64=logo_b64 or "", metrics=m,
        exec_summary=exec_summary, highlights=highlights, lowlights=lowlights,
        ai_narrative=ai, tbl_in_term=tables["in_term"], tbl_roll=tables["roll"],
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
