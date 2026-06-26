"""Deterministic insight engine — shared by the dashboard (per-tab intelligence
panels) and the newsletter (global highlights/lowlights).

Operates purely on the numeric metrics bundle (output/metrics.json) built by
generate_dashboard.build_metrics_bundle. Figures are never invented here; this
module only selects, scores and phrases movements that already exist.
"""

# Minimum movement to be "notable" (pp for %-metrics, or % change otherwise).
THRESHOLD = 1.0

AREA_IN_TERM = "In-Term"
AREA_ROLL = "Roll Rates"


# ── formatting ───────────────────────────────────────────────────────────────
def fmt_value(value, unit):
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "Rm":
        return f"R{value:.2f}m"
    return f"{int(value):,}"


def fmt_delta(delta, unit, arrow=True):
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


def score(delta, value, unit):
    """Normalise a movement to comparable units: pp directly, else % change."""
    if delta is None:
        return 0.0
    if unit == "%":
        return abs(delta)
    base = abs(value) if value else 0.0
    return abs(delta) / base * 100 if base else 0.0


# ── candidate facts ──────────────────────────────────────────────────────────
def candidate_facts(m: dict) -> list:
    """Return a de-duplicated, score-ranked list of fact dicts:
    {key, area, text, score, good}."""
    facts = []

    def add(key, area, text, sc, good):
        facts.append({"key": key, "area": area, "text": text, "score": sc, "good": good})

    # In-term cards: target beat/miss + MoM (all higher-is-better)
    for c in m["in_term"]["cards"]:
        v, u, lbl = c["value"], c["unit"], c["label"]
        dt = c["delta_target"]
        if abs(dt) >= 0.1:
            verb = "beat" if c["meets_target"] else "missed"
            add(f"it:{c['key']}:tgt", AREA_IN_TERM,
                f"{lbl} {verb} target — {fmt_value(v, u)} vs {fmt_value(c['target'], u)} target ({fmt_delta(dt, u, arrow=False)}).",
                abs(dt), c["meets_target"])
        if c["mom"] is not None and abs(c["mom"]) >= THRESHOLD:
            add(f"it:{c['key']}:mom", AREA_IN_TERM,
                f"{lbl} {'rose' if c['mom'] > 0 else 'fell'} {fmt_delta(c['mom'], u, arrow=False)} MoM to {fmt_value(v, u)}.",
                abs(c["mom"]), c["mom"] > 0)

    # Roll-rate cards (direction per higher_good)
    for c in m["roll"]["cards"]:
        if c["mom"] is None or abs(c["mom"]) < THRESHOLD:
            continue
        good = (c["mom"] > 0) if c["higher_good"] else (c["mom"] < 0)
        add(f"roll:{c['label']}", AREA_ROLL,
            f"{c['label']} {'up' if c['mom'] > 0 else 'down'} {fmt_delta(c['mom'], '%', arrow=False)} MoM to {fmt_value(c['value'], '%')}.",
            abs(c["mom"]), good)

    # In-term segment movers (yield + payer-rate MoM, higher-good).
    # Skip MPM2 (sparse out-of-term remnant → garbage rates); guard implausibles.
    for s in m["in_term"]["segments"]:
        if s["segment"] == "MPM2":
            continue
        for metric, u, nice in [("yield_pct", "%", "yield"), ("payer_rate_pct", "%", "payer rate")]:
            mom, latest = s[metric]["mom"], s[metric]["latest"]
            if mom is None or latest is None:
                continue
            if abs(mom) < max(THRESHOLD, 1.5) or abs(latest) > 110 or abs(mom) > 80:
                continue
            add(f"seg:{s['segment']}:{metric}", AREA_IN_TERM,
                f"{s['segment']} {nice} {'up' if mom > 0 else 'down'} {fmt_delta(mom, u, arrow=False)} MoM to {fmt_value(latest, u)}.",
                abs(mom), mom > 0)

    # De-dupe per key, keep the strongest signal, rank by score.
    best = {}
    for f in facts:
        if f["key"] not in best or f["score"] > best[f["key"]]["score"]:
            best[f["key"]] = f
    return sorted(best.values(), key=lambda f: f["score"], reverse=True)


def split(facts: list, n: int = 6):
    """Global top-n good / top-n bad (used by the newsletter)."""
    highlights = [f for f in facts if f["good"]][:n]
    lowlights = [f for f in facts if not f["good"]][:n]
    return highlights, lowlights


def area_insights(facts: list, area: str, n: int = 5) -> list:
    """Top-n most significant movements for one dashboard tab (good or bad)."""
    return [f for f in facts if f["area"] == area][:n]
