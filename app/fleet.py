"""Fleet conservation and a separate contracted regional capacity block.

Quarter-end fleet *inventory* guidance is not purchase-delivery guidance. The
minimum net entries/exits between inventory endpoints are inferred here, with
all events at month-end. Entry-to-service, market assignment, spare capacity and
schedule growth remain explicit assumptions until BTS traffic is ingested.
"""
from __future__ import annotations

from datetime import date
import math
import re

import numpy as np

from .evidence import available_records

GROUPS = ("narrowbody", "widebody", "regional")
FAMILIES = {
    "B777": "widebody", "B787": "widebody", "B767": "widebody",
    "B757": "narrowbody", "B737_MAX": "narrowbody", "B737_NG": "narrowbody",
    "A319_A320": "narrowbody", "A321": "narrowbody",
    "regional_50": "regional", "regional_70_76": "regional",
}

PURCHASE_FLOWS = ("purchase_deposit_cash_musd", "purchase_settlement_cash_musd",
                  "purchase_borrowing_musd", "purchase_interest_musd", "purchase_financing_fee_musd",
                  "purchase_asset_additions_musd", "residual_cash_capex_musd")
PURCHASE_STOCKS = ("allocated_purchase_deposits_musd", "purchase_debt_principal_musd",
                   "cumulative_purchase_asset_additions_musd")
RETENTION_FLOWS = ("retention_maintenance_expense_musd", "retention_carrying_expense_musd",
                   "retention_extra_fuel_expense_musd")


