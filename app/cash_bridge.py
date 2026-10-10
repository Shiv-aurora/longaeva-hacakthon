"""Explicit incremental cash/funding events over the provisional operating bridge.

This is a transaction sensitivity ledger, not a fitted balance-sheet forecast.
Historical OCF already contains working capital, interest and loyalty cash effects.
Events therefore describe departures from that retained reference bridge, never
gross loyalty receipts inferred from a deferred-revenue accounting roll-forward.
"""
from datetime import date
import math
import re

import numpy as np

from .evidence import available_records

VERSION = "incremental-cash-ledger-0.1.0"
STOCK_VARIABLES = {
    "restricted_cash_musd": "restricted_cash",
    "debt_principal_musd": "debt_principal",
    "revolver_limit_musd": "undrawn_revolver",
    "advance_sales_musd": "advance_ticket_sales",
    "loyalty_deferred_musd": "loyalty_deferred_total",
    "receivables_musd": "receivables_net",
    "inventory_musd": "fuel_parts_supplies_net",
    "prepaids_musd": "prepaid_expenses_other",
    "payables_musd": "accounts_payable",
}
WORKING_CAPITAL = {
    "advance_sales_change": ("advance_sales_musd", 1),
    "loyalty_balance_change": ("loyalty_deferred_musd", 1),
    "receivables_change": ("receivables_musd", -1),
    "inventory_change": ("inventory_musd", -1),
    "prepaids_change": ("prepaids_musd", -1),
    "payables_change": ("payables_musd", 1),
}
SIGNED_KINDS = {*WORKING_CAPITAL, "restricted_transfer", "investment_trade"}
KINDS = SIGNED_KINDS | {"borrow", "repay", "revolver_draw", "revolver_repay", "additional_capex",
                         "capex_refund", "cash_interest", "financing_fee", "asset_sale"}
STOCK_OUTPUTS = ("restricted_cash_musd", "short_term_investments_musd", "debt_principal_musd",
                 "revolver_drawn_musd", "revolver_available_musd", *[v[0] for v in WORKING_CAPITAL.values()])
FLOW_OUTPUTS = ("working_capital_cash_adjustment_musd", "incremental_cash_interest_musd",
                "other_investing_cash_flow_musd", "financing_cash_flow_musd", "restricted_cash_transfer_musd",
                "net_cash_change_musd")


def validate_plan(plan, months):
    if plan is None:
        return
    if not isinstance(plan, dict) or set(plan) != {"basis", "events"}:
        raise ValueError("Cash plan requires only basis and events")
    if not isinstance(plan["basis"], str) or not 1 <= len(plan["basis"].strip()) <= 600:
        raise ValueError("Cash plan basis must describe the analyst assumptions (1-600 characters)")
    if not isinstance(plan["events"], list) or len(plan["events"]) > 96:
        raise ValueError("Cash plan supports at most 96 explicit events")
    for event in plan["events"]:
        if not isinstance(event, dict) or set(event) != {"month", "kind", "amount_musd", "scope"}:
            raise ValueError("Cash event requires month, kind, amount_musd and scope")
        if not isinstance(event["month"], str) or not re.fullmatch(r"\d{4}-\d{2}", event["month"]):
            raise ValueError("Cash event month must be YYYY-MM")
        date.fromisoformat(event["month"] + "-01")
        if event["month"] not in months:
            raise ValueError("Cash event falls outside the simulated months; it cannot be silently dropped")
        if (not isinstance(event["kind"], str) or event["kind"] not in KINDS
                or not isinstance(event["scope"], str) or event["scope"] not in {"both", "intervention"}):
            raise ValueError("Unsupported cash event kind or scope")
        value = event["amount_musd"]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or abs(value) > 100000:
            raise ValueError("Cash event amount must be finite USD millions within +/-100000")
        if event["kind"] not in SIGNED_KINDS and value < 0:
            raise ValueError("This cash event requires a nonnegative amount")


