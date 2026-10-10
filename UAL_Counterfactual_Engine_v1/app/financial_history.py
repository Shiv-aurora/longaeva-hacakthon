"""SEC filing-date financial reconstruction with explicit YTD differencing.

Company Facts includes standard, entity-wide taxonomy facts, not custom airline
or market disclosures. Fiscal metadata and frames are not used as period dates.
No quarterly value is divided into fabricated historical monthly observations.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
import hashlib
import json
import math
from pathlib import Path
import re

import duckdb

from .evidence import digest
from .source_store import RAW, ROOT, fetch_https, preserve_source, read_source

VERSION = "sec-financial-history-0.1.0"
HISTORY = ROOT / "data" / "history"
ENTITIES = {"UAL": 100517, "DAL": 27904, "AAL": 6201}
FLOW_TAGS = {
    "operating_revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "SalesRevenueServicesNet"),
    "operating_income": ("OperatingIncomeLoss",),
    "fuel_expense": ("FuelCosts",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "investing_cash_flow": ("NetCashProvidedByUsedInInvestingActivities",),
    "financing_cash_flow": ("NetCashProvidedByUsedInFinancingActivities",),
    "operating_cash_flow_continuing_operations": ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",),
    "investing_cash_flow_continuing_operations": ("NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",),
    "financing_cash_flow_continuing_operations": ("NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",),
    "net_income": ("NetIncomeLoss",),
    "income_tax_expense": ("IncomeTaxExpenseBenefit",),
    "interest_expense": ("InterestExpenseNonoperating", "InterestExpense"),
    "depreciation_amortization": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization"),
}
STOCK_TAGS = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "cash_including_restricted": ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",),
    "short_term_investments": ("ShortTermInvestments",),
    "assets": ("Assets",), "liabilities_and_equity": ("LiabilitiesAndStockholdersEquity",),
    "current_assets": ("AssetsCurrent",), "current_liabilities": ("LiabilitiesCurrent",),
    "accounts_payable": ("AccountsPayableCurrent",),
    "debt_and_capital_leases_current": ("LongTermDebtAndCapitalLeaseObligationsCurrent",),
    "debt_and_capital_leases_noncurrent": ("LongTermDebtAndCapitalLeaseObligations",),
    "ppe": ("PropertyPlantAndEquipmentNet",),
}


def tag_map(entity: str) -> dict:
    if entity not in ENTITIES:
        raise ValueError("Unsupported SEC entity")
    # These concepts have different definitions; preserve their names and do not
    # claim peer-comparable adjusted capex. UAL is net of equipment deposit returns.
    purchases = "PaymentsToAcquireProductiveAssets"
    return {**FLOW_TAGS, "cash_asset_purchases": (purchases,), **STOCK_TAGS}


def normalize_facts(blob: dict, entity: str, source_sha: str) -> list[dict]:
    if blob.get("cik") != ENTITIES.get(entity):
        raise ValueError("Company Facts CIK does not match the requested entity")
    taxonomy = blob.get("facts", {}).get("us-gaap", {})
    result = []
    for variable, tags in tag_map(entity).items():
        for priority, tag in enumerate(tags):
            concept = taxonomy.get(tag, {})
            for i, raw in enumerate(concept.get("units", {}).get("USD", [])):
                if raw.get("form") not in {"10-K", "10-Q", "10-K/A", "10-Q/A"}:
                    continue
                end = date.fromisoformat(raw["end"])
                filed = date.fromisoformat(raw["filed"])
                start = date.fromisoformat(raw["start"]) if "start" in raw else None
                value = raw["val"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError("SEC financial fact contains a nonfinite/non-numeric value")
                if (start and start > end) or filed < end or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", raw["accn"]):
                    raise ValueError("SEC financial fact has invalid dates or accession")
                if (variable in STOCK_TAGS) != (start is None):
                    continue  # A stock and a duration cannot be substituted.
                result.append({"fact_id": f"{source_sha}:{tag}:USD:{i}", "entity": entity,
                               "variable": variable, "tag": tag, "priority": priority,
                               "start": start.isoformat() if start else None, "end": end.isoformat(),
                               "value_musd": value / 1_000_000, "units": "USD_million",
                               "published_on": filed.isoformat(), "accession": raw["accn"],
                               "form": raw["form"], "source_sha256": source_sha,
                               "source_pointer": f"/facts/us-gaap/{tag}/units/USD/{i}",
                               "filing_url": f"https://www.sec.gov/Archives/edgar/data/{blob['cik']}/{raw['accn'].replace('-', '')}/{raw['accn']}-index.html"})
    return result


def select_fact(facts: list[dict], variable: str, start: str | None, end: str, cutoff: str) -> dict | None:
    candidates = [r for r in facts if r["variable"] == variable and r["start"] == start
                  and r["end"] == end and r["published_on"] <= cutoff]
    if not candidates:
        return None
    latest = max(r["published_on"] for r in candidates)
    candidates = [r for r in candidates if r["published_on"] == latest]
    priority = min(r["priority"] for r in candidates)
    candidates = [r for r in candidates if r["priority"] == priority]
    if len({r["value_musd"] for r in candidates}) != 1:
        raise ValueError(f"Conflicting {variable} values for {end}, filed {latest}")
    return sorted(candidates, key=lambda r: (r["accession"], r["fact_id"]))[-1]


def quarter_dates(year: int, quarter: int) -> tuple[str, str]:
    month = quarter * 3
    return f"{year:04d}-{month - 2:02d}-01", f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def partition(year: int) -> str:
    return "fit_2013_2019" if year <= 2019 else "selection_2020_2023" if year <= 2023 else "locked_test_2024_plus"


def quarter_fact(facts: list[dict], variable: str, start: str, end: str, cutoff: str) -> dict | None:
    direct = select_fact(facts, variable, start, end, cutoff)
    if direct:
        return {"value_musd": direct["value_musd"], "published_on": direct["published_on"],
                "method": "reported_quarter", "source_fact_ids": [direct["fact_id"]], "cross_filing": False}
    ytd_start = end[:4] + "-01-01"
    if start == ytd_start:
        return None
    previous_end = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    current = select_fact(facts, variable, ytd_start, end, cutoff)
    previous = select_fact(facts, variable, ytd_start, previous_end, cutoff)
    if not current or not previous:
        return None  # Missing history is never treated as zero.
    return {"value_musd": current["value_musd"] - previous["value_musd"],
            "published_on": max(current["published_on"], previous["published_on"]),
            "method": "ytd_difference", "source_fact_ids": [current["fact_id"], previous["fact_id"]],
            "cross_filing": current["accession"] != previous["accession"]}


def financial_panel(facts: list[dict], entity: str, cutoff: str, start_year: int = 2013) -> dict:
    date.fromisoformat(cutoff)
    if entity not in ENTITIES or not 2013 <= start_year <= int(cutoff[:4]):
        raise ValueError("Financial history begins in 2013; cutoff must follow start year")
    facts = [r for r in facts if r["entity"] == entity]
    rows, issues = [], []
    mapping = tag_map(entity)
    last_year = min(int(cutoff[:4]), max((int(r["end"][:4]) for r in facts), default=start_year - 1))
    for year in range(start_year, last_year + 1):
        for quarter in range(1, 5):
            start, end = quarter_dates(year, quarter)
            if end > cutoff:
                continue
            values = {}
            for variable in mapping:
                try:
                    if variable in STOCK_TAGS:
                        fact = select_fact(facts, variable, None, end, cutoff)
                        value = None if fact is None else {"value_musd": fact["value_musd"], "published_on": fact["published_on"],
                                "method": "reported_stock", "source_fact_ids": [fact["fact_id"]], "cross_filing": False}
                    else:
                        value = quarter_fact(facts, variable, start, end, cutoff)
                except ValueError as exc:
                    value = None
                    issues.append({"entity": entity, "period_end": end, "variable": variable, "issue": str(exc)})
                if value is not None:
                    values[variable] = value
            for variable, left, right in (("operating_expense", "operating_revenue", "operating_income"),
                                          ("free_cash_flow", "operating_cash_flow", "cash_asset_purchases")):
                if left in values and right in values:
                    a, b = values[left], values[right]
                    values[variable] = {"value_musd": a["value_musd"] - b["value_musd"],
                                        "published_on": max(a["published_on"], b["published_on"]),
                                        "method": f"identity:{left}-{right}", "source_fact_ids": list(dict.fromkeys(a["source_fact_ids"] + b["source_fact_ids"])),
                                        "cross_filing": a["cross_filing"] or b["cross_filing"]}
            if "operating_expense" in values and "fuel_expense" in values:
                a, b = values["operating_expense"], values["fuel_expense"]
                values["nonfuel_expense"] = {"value_musd": a["value_musd"] - b["value_musd"],
                    "published_on": max(a["published_on"], b["published_on"]), "method": "identity:operating_expense-fuel_expense",
                    "source_fact_ids": list(dict.fromkeys(a["source_fact_ids"] + b["source_fact_ids"])), "cross_filing": a["cross_filing"] or b["cross_filing"]}
            if values:
                rows.append({"entity": entity, "quarter": f"{year}-Q{quarter}", "period_start": start, "period_end": end,
                             "partition": partition(year), "values": values,
                             "missing_variables": sorted(set(mapping) - values.keys())})
    return {"entity": entity, "cutoff": cutoff, "rows": rows, "issues": issues,
            "vintage_status": "SEC filing-date reconstruction from a current Company Facts snapshot; release times and removed/revised extraction contexts are not recovered",
            "period_status": "Reported quarterly values or explicitly differenced YTD facts; no fabricated monthly history",
            "selection_policy": "Latest available filing date, mapped tag priority; conflicting values excluded. Direct quarter preferred to YTD differences.",
            "cash_asset_purchase_definition": tag_map(entity)["cash_asset_purchases"][0]}


def download_company_facts(entity: str, destination: Path = RAW, user_agent: str = "UAL Counterfactual Lab research") -> dict:
    if entity not in ENTITIES:
        raise ValueError("Unsupported SEC entity")
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{ENTITIES[entity]:010d}.json"
    content = fetch_https(url, user_agent)
    blob = json.loads(content)
    if blob.get("cik") != ENTITIES[entity]:
        raise ValueError("Downloaded Company Facts CIK differs from the requested company")
    return {"entity": entity, "cik": blob["cik"], "entity_name": blob["entityName"],
            **preserve_source(content, url, "application/json", destination)}


def build_history(sources: list[dict], cutoff: str, destination: Path = HISTORY, raw: Path = RAW) -> dict:
    if not sources or len({s["entity"] for s in sources}) != len(sources):
        raise ValueError("History requires at least one source and one snapshot per entity")
    panels, all_facts = [], []
    for source in sources:
        content, metadata = read_source(source["sha256"], raw)
        if metadata["source_url"] != source["source_url"]:
            raise ValueError("Historical source URL differs from raw metadata")
        facts = normalize_facts(json.loads(content), source["entity"], source["sha256"])
        all_facts.extend(facts)
        panels.append(financial_panel(facts, source["entity"], cutoff))
    manifest = {"version": VERSION, "cutoff": cutoff, "sources": sources,
                "facts_digest": digest(all_facts), "panels_digest": digest(panels),
                "code_sha256": hashlib.sha256(Path(__file__).read_bytes() + Path(__file__).with_name("source_store.py").read_bytes()).hexdigest(),
                "coverage": [{"entity": p["entity"], "quarters": len(p["rows"]),
                              "first": p["rows"][0]["quarter"] if p["rows"] else None,
                              "last": p["rows"][-1]["quarter"] if p["rows"] else None,
                              "quarter_counts_by_partition": {part: sum(r["partition"] == part for r in p["rows"]) for part in ("fit_2013_2019", "selection_2020_2023", "locked_test_2024_plus")},
                              "observations_by_variable": {v: sum(v in r["values"] for r in p["rows"]) for v in tag_map(p["entity"])},
                              "conflicts": len(p["issues"])} for p in panels],
                "vintage_status": panels[0]["vintage_status"],
                "boundary": "Entity-wide standard US-GAAP financial facts only; custom tags, four-market traffic/fares and operating-carrier mapping remain separate requirements"}
    manifest["snapshot_id"] = digest(manifest)
    target = destination / manifest["snapshot_id"]
    target.mkdir(parents=True, exist_ok=True)
    contents = {"facts.json": all_facts, "panels.json": panels, "manifest.json": manifest}
    for name, value in contents.items():
        path = target / name
        if path.exists() and digest(json.loads(path.read_text())) != digest(value):
            raise ValueError("Immutable history snapshot was modified")
        if not path.exists():
            with path.open("x") as stream:
                json.dump(value, stream, indent=2, allow_nan=False)
                stream.write("\n")
    parquet = target / "facts.parquet"
    con = duckdb.connect()
    try:
        if not parquet.exists():
            escaped = str(parquet).replace("'", "''")
            con.execute(f"COPY (SELECT * FROM read_json_auto(?)) TO '{escaped}' (FORMAT PARQUET)", [str(target / "facts.json")])
        # Verify content, not only existence: snapshots cannot silently serve
        # modified Parquet while their canonical JSON still passes its checksum.
        result = con.execute("SELECT * FROM read_parquet(?) ORDER BY fact_id", [str(parquet)])
        names = [c[0] for c in result.description]
        restored = [dict(zip(names, (v.isoformat() if isinstance(v, date) else v for v in row))) for row in result.fetchall()]
        if digest(restored) != digest(sorted(all_facts, key=lambda f: f["fact_id"])):
            raise ValueError("Historical Parquet contents differ from preserved facts")
    finally:
        con.close()
    (destination / "latest.json").write_text(json.dumps({"snapshot_id": manifest["snapshot_id"]}, indent=2) + "\n")
    return {**manifest, "path": str(target)}


def history_status(destination: Path = HISTORY) -> dict:
    """Read a verified cached snapshot; HTTP requests never trigger downloads."""
    latest = destination / "latest.json"
    if not latest.exists():
        return {"status": "not_built", "message": "Build the SEC financial history with scripts/build_history.py"}
    snapshot_id = json.loads(latest.read_text())["snapshot_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot_id):
        raise ValueError("Invalid financial history snapshot identifier")
    target = destination / snapshot_id
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest.get("snapshot_id") != snapshot_id or digest({k: v for k, v in manifest.items() if k != "snapshot_id"}) != snapshot_id:
        raise ValueError("Financial history manifest checksum failed")
    for name, key in (("facts.json", "facts_digest"), ("panels.json", "panels_digest")):
        if digest(json.loads((target / name).read_text())) != manifest[key]:
            raise ValueError("Financial history data checksum failed")
    return {"status": "available", **manifest}
