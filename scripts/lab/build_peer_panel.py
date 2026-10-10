"""Build data/lab/peer_quarterly.csv and app/static/data/peer_recovery.json: the fare clock pooled over three airlines.

Step 1 (only when the local stores are present): write the small tracked panel
data/lab/peer_quarterly.csv and its manifest from
- data/lab/ual_quarterly.csv and data/lab/dal_quarterly.csv (earnings releases, tracked),
- data/history/<snapshot>/facts.parquet (SEC XBRL facts; American's revenue and fuel),
- data/operating_history/<snapshot>/monthly.parquet (BTS T-100; American's mainline seat miles).
The two stores are not in git. Without them the tracked panel is left as it is.

Step 2: estimate everything from the tracked panel alone and write the output
JSON. No network. For fixed inputs and seed the output is identical apart from
`generated_at`.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import peer_panel

HISTORY = ROOT / "data" / "history"
OPERATING = ROOT / "data" / "operating_history"
UAL_CSV = ROOT / "data" / "lab" / "ual_quarterly.csv"
DAL_CSV = ROOT / "data" / "lab" / "dal_quarterly.csv"
AMERICAN_FIRST = "2014-03-31"          # first full quarter after the US Airways merger closed (December 2013)
AMERICAN_MAINLINE_FUEL_LAST = "2019-12-31"   # last quarter whose fuel line covered mainline flying only
AMERICAN_FIRST_FILED_CUTOFF = "2020-12-31"   # facts filed by this date predate the move of regional fuel into the line
RULES = [
    "United: total revenue, fuel expense and ASM as printed in its earnings releases (data/lab/ual_quarterly.csv).",
    "Delta: adjusted total revenue (third-party refinery sales removed) and adjusted fuel expense as printed, and "
    "ASM, from its earnings releases (data/lab/dal_quarterly.csv); the GAAP columns are not used. Adjusted revenue is "
    "not printed for 2017Q4, so that cell is blank.",
    f"American: rows start at {AMERICAN_FIRST}, the first full quarter after the merger with US Airways.",
    "American revenue: SEC XBRL operating revenue, latest filing; a quarter not reported directly is the difference "
    "of two year-to-date facts (app/financial_history.quarter_fact).",
    f"American fuel: SEC XBRL FuelCosts. Through {AMERICAN_MAINLINE_FUEL_LAST} the value as first filed (facts filed "
    f"by {AMERICAN_FIRST_FILED_CUTOFF}), which covers mainline flying only; later filings restate only some periods to "
    "include regional fuel, and mixing them breaks the fourth quarter of 2019. From 2020 the latest filing, which "
    "covers all flying.",
    "fuel_scope_factor: 1 where the fuel figure covers all flying. For American through 2019 it is fiscal 2019 fuel "
    "as restated in a later 10-K over fiscal 2019 fuel as first filed, both in the store; multiplying by it brings "
    "the mainline-only figure to consolidated scope under the assumption of a constant regional share.",
    "American ASM: BTS T-100 scheduled passenger seat miles of operating carrier AA, plus US (US Airways) while it "
    "reported separately, summed over months; a quarter needs all three months. Mainline only: regional partners "
    "and some international flying are not included.",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _number(value: float) -> str:
    return f"{round(float(value), 3):.3f}".rstrip("0").rstrip(".")


def stores() -> dict | None:
    """Paths and identifiers of the two local stores, or None when either is absent."""
    found = {}
    for name, root, parquet in (("sec_xbrl", HISTORY, "facts.parquet"), ("bts_t100", OPERATING, "monthly.parquet")):
        latest = root / "latest.json"
        if not latest.is_file():
            return None
        snapshot = json.loads(latest.read_text())["snapshot_id"]
        path = root / snapshot / parquet
        if not path.is_file():
            return None
        manifest = json.loads((root / snapshot / "manifest.json").read_text())
        found[name] = {"path": path, "snapshot_id": snapshot, "cutoff": manifest.get("cutoff"), "manifest": manifest}
    return found


def release_rows(carrier: str, path: Path, revenue: str, fuel: str) -> list[dict]:
    with open(path, newline="") as stream:
        rows = list(csv.DictReader(stream))
    name = path.relative_to(ROOT).as_posix()
    return [{"carrier": carrier, "period_end": r["period_end"], "total_revenue_musd": r[revenue].strip(),
             "fuel_expense_musd": r[fuel].strip(), "asm_m": r["asm_m"].strip(), "fuel_scope_factor": "1",
             "revenue_source": f"{name}:{revenue}" if r[revenue].strip() else "",
             "fuel_source": f"{name}:{fuel}" if r[fuel].strip() else "",
             "asm_source": f"{name}:asm_m" if r["asm_m"].strip() else ""} for r in rows]


def american_rows(found: dict) -> tuple[list[dict], dict]:
    """American from the two stores: consolidated revenue and fuel from XBRL facts, mainline seat miles from BTS."""
    import duckdb
    from app import financial_history as fh

    con = duckdb.connect()
    try:
        result = con.execute("SELECT * FROM read_parquet(?) WHERE entity = 'AAL' AND variable IN "
                             "('operating_revenue', 'fuel_expense')", [str(found["sec_xbrl"]["path"])])
        names = [c[0] for c in result.description]
        facts = [dict(zip(names, (v.isoformat() if hasattr(v, "isoformat") else v for v in row)))
                 for row in result.fetchall()]
        months = con.execute("SELECT row.period, row.carrier, SUM(row.asm_million) FROM read_parquet(?) "
                             "WHERE row.carrier IN ('AA', 'US') GROUP BY 1, 2 ORDER BY 1, 2",
                             [str(found["bts_t100"]["path"])]).fetchall()
    finally:
        con.close()
    cutoff = found["sec_xbrl"]["cutoff"]
    seat_miles = {}
    for period, code, asm in months:
        year, month = int(period[:4]), int(period[5:7])
        end = fh.quarter_dates(year, (month - 1) // 3 + 1)[1]
        seat_miles.setdefault(end, {}).setdefault(code, []).append(asm)
    annual = lambda limit: fh.select_fact(facts, "fuel_expense", "2019-01-01", "2019-12-31", limit)
    first_filed, restated = annual(AMERICAN_FIRST_FILED_CUTOFF), annual(cutoff)
    if not first_filed or not restated or restated["value_musd"] <= first_filed["value_musd"]:
        raise ValueError("The store does not hold fiscal 2019 fuel both as first filed and as restated")
    factor = restated["value_musd"] / first_filed["value_musd"]
    rows = []
    for end in sorted(e for e in seat_miles if e >= AMERICAN_FIRST):
        start = end[:5] + f"{int(end[5:7]) - 2:02d}-01"
        codes = seat_miles[end]
        if len(codes.get("AA", [])) != 3 or ("US" in codes and len(codes["US"]) != 3):
            continue   # an incomplete quarter is left out, never scaled up
        mainline = end <= AMERICAN_MAINLINE_FUEL_LAST
        revenue = fh.quarter_fact(facts, "operating_revenue", start, end, cutoff)
        fuel = fh.quarter_fact(facts, "fuel_expense", start, end, AMERICAN_FIRST_FILED_CUTOFF if mainline else cutoff)
        if revenue is None or fuel is None:
            continue
        rows.append({"carrier": "AAL", "period_end": end, "total_revenue_musd": _number(revenue["value_musd"]),
                     "fuel_expense_musd": _number(fuel["value_musd"]), "asm_m": _number(sum(sum(v) for v in codes.values())),
                     "fuel_scope_factor": f"{factor:.6f}" if mainline else "1",
                     "revenue_source": f"sec_xbrl:operating_revenue:{revenue['method']}",
                     "fuel_source": "sec_xbrl:FuelCosts:" + ("as_first_filed_mainline_only:" if mainline else
                                                             "latest_filing_all_flying:") + fuel["method"],
                     "asm_source": "bts_t100:" + "+".join(sorted(codes)) + ":mainline_scheduled_passenger"})
    scope = {"fiscal_2019_fuel_as_first_filed_musd": first_filed["value_musd"],
             "fiscal_2019_fuel_as_first_filed_filed_on": first_filed["published_on"],
             "fiscal_2019_fuel_restated_musd": restated["value_musd"],
             "fiscal_2019_fuel_restated_filed_on": restated["published_on"], "factor": round(factor, 6),
             "fact_ids": [first_filed["fact_id"], restated["fact_id"]]}
    return rows, scope


def build_tracked_panel(csv_path: Path, manifest_path: Path) -> dict | None:
    """Write the tracked panel and its manifest from the stores; None (and nothing written) when a store is absent."""
    found = stores()
    if found is None:
        return None
    american, scope = american_rows(found)
    rows = (release_rows("UAL", UAL_CSV, "total_revenue_musd", "fuel_expense_musd")
            + release_rows("DAL", DAL_CSV, "adj_total_revenue_musd", "adj_fuel_expense_musd") + american)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=peer_panel.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    coverage = {}
    for carrier in peer_panel.CARRIERS:
        mine = [r["period_end"] for r in rows if r["carrier"] == carrier]
        coverage[carrier] = {"rows": len(mine), "first": min(mine), "last": max(mine)}
    manifest = {
        "area": "peer_panel", "builder": "scripts/lab/build_peer_panel.py",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output": {"path": csv_path.relative_to(ROOT).as_posix(), "sha256": _sha(csv_path), "rows": len(rows)},
        "tracked_inputs": [{"path": p.relative_to(ROOT).as_posix(), "sha256": _sha(p)} for p in (UAL_CSV, DAL_CSV)],
        "stores": {name: {"path": s["path"].relative_to(ROOT).as_posix(), "snapshot_id": s["snapshot_id"],
                          "sha256": _sha(s["path"]), "cutoff": s["cutoff"], "tracked": False,
                          "version": s["manifest"].get("version")} for name, s in found.items()},
        "sec_sources": [{k: s.get(k) for k in ("entity", "source_url", "sha256", "retrieved_at_utc")}
                        for s in found["sec_xbrl"]["manifest"].get("sources", []) if s.get("entity") == "AAL"],
        "bts_boundary": found["bts_t100"]["manifest"].get("boundary"),
        "american_fuel_scope": scope, "coverage": coverage, "rules": RULES}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.is_file():   # an unchanged panel keeps its first build time, so a rerun leaves the manifest alone
        previous = json.loads(manifest_path.read_text())
        if {**previous, "built_at": None} == {**json.loads(json.dumps(manifest)), "built_at": None}:
            manifest["built_at"] = previous["built_at"]
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def write(payload: dict, path: Path) -> None:
    peer_panel.validate(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, allow_nan=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=peer_panel.SEED)
    parser.add_argument("--draws", type=int, default=peer_panel.N_DRAWS, help="joint draws of the curve (at least 500)")
    parser.add_argument("--panel", type=Path, default=peer_panel.PANEL_CSV)
    parser.add_argument("--fare-clock", type=Path, default=peer_panel.FARE_CLOCK_JSON)
    parser.add_argument("--demand", type=Path, default=peer_panel.DEMAND_JSON)
    parser.add_argument("--out", type=Path, default=peer_panel.OUTPUT)
    parser.add_argument("--skip-panel", action="store_true", help="do not rewrite the tracked panel from the stores")
    parser.add_argument("--generated-at", default=None, help="fixed UTC timestamp, for reproducible bytes")
    args = parser.parse_args()
    manifest = None
    if not args.skip_panel and args.panel == peer_panel.PANEL_CSV:
        manifest = build_tracked_panel(args.panel, peer_panel.MANIFEST_JSON)
    payload = peer_panel.build(panel_path=args.panel, fare_clock_path=args.fare_clock, demand_path=args.demand,
                               seed=args.seed, n_draws=args.draws, generated_at=args.generated_at)
    write(payload, args.out)
    pooled = payload["pooled"]
    print(json.dumps({"out": str(args.out), "as_of": payload["as_of"],
                      "panel": "rebuilt from the local stores" if manifest else "tracked file used as it is",
                      "headline": payload["headline"], "n_rows": pooled["n_rows"],
                      "n_quarters_effective": pooled["n_quarters_effective"],
                      "one_year_share": pooled["one_year_share"], "half_life_quarters": pooled["half_life_quarters"],
                      "half_life_range": pooled["half_life_range"], "inputs": payload["inputs"]}, indent=2))


if __name__ == "__main__":
    main()