def opening_state(snapshot, cutoff, baseline):
    """Require same-date reported stocks, not a zero fill or a taxonomy alias."""
    records = available_records(snapshot, cutoff)
    effective = baseline["initial_state_effective_on"]
    state = {"cash_musd": baseline["cash_musd"],
             "short_term_investments_musd": baseline["short_term_investments_musd"],
             "revolver_drawn_musd": 0.0}
    references = {}
    for field, variable in STOCK_VARIABLES.items():
        candidates = [r for r in records if r["entity"] == "UAL" and r["variable"] == variable
                      and r["effective_start"] == effective == r["effective_end"]
                      and r["evidence_type"] == "reported" and r["units"] == "USD_million"]
        if not candidates or len({r["value"] for r in candidates}) != 1:
            raise ValueError(f"Cash ledger requires unambiguous reported UAL.{variable} at {effective}")
        selected = sorted(candidates, key=lambda r: (r["published_on"], r["record_id"]))[-1]
        value = selected["value"]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("Cash ledger opening stocks must be finite and nonnegative")
        state[field] = value
        references[field] = selected["record_id"]
    # The reported facility is expressly undrawn. Its future availability is an
    # assumption; no covenant compliance forecast is inferred from this amount.
    state["revolver_available_musd"] = state["revolver_limit_musd"]
    return {"version": VERSION, "effective_on": effective, "state": state, "source_record_ids": references,
            "status": "Reported opening stocks held at the scenario start; explicit incremental events only. No inferred gross collections, automatic borrowing, future covenant guarantee, or complete balance-sheet roll-forward."}


def month_events(plan, month, intervention):
    values = {kind: 0.0 for kind in KINDS}
    for event in plan["events"] if plan is not None else []:
        if event["month"] == month and (event["scope"] == "both" or intervention):
            values[event["kind"]] += event["amount_musd"]
    return values


def step(state, events, operating_cash_flow, cash_capex):
    """Settle one month atomically; negative cash exposes a funding shortfall.

    Non-cash remeasurement/reclassification is outside this event contract. There
    is no tax deduction assumed for incremental cash interest. Existing interest
    remains embedded in the reference OCF. No borrowing enters FCF or earnings.
    """
    # Validate the proposed settlement on a separate state so a rejected event
    # cannot leave the caller with partially updated debt or working capital.
    next_state = dict(state)
    wc_cash = 0.0
    for kind, (field, sign) in WORKING_CAPITAL.items():
        change = events[kind]
        next_state[field] += change
        wc_cash += sign * change
    draw, repayment = events["revolver_draw"], events["revolver_repay"]
    if draw > state["revolver_available_musd"] + 1e-8:
        raise ValueError("Revolver draw exceeds opening-month available credit")
    if repayment > state["revolver_drawn_musd"] + draw + 1e-8:
        raise ValueError("Revolver repayment exceeds drawn principal")
    # Same-month repayments do not increase this month's gross drawing limit.
    next_state["revolver_drawn_musd"] += draw - repayment
    next_state["revolver_available_musd"] = state["revolver_limit_musd"] - next_state["revolver_drawn_musd"]
    nonrevolver = state["debt_principal_musd"] - state["revolver_drawn_musd"]
    if events["repay"] > nonrevolver + events["borrow"] + 1e-8:
        raise ValueError("Debt repayment exceeds nonrevolver principal")
    next_state["debt_principal_musd"] += events["borrow"] + draw - events["repay"] - repayment
    next_state["restricted_cash_musd"] += events["restricted_transfer"]
    next_state["short_term_investments_musd"] += events["investment_trade"]
    for field in STOCK_OUTPUTS:
        if next_state[field] < -1e-8 or not math.isfinite(next_state[field]):
            raise ValueError(f"Cash event overdraws or invalidates {field}")
        next_state[field] = max(0.0, next_state[field])
    ocf = np.asarray(operating_cash_flow) + wc_cash - events["cash_interest"]
    capex = np.asarray(cash_capex) + events["additional_capex"] - events["capex_refund"]
    fcf = ocf - capex
    investing = events["asset_sale"] - events["investment_trade"]
    financing = events["borrow"] + draw - events["repay"] - repayment - events["financing_fee"]
    cash_change = fcf + investing + financing - events["restricted_transfer"]
    next_state["cash_musd"] = np.asarray(state["cash_musd"]) + cash_change
    flows = {"operating_cash_flow_musd": ocf, "cash_capex_musd": capex, "free_cash_flow_musd": fcf,
             "working_capital_cash_adjustment_musd": wc_cash,
             "incremental_cash_interest_musd": events["cash_interest"],
             "other_investing_cash_flow_musd": investing, "financing_cash_flow_musd": financing,
             "restricted_cash_transfer_musd": events["restricted_transfer"], "net_cash_change_musd": cash_change}
    return next_state, flows
