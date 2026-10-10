# Retained aircraft while replacements are delayed

Core `monthly-fleet-0.6.0` adds explicit retirement-extension actions to the same
fleet, capacity, operating-cost and cash paths. It advances the proposal's page 4
retained-aircraft cost example and Appendix B's retention limits. It is a fixed
analyst sensitivity; fitted retirement policy and verified maintenance/crew/hub
feasibility remain open requirements.

## Contract and eligibility boundary

An optional `retention_plan` requires a stated `basis`, a simultaneous
`max_retained_aircraft` cap, and 1–48 replacement `links`. Every link names an
existing mainline net exit and an explicitly mapped purchase in the same fleet
group and original month. This records an assumed replacement relationship;
inventory guidance does not itself prove a gross retirement or replacement link.
Regional exits, historical retirements and unmapped replacements are unsupported.
No extra aircraft are created.

Links specify a unique retention ID, requested integer aircraft count, maximum
extension, minimum notice, explicit maintenance/crew support booleans, additional
maintenance and rent/other carrying per aircraft-month, retained productivity,
and extra fuel-burn fraction. JSON fractions are decimal fractions; the screen
shows percentages. Counts cannot exceed either the linked exit or purchase, even
across multiple links. Unknown fields, nonfinite rates, invalid counts and
unsupported boundaries are rejected.

Retention is applied only when the linked purchase actually moves under the
scenario's delay/count/group/window selection. One delayed replacement supports
at most one retained aircraft. The extension is capped by the entered maximum;
it can end before the replacement arrives. Notice is the number of full monthly
steps between shock onset and the planned exit, under the assumed monthly decision
clock. Missing assumed maintenance/crew support, missed notice, insufficient
replacement count or exhausted simultaneous retention capacity leaves requested
aircraft unapplied, with reasons visible in the result.

Actions have a recorded deterministic priority: original exit month, then retention
ID. The simultaneous cap applies across overlapping extension intervals, including
months beyond the output horizon. Partial requests can apply. Both pricing modes
receive the same fixed retention actions; the frozen mode freezes pricing only.
No optimized action or fitted observed-like policy is implied.

## Fleet, productivity and costs

The original exit is split into an unchanged portion and the retained portion's
extended month-end exit. Retained aircraft remain in existing inventory. When an
extension lasts through delivery, the old aircraft exits when its replacement
settles, and the replacement enters service next month. Beyond-horizon extended
exits remain queued. The eventual fleet endpoints conserve the same aircraft.

Retained aircraft start contributing in the month after their planned exit and
stop after the effective exit. Available capacity uses their explicit productivity
fraction, after proportionally allocating group outages. Availability counts
remain integer; productivity equivalents can be fractional. These coarse assumed
limits do not substitute for a crew roster, maintenance program or hub model.
Committed schedules remain those of the paired reference; retention adds no
schedule commitments and operated capacity stays within that schedule/aircraft
ceiling.

Operated capacity is assumed allocated proportionally across effective available
aircraft. The retained portion incurs the entered extra fuel burn; extra fuel is
zero with no productive retained capacity or no operated capacity. It enters the
existing assumed pricing-pressure/lag rule. Regional fuel remains partitioned
from the consolidated anchor, never charged again for mainline retention.

Maintenance and carrying rates are **additional** to the existing modeled nonfuel
costs, not purported gross historical cost estimates. They accrue per retained
aircraft-month even when idle or unavailable. They reduce operating profit, then
flow through the existing assumed marginal cash-tax OCF bridge. No capitalized
overhaul, depreciation, lease liability or verified tax deduction is inferred.
Fuel is path-dependent; monthly fuel quantiles cannot be summed into a horizon
quantile. Retirement extensions do not change deposit, settlement or purchase
financing calendars.

## Example and verification

`data/retention_plan_example.json` assumes ten September A319/A320 net exits link
to the September MAX purchase batch and eight December exits to the December
batch. With 30 MAX aircraft delayed nine months, ten old aircraft exit in June
2027 and eight in September 2027. Assume one month notice, maintenance/crew
support, at most nine months extension and eighteen simultaneous retained aircraft.
Additional maintenance is $0.02M and rent/other carrying $0.01M per aircraft-month;
productivity is 90% and extra fuel burn 15%. These are illustrative assumptions,
not United cost or eligibility evidence.

The twelve-month case has 146 retained aircraft-months and $4.38M additional fixed
maintenance/carrying expense, plus variable extra fuel. A cap of twelve applies
only two of the eight December requests while the ten September aircraft remain
retained; the six unapplied requests are visible. The example can preserve
capacity and still lower profit or cash relative to no retention. Its sign depends
on assumed costs, demand and pricing; it is not evidence of an optimal policy.

```bash
python scripts/run_monthly.py --purchase-plan data/purchase_plan_example.json \
  --retention-plan data/retention_plan_example.json \
  --delivery-count 30 --delivery-delay 9 --delivery-group narrowbody
python scripts/run_monthly.py --purchase-plan data/purchase_plan_example.json \
  --delivery-count 30 --delivery-delay 9 --delivery-group narrowbody
python scripts/audit_retention_example.py data/runs/RETAINED_ID.json data/runs/COMPARATOR_ID.json
```

The 27 new tests cover existing-aircraft conservation, eventual endpoints, service
timing, lower productivity/idle cases, schedule ceilings, maintenance/carrying/tax
identities, operated-only fuel, regional partition, outages/cuts, support/notice/
extension gates, simultaneous caps, partial replacements, zero-delay placebo,
same fixed actions across policies, invalid links/values, both inverse workflows,
API, full archived replay/tamper rejection and one-page thesis at maximum note
lengths. The full suite passes 279 tests.

The independent audit declares the retirement calendar and aircraft-month counts
outside the model, then uses Decimal arithmetic to check reference invariance,
unchanged purchase cash, conservation, productivity/capacity, fixed expenses and
fuel identities after registered-core replay. Its fuel-quantile identity is limited
to the example's common all-market delivered-price assumption; it makes no general
quantile-addition claim. All 2,287 checks pass. This is accounting/mechanism
evidence, not empirical eligibility or forecast validation.

Saved 10,000-path retained run:
`e1c222d28454743cc410c851c3d44f9b6a811d555943677a5dbfdd3986215c1e`.
Identical-settings comparator:
`9a235790886f2bae85f664a3eb841c9b9bd0cb7b06b469adb3b39bd1098ce3d1`.
Audit: `data/retention_checks/0334494ec15197e570c9a3236bc0dd08811ca366bf7b418af63faf768faaa221.json`.
Thesis renderer 0.4.0 records the retained-count peak and fixed upkeep/carrying
total; its companion contains every eligibility decision, link, cost and queue.

The browser's downloaded 10,000-path result matched the CLI's deterministic
output and passed registered-core replay. The browser also showed the twelve-
aircraft cap applying only two December requests, with six requests unapplied.
Its nine-cell delay/demand search was independently replayed in full:
`4299b5453d76f9984b0cf966fcb8ec7261218301531e4ba963147220575c1167`.
Inspecting the four-month-delay witness preserved both purchase and retention
plans, the thirty-aircraft delay count and the eighteen-aircraft retention cap;
extended exits moved to January and April 2027. The final preview returned to
the nine-month case. No browser errors were captured. The earlier purchase-only
0.5.0 run also still replays with its registered core.

Visual evidence: `screenshots/monthly-retention.png` shows the final monthly
costs, retained counts and both applied retirement decisions.
