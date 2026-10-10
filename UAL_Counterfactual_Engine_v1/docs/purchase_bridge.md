# Aircraft purchases, deposits and funding

`monthly-fleet-0.5.0` links explicit cash purchases to the same month-end aircraft
queue used by capacity. This advances M02/M05/M06, but does not complete the gross
delivery reconciliation, fleet retention policy or consolidated financial model.

## Evidence and allocation boundary

The July inventory guidance implies 57 narrowbody and 12 widebody minimum entries,
plus 18 narrowbody exits. [UAL's Q2 2026 10-Q, page 27](https://www.sec.gov/Archives/edgar/data/100517/000010051726000139/ual-20260630.htm)
separately expects H2 purchases of 40 MAX 9, six A321neo, five A321XLR and 12 787s.
The six extra narrowbody inventory entries are unresolved. Expected purchases
cannot be treated as the complete inventory queue or as exact monthly settlements.
Purchase prices, deposit allocations and financing terms are not supplied by that
table. This implementation does not fill them with purported reported values.

An optional `purchase_plan` allocates purchase counts to existing **mainline entry
record IDs**. Allocation can cover part of an entry, with multiple purchase batches,
but cannot exceed it. This adds no aircraft to the fleet. Unallocated entries keep
their dates and remain visible in `purchase_mapping`. The original linked entry
must be within the projection; historical cash cannot be silently rebased.

With a purchase plan, only allocated purchases are eligible for delivery delays.
Without one, delays continue to move inferred net entries with no purchase cash
effect. `delivery_aircraft_count` is optional: blank selects all eligible aircraft.
An explicit count selects chronological month/family/purchase-ID order in the
chosen mainline group and onset/duration window. Excess counts are rejected;
partial batch selection prorates its deposits and purchase terms. A zero-delay
search point retains the same allocation and paired calendar.

## Explicit financial contract

Plan fields are `basis`, `residual_monthly_capex_musd` and `purchases`. Each purchase
requires `purchase_id`, `fleet_record_id`, integer `aircraft`, `unit_price_musd`,
`opening_deposits_musd`, dated `deposits`, `debt_fraction`, `annual_interest_rate`
and `financing_fee_fraction`. Rates/fractions are decimal fractions in JSON and
percentages in the interface. Unknown fields, duplicates, invalid counts, excess
deposits, nonfinite values and dates outside the supported calendar are rejected.

Opening allocated deposits are assumed subsets of already-paid assets. They do
not reduce opening cash again. Future deposits are cash payments at their entered
dates, no later than the original delivery date. Delaying delivery leaves those
payment dates intact; no deposit refund, extension charge or capitalized interest
is inferred. At month-end settlement:

- Acquired-asset addition = aircraft count × assumed unit price.
- Delivery cash payment = acquired cost − allocated deposits released.
- Cash loan proceeds = delivery cash payment × assumed debt fraction.
- Financing cash fee = cash loan proceeds × assumed fee fraction.

**The historical capex reference is replaced in both paired cases.** Active cash
capex = assumed residual monthly cash capex + new deposits + delivery payments.
The residual must cover every unallocated purchase and other capital payment;
the engine does not estimate it. Adding gross purchase payments to unchanged
historical net capex would count spending twice. Optional cash-event capex changes
are incremental to this active schedule.

Loans are assumed available at settlement and non-amortizing through the horizon.
Interest equals opening-month purchase principal × annual rate / 12, starting the
month after draw. It reduces OCF without a new assumed tax benefit. Generic debt
repayment events are rejected with a purchase plan because loan allocation and
amortization are not modeled. Separate revolver repayment remains available.
Loan proceeds/fees change cash and total principal outside FCF and earnings.
No shortfall causes an automatic loan or revolver draw.

The ledger tracks allocated deposit balances, purchase principal and cumulative
acquired-asset additions. These are **subsets**, not consolidated deposit or PPE
stocks. Depreciation, leases, disposals, retained-aircraft costs, complete gross
commitments, contractual maturities and lender/covenant feasibility remain pending.
Beyond-horizon settlements stay queued; their deposits are not written off.
Aircraft become operational the month following their settlement.

## Example and verification

`data/purchase_plan_example.json` is explicitly illustrative. It maps the 40 MAX
net entries to 20 September/20 December purchases and assumes $40M each, $4M per
aircraft already deposited, another $2M in August, 75% borrowing against the
remaining delivery payment, 6% annual interest, a 1% financing fee and $100M monthly
residual capex. The residual does not establish a United capital-spending forecast.

With 30 aircraft delayed nine months, 20 move to June 2027, ten December aircraft
remain on time and ten move to September 2027. By July 2027 the model retains $60M
of allocated deposits for those ten queued aircraft. September's $680M delivery
payment is deferred together with $510M borrowing and its $5.1M fee: cash preserved
that month is $175.1M, before later operating and interest differences.

```bash
python scripts/run_monthly.py --purchase-plan data/purchase_plan_example.json \
  --delivery-count 30 --delivery-delay 9 --delivery-group narrowbody
python scripts/audit_purchase_example.py data/runs/RUN_ID.json
```

The UI exposes editable purchase batches, linked entries, deposits, rates and
residual capex. The illustrative plan requires an explicit load action. Saved-run
tables compare control/intervention payments, borrowing, capex and deposit/asset
subsets; both inverse workflows preserve the plan and aircraft count. The thesis
keeps true pathwise FCF separate from cash funding and describes the delay boundary.

Thirty additional tests cover exact selection, partial batch funding, conservation,
service lag, fixed deposit dates, out-of-horizon obligations, capex replacement,
interest, all-path cash identities, invalid mapping/dates, generic repayment gates,
paired placebo, quarterly aggregation, both solvers, API, archived replay/tamper
rejection and one-page thesis semantics. The full suite passes 227 tests.
`scripts/audit_purchase_example.py` uses an independently declared delivery calendar
and Decimal arithmetic, after registered-core replay; 1,421 identities pass. This
checks accounting, not empirical prices, full source extraction or forecast quality.

The final 10,000-path run is
`95bc39392c34a8d5e03a59b17265ae7c1573384da40862a81abf90ca1167cb54`;
its immutable audit is
`data/purchase_bridge_checks/62dbae27989c44ce1b6851aef9fbb403378e0021e32d1d48235952d59c9103d7.json`.
The downloaded browser run independently replayed and matched the CLI record.
A 3×3 delivery-delay/demand search also replayed its nine cells from the actual
download (`32e033da456a49b93dfa97d03b48908e95d676873cf82a6ef6152c2a5b1ae89a`).
Inspecting its four-month witness restored the full purchase plan and count of 30;
the final preview was returned to the nine-month example. No browser warnings or
errors were captured. Screenshot: `screenshots/monthly-purchase-funding.png`.
The earlier funded 0.4.0 run still independently replays through its archived core.