def retention_queue(retention, purchases, fleet, queue, s, months):
    """Apply explicitly assumed eligible retirement extensions, never new aircraft.

    One retained aircraft is linked to one delayed replacement. Limits/clearances
    are analyst inputs, not fitted management decisions or airworthiness findings.
    """
    if retention is None:
        return queue, None
    if purchases is None:
        raise ValueError("Retention requires an explicit purchase plan and replacement links")
    if not isinstance(retention, dict) or set(retention) != {"basis", "max_retained_aircraft", "links"}:
        raise ValueError("Retention plan requires basis, max_retained_aircraft and links")
    if not isinstance(retention["basis"], str) or not 1 <= len(retention["basis"].strip()) <= 600:
        raise ValueError("Retention plan requires an analyst basis (1-600 characters)")
    def integer(v, name, low, high):
        if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    def number(v, name, high):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= high:
            raise ValueError(f"{name} must be finite between 0 and {high}")
    integer(retention["max_retained_aircraft"], "Retention capacity", 0, 1000)
    links = retention["links"]
    if not isinstance(links, list) or not 1 <= len(links) <= 48:
        raise ValueError("Retention plan requires 1-48 explicit replacement links")
    purchase_lookup = {p["purchase_id"]: p for p in purchases["purchases"]}
    events = {e["record_id"]: e for e in fleet["events"]}
    fields = {"retention_id", "exit_record_id", "purchase_id", "aircraft", "max_extension_months",
              "minimum_notice_months", "maintenance_supported", "crew_supported",
              "additional_maintenance_musd_per_aircraft_month", "additional_carrying_musd_per_aircraft_month",
              "extra_fuel_burn_fraction", "productivity_fraction"}
    ids, exit_allocations, purchase_allocations = set(), {}, {}
    for link in links:
        if not isinstance(link, dict) or set(link) != fields:
            raise ValueError("Retention link has missing or unsupported fields")
        key = link["retention_id"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", key) or key in ids:
            raise ValueError("Retention IDs must be unique 1-60 character letters, digits, underscores or hyphens")
        ids.add(key)
        ref, pid = link["exit_record_id"], link["purchase_id"]
        if not isinstance(ref, str) or ref not in events or events[ref]["kind"] != "exit" or events[ref]["group"] == "regional":
            raise ValueError("Retention link must reference an existing mainline exit")
        if not isinstance(pid, str) or pid not in purchase_lookup:
            raise ValueError("Retention link must reference a mapped replacement purchase")
        old, new = events[ref], events[purchase_lookup[pid]["fleet_record_id"]]
        if old["month"] not in months:
            raise ValueError("Retention cannot restore a historical exit before the projection")
        if old["group"] != new["group"] or old["month"] != new["month"]:
            raise ValueError("Retention requires a replacement in the same mainline group and planned month")
        integer(link["aircraft"], "Retained aircraft", 1, 1000)
        exit_allocations[ref] = exit_allocations.get(ref, 0) + link["aircraft"]
        purchase_allocations[pid] = purchase_allocations.get(pid, 0) + link["aircraft"]
        if exit_allocations[ref] > old["aircraft"] or purchase_allocations[pid] > purchase_lookup[pid]["aircraft"]:
            raise ValueError("Retention allocations exceed their planned exit or replacement purchase")
        integer(link["max_extension_months"], "Maximum retirement extension", 0, 24)
        integer(link["minimum_notice_months"], "Minimum retention notice", 0, 24)
        for name in ("maintenance_supported", "crew_supported"):
            if not isinstance(link[name], bool):
                raise ValueError("Retention support assumptions must be explicit booleans")
        for name in ("additional_maintenance_musd_per_aircraft_month", "additional_carrying_musd_per_aircraft_month"):
            number(link[name], name, 10)
        for name in ("extra_fuel_burn_fraction", "productivity_fraction"):
            number(link[name], name, 1)
    delayed = {pid: sum(e["aircraft"] for e in queue if e.get("purchase_id") == pid and "original_month" in e) for pid in purchase_lookup}
    reserved, decisions, shifted_exits = {}, [], []
    # Priority is fully recorded and deterministic; no future observations enter it.
    for link in sorted(links, key=lambda x: (events[x["exit_record_id"]]["month"], x["retention_id"])):
        old = events[link["exit_record_id"]];pid = link["purchase_id"]
        exit_index = (int(old["month"][:4])-int(months[0][:4]))*12+int(old["month"][5:])-int(months[0][5:])+1
        extension = min(s.delivery_delay_months, link["max_extension_months"])
        reasons = []
        if not delayed[pid]:reasons.append("No delayed replacement aircraft remain allocated")
        if not link["maintenance_supported"]:reasons.append("Maintenance support is not assumed available")
        if not link["crew_supported"]:reasons.append("Crew support is not assumed available")
        if exit_index-s.onset_month < link["minimum_notice_months"]:reasons.append("Retention notice window is missed")
        if extension == 0:reasons.append("No permitted retirement extension")
        interval = [_shift(old["month"], k) for k in range(1, extension+1)]
        remaining_capacity = min((retention["max_retained_aircraft"]-reserved.get(m,0) for m in interval), default=0)
        n = 0 if reasons else min(link["aircraft"], delayed[pid], remaining_capacity)
        if n < link["aircraft"] and not reasons:
            reasons.append("Replacement count or simultaneous retention capacity limits the request")
        delayed[pid] -= n
        # Reserve every aircraft-month of the extension, not just the exit month,
        # so later links cannot reuse simultaneous maintenance/crew capacity.
        for month in interval:reserved[month] = reserved.get(month,0)+n
        decision = {**link, "group": old["group"], "original_exit_month": old["month"],
                    "effective_exit_month": _shift(old["month"], extension) if n else old["month"],
                    "retained_aircraft": n, "unapplied_aircraft": link["aircraft"]-n,
                    "extension_months": extension if n else 0, "reasons": reasons}
        decisions.append(decision)
        if n:
            shifted_exits.append({**old, "aircraft": n, "retention_id": link["retention_id"],
                                  "original_exit_month": old["month"], "month": decision["effective_exit_month"],
                                  "status": "Assumed eligible retirement extension linked to a delayed replacement"})
    adjusted = []
    for e in queue:
        if e["kind"] != "exit":
            adjusted.append(e);continue
        n = e["aircraft"]-sum(d["retained_aircraft"] for d in decisions if d["exit_record_id"] == e["record_id"])
        if n:adjusted.append({**e, "aircraft": n})
    adjusted.extend(shifted_exits)
    return adjusted, {"basis": retention["basis"], "max_retained_aircraft": retention["max_retained_aircraft"],
                      "decisions": decisions, "boundary": "Fixed analyst retention actions, identical across pricing policies; no fitted retirement policy, verified maintenance/crew clearance or automatic schedule increase."}


def validate_purchase_plan(plan, fleet, months):
    """Explicitly allocate purchases to net entries; never infer legal ownership."""
    if plan is None:
        return
    if not isinstance(plan, dict) or set(plan) != {"basis", "residual_monthly_capex_musd", "purchases"}:
        raise ValueError("Purchase plan requires basis, residual_monthly_capex_musd and purchases")
    if not isinstance(plan["basis"], str) or not 1 <= len(plan["basis"].strip()) <= 600:
        raise ValueError("Purchase plan requires an analyst basis (1-600 characters)")

    def number(value, name, low=0, high=100000):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be finite between {low} and {high}")

    number(plan["residual_monthly_capex_musd"], "Residual monthly cash capex")
    purchases = plan["purchases"]
    if not isinstance(purchases, list) or not 1 <= len(purchases) <= 48:
        raise ValueError("Purchase plan requires 1-48 explicit purchase batches")
    entries = {e["record_id"]: e for e in fleet["events"] if e["kind"] == "entry" and e["group"] != "regional"}
    allocated, ids = {}, set()
    fields = {"purchase_id", "fleet_record_id", "aircraft", "unit_price_musd", "opening_deposits_musd",
              "deposits", "debt_fraction", "annual_interest_rate", "financing_fee_fraction"}
    for p in purchases:
        if not isinstance(p, dict) or set(p) != fields:
            raise ValueError("Purchase batch has missing or unsupported fields")
        key = p["purchase_id"]
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", key) or key in ids:
            raise ValueError("Purchase IDs must be unique 1-60 character letters, digits, underscores or hyphens")
        ids.add(key)
        ref = p["fleet_record_id"]
        if not isinstance(ref, str) or ref not in entries:
            raise ValueError("Purchase batch must reference an existing mainline entry record")
        entry = entries[ref]
        if entry["month"] not in months:
            raise ValueError("Original purchase entry must fall inside the projection; historical cash cannot be rebased")
        n = p["aircraft"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= entry["aircraft"]:
            raise ValueError("Purchase aircraft count must be a positive integer within its linked entry")
        allocated[ref] = allocated.get(ref, 0) + n
        if allocated[ref] > entry["aircraft"]:
            raise ValueError("Purchase allocations exceed the linked fleet entry")
        number(p["unit_price_musd"], "Purchase unit price", .01, 500)
        cost = n * p["unit_price_musd"]
        number(p["opening_deposits_musd"], "Allocated opening deposits", 0, cost)
        number(p["debt_fraction"], "Debt fraction of delivery settlement", 0, 1)
        number(p["annual_interest_rate"], "Annual purchase debt interest rate", 0, .5)
        number(p["financing_fee_fraction"], "Financing fee fraction", 0, .1)
        if not isinstance(p["deposits"], list) or len(p["deposits"]) > 24:
            raise ValueError("Purchase batch supports at most 24 dated deposit payments")
        paid = p["opening_deposits_musd"]
        for d in p["deposits"]:
            if not isinstance(d, dict) or set(d) != {"month", "amount_musd"}:
                raise ValueError("Deposit payment requires month and amount_musd")
            if not isinstance(d["month"], str) or d["month"] not in months or d["month"] > entry["month"]:
                raise ValueError("Deposit must fall in the projection no later than the original delivery month")
            number(d["amount_musd"], "Deposit cash payment")
            paid += d["amount_musd"]
        if paid > cost + 1e-8:
            raise ValueError("Opening and future deposits exceed the purchase price")


def purchase_cash_trajectory(plan, track, months):
    """Cash purchases funded by cash loan proceeds, with month-end settlements.

    This rolls forward only the explicitly allocated deposits, acquired-asset
    additions and assumed non-amortizing purchase debt, not consolidated PPE.
    Earlier deposits stay paid when settlement moves. Interest starts next month.
    """
    purchases = {p["purchase_id"]: p for p in plan["purchases"]}
    deposits = {k: p["opening_deposits_musd"] for k, p in purchases.items()}
    debt = {k: 0.0 for k in purchases}
    acquired = 0.0
    rows = []
    for month in months:
        row = {name: 0.0 for name in PURCHASE_FLOWS}
        row["residual_cash_capex_musd"] = plan["residual_monthly_capex_musd"]
        row["purchase_interest_musd"] = sum(debt[k] * p["annual_interest_rate"] / 12 for k, p in purchases.items())
        for k, p in purchases.items():
            payment = sum(d["amount_musd"] for d in p["deposits"] if d["month"] == month)
            deposits[k] += payment
            row["purchase_deposit_cash_musd"] += payment
        for e in track["effective_queue"]:
            if e["month"] != month or "purchase_id" not in e:
                continue
            k = e["purchase_id"]; p = purchases[k]
            cost = e["aircraft"] * p["unit_price_musd"]
            release = (p["opening_deposits_musd"] + sum(d["amount_musd"] for d in p["deposits"])) * e["aircraft"] / p["aircraft"]
            settlement = cost - release
            borrowing = settlement * p["debt_fraction"]
            deposits[k] -= release
            debt[k] += borrowing
            acquired += cost
            row["purchase_settlement_cash_musd"] += settlement
            row["purchase_borrowing_musd"] += borrowing
            row["purchase_financing_fee_musd"] += borrowing * p["financing_fee_fraction"]
            row["purchase_asset_additions_musd"] += cost
        if any(v < -1e-8 for v in deposits.values()):
            raise ValueError("Purchase settlement consumes unavailable allocated deposits")
        row.update(month=month, allocated_purchase_deposits_musd=max(0.0, sum(deposits.values())),
                   purchase_debt_principal_musd=sum(debt.values()),
                   cumulative_purchase_asset_additions_musd=acquired)
        row["cash_capex_musd"] = sum(row[k] for k in ("residual_cash_capex_musd", "purchase_deposit_cash_musd", "purchase_settlement_cash_musd"))
        rows.append(row)
    return {"monthly": rows, "opening_allocated_deposits_musd": sum(p["opening_deposits_musd"] for p in purchases.values()),
            "pending_settlements": [e for e in track["remaining_queue"] if "purchase_id" in e],
            "status": "Analyst allocations and prices. Historical net capex is replaced by explicit residual capex plus purchase cash payments. Allocated deposits and acquired assets are subsets, not consolidated balance-sheet stocks. Debt is assumed available at settlement, non-amortizing through the horizon; interest begins next month."}


def fleet_plan(snapshot: dict, cutoff: str) -> dict:
    """Choose a common reported opening date; retain guidance as guidance."""
    records = [r for r in available_records(snapshot, cutoff)
               if r["entity"].startswith("fleet.") and r["variable"] == "inventory"]
    for r in records:
        if r["units"] != "aircraft" or r["value"] < 0 or int(r["value"]) != r["value"]:
            raise ValueError("Fleet inventories require nonnegative integer aircraft counts")
    histories = {family: [r for r in records if r["entity"] == "fleet." + family
                         and r["evidence_type"] == "reported" and r["effective_end"] <= cutoff]
                 for family in FAMILIES}
    common = set.intersection(*({r["effective_end"] for r in rows} for rows in histories.values()))
    if not common:
        raise ValueError(f"No common verified fleet inventory available at cutoff {cutoff}")
    effective = max(common)
    opening, events, used_ids = {}, [], []
    for family, group in FAMILIES.items():
        candidates = [r for r in histories[family] if r["effective_end"] == effective]
        if len(candidates) != 1:
            raise ValueError("Conflicting fleet inventory records")
        r = candidates[0]
        opening[family] = int(r["value"])
        used_ids.append(r["record_id"])
        targets = sorted([r for r in records if r["entity"] == "fleet." + family
                          and r["evidence_type"] == "guidance" and r["effective_end"] > effective],
                         key=lambda r: r["effective_end"])
        previous, dates = opening[family], set()
        for r in targets:
            end = r["effective_end"]
            if end in dates:
                raise ValueError("Conflicting fleet guidance records")
            dates.add(end)
            current = int(r["value"])
            delta = current - previous
            if delta:
                events.append({"month": end[:7], "family": family, "group": group,
                               "kind": "entry" if delta > 0 else "exit", "aircraft": abs(delta),
                               "record_id": r["record_id"], "status": "inferred minimum net fleet movement"})
            used_ids.append(r["record_id"])
            previous = current
    return {"effective_on": effective, "opening_by_family": opening,
            "opening_by_group": {g: sum(opening[f] for f in FAMILIES if FAMILIES[f] == g) for g in GROUPS},
            "events": sorted(events, key=lambda e: (e["month"], e["family"])),
            "record_ids": used_ids,
            "timing_status": "Month-end movements inferred from quarter-end inventory guidance; operational from next month",
            "entry_boundary": "Inventory net entries may include purchases, leases or other movements. Not the SEC purchase-delivery queue."}


def _shift(month: str, offset: int) -> str:
    d = date.fromisoformat(month + "-01")
    index = d.year * 12 + d.month - 1 + offset
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def trajectory(plan: dict, months: list[str], s, weights: np.ndarray, intervention: bool) -> dict:
    """Entries/exits conserve family inventory; outages reduce service, not stock."""
    if not months or len(weights) != len(months):
        raise ValueError("Fleet trajectory requires one shock weight per projection month")
    purchase_plan = getattr(s, "purchase_plan", None)
    validate_purchase_plan(purchase_plan, plan, months)
    candidates = []
    for raw in plan["events"]:
        allocations = [p for p in purchase_plan["purchases"] if p["fleet_record_id"] == raw["record_id"]] if purchase_plan else []
        for p in sorted(allocations, key=lambda p: p["purchase_id"]):
            candidates.append({**raw, "aircraft": p["aircraft"], "purchase_id": p["purchase_id"],
                               "status": "Analyst purchase allocation to an inferred net inventory entry"})
        remaining = raw["aircraft"] - sum(p["aircraft"] for p in allocations)
        if remaining:
            candidates.append({**raw, "aircraft": remaining})
    queue = []
    count = getattr(s, "delivery_aircraft_count", None)
    left = count
    for event in sorted(candidates, key=lambda e: (e["month"], e["family"], e.get("purchase_id", ""))):
        index = (int(event["month"][:4]) - int(months[0][:4])) * 12 + int(event["month"][5:]) - int(months[0][5:]) + 1
        eligible = (event["kind"] == "entry" and event["group"] != "regional"
                and s.delivery_group in ("all", event["group"])
                and s.onset_month <= index < s.onset_month + s.duration_months
                and (purchase_plan is None or "purchase_id" in event))
        selected = min(event["aircraft"], left) if eligible and left is not None else event["aircraft"] if eligible else 0
        if left is not None:
            left -= selected
        if intervention and s.delivery_delay_months > 0 and selected:
            if selected < event["aircraft"]:
                queue.append({**event, "aircraft": event["aircraft"] - selected})
            event = {**event, "aircraft": selected, "original_month": event["month"],
                     "month": _shift(event["month"], s.delivery_delay_months)}
        queue.append(event)
    if left:
        raise ValueError("Requested delay aircraft count exceeds eligible future entries in the selected group and shock window")
    queue, retention = retention_queue(getattr(s, "retention_plan", None), purchase_plan, plan, queue, s, months)
    stock = dict(plan["opening_by_family"])
    applied, rows = [], []

    def apply_before(month: str, inclusive: bool = False):
        for i, e in enumerate(queue):
            if i not in applied and (e["month"] < month or (inclusive and e["month"] == month)):
                stock[e["family"]] += e["aircraft"] * (1 if e["kind"] == "entry" else -1)
                if stock[e["family"]] < 0:
                    raise ValueError("Planned exits exceed aircraft in the fleet")
                applied.append(i)

    for t, month in enumerate(months):
        apply_before(month)
        owned = {g: sum(stock[f] for f in FAMILIES if FAMILIES[f] == g) for g in GROUPS}
        unavailable = {g: min(owned[g], math.floor(.5 + owned[g] * s.aircraft_unavailable_pct / 100 * weights[t]))
                       if intervention and s.availability_group in ("all", g) else 0 for g in GROUPS}
        row = {"month": month, "opening_inventory": owned,
               "unavailable": unavailable,
               "available": {g: owned[g] - unavailable[g] for g in GROUPS},
               "entries": {g: sum(e["aircraft"] for e in queue if e["month"] == month and e["kind"] == "entry" and e["group"] == g) for g in GROUPS},
               "exits": {g: sum(e["aircraft"] for e in queue if e["month"] == month and e["kind"] == "exit" and e["group"] == g) for g in GROUPS}}
        if retention is not None:
            active = [d for d in retention["decisions"] if d["original_exit_month"] < month <= d["effective_exit_month"] and d["retained_aircraft"]]
            retained = {g: sum(d["retained_aircraft"] for d in active if d["group"] == g) for g in GROUPS}
            ratio = {g: row["available"][g]/owned[g] if owned[g] else 0 for g in GROUPS}
            effective = {g: row["available"][g] - sum(d["retained_aircraft"]*ratio[g]*(1-d["productivity_fraction"]) for d in active if d["group"] == g) for g in GROUPS}
            premium = {g: sum(d["retained_aircraft"]*ratio[g]*d["productivity_fraction"]*d["extra_fuel_burn_fraction"] for d in active if d["group"] == g)/effective[g] if effective[g] else 0 for g in GROUPS}
            row["retention"] = {"retained_by_group": retained, "effective_available_aircraft": effective,
                                "fuel_burn_premium_fraction_by_group": premium,
                                "retention_maintenance_expense_musd": sum(d["retained_aircraft"]*d["additional_maintenance_musd_per_aircraft_month"] for d in active),
                                "retention_carrying_expense_musd": sum(d["retained_aircraft"]*d["additional_carrying_musd_per_aircraft_month"] for d in active)}
        apply_before(month, inclusive=True)
        row["closing_inventory"] = {g: sum(stock[f] for f in FAMILIES if FAMILIES[f] == g) for g in GROUPS}
        rows.append(row)
    result = {"monthly": rows, "remaining_queue": [e for i, e in enumerate(queue) if i not in applied],
            "effective_queue": queue,
            "opening_state_status": "Reported opening inventory; subsequent pre-projection movements follow guidance assumptions, not new observations"}
    if retention is not None:
        result["retention"] = {**retention, "pending_retirements": [e for e in result["remaining_queue"] if "retention_id" in e]}
    if purchase_plan is not None:
        result["purchase_cash"] = purchase_cash_trajectory(purchase_plan, result, months)
        result["purchase_mapping"] = {"allocated_aircraft": sum(p["aircraft"] for p in purchase_plan["purchases"]),
                                      "unallocated_mainline_entries": [e for e in candidates if e["kind"] == "entry" and e["group"] != "regional" and "purchase_id" not in e],
                                      "boundary": "Partial analyst allocation, not a reconciled SEC gross delivery queue. Unallocated entries retain their original dates. Residual capex must cover every unmodeled cash purchase and other capital payment."}
    return result


def market_split(b: dict, regional_share: float) -> np.ndarray:
    """ASM components, not airline ownership. All shares except annual total assumed."""
    asm = np.array([m["asm_million"] for m in b["markets"]])
    regional_domestic_share = asm.sum() * regional_share / asm[0]
    if not 0 <= regional_domestic_share <= 1:
        raise ValueError("Assumed regional capacity exceeds domestic capacity")
    shares = np.array([[.85 * (1 - regional_domestic_share), .15 * (1 - regional_domestic_share), regional_domestic_share],
                       [.05, .95, 0], [0, 1, 0], [.8, .2, 0]])
    return asm[:, None] * shares


def capacity_paths(b: dict, s, plan: dict, months: list[str], weights: np.ndarray) -> dict:
    """Separate commitments from realizable capacity; unused spare is not sold ASM."""
    control = trajectory(plan, months, s, weights, False)
    scenario = trajectory(plan, months, s, weights, True)
    split = market_split(b, b["regional_asm_share"])
    initial = np.array([plan["opening_by_group"][g] for g in GROUPS], float)
    if (initial <= 0).any():
        raise ValueError("Each modeled fleet block needs positive opening inventory")
    intended = np.array([[r["opening_inventory"][g] for g in GROUPS] for r in control["monthly"]])
    schedule_ratio = 1 + s.schedule_growth_fraction * (intended / initial - 1)

    def capacity(track: dict) -> np.ndarray:
        available = np.array([[r["retention"]["effective_available_aircraft"][g] if "retention" in r else r["available"][g] for g in GROUPS] for r in track["monthly"]])
        ceiling_ratio = available / initial / (1 - s.fleet_spare_fraction)
        return split[None, :, :] * np.minimum(schedule_ratio, ceiling_ratio)[:, None, :]

    return {"plan": plan, "control": control, "intervention": scenario,
            "reference_split_asm_million": split.tolist(),
            "committed_components": split[None, :, :] * schedule_ratio[:, None, :],
            "control_components": capacity(control), "intervention_components": capacity(scenario)}
