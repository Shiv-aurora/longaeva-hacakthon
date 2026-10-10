# Monthly fleet and financial sensitivity model card

Version: monthly-fleet-0.7.0. Partial implementation of the Phase 1 proposal;
behavioral and historical validation remain pending.

The separate traffic research scorecard now carries an unresolved AA Q1 2021
source-review warning. Its fitted traffic component is not deployed in this
financial sensitivity model. See [traffic vintage review](traffic_vintages.md)
before interpreting its historical comparisons or accepting new estimation inputs.

## Scope currently running

Four markets: domestic, Atlantic, Pacific and Latin America. Monthly steps and
quarterly reporting; 1–12 month primary horizon, 13–24 months exploratory. Default
10,000 paths with common fuel/demand draws across intervention, control and pricing
comparisons. Supported shocks: fuel, demand, direct capacity reductions, future
mainline fleet-entry delays, aircraft outages and regional contract rate changes.

Onset/duration selects the delayed events. Availability and rate shocks use the
common recovery curve. Geography applies to fuel/demand/direct capacity. Fleet
shocks apply to the selected group across its assigned markets; contract rates
apply to the whole regional block.

The ledger contains 82 verified records from United's Q2 issuer release, Q1/Q2 SEC
10-Qs, July 15 investor update and 2025 annual regional disclosure.
Original URLs, publication dates and period boundaries are preserved. Normalized
transcriptions are not verbatim document excerpts. Original SEC Company Facts
bytes, dated fact versions and exact JSON pointers are preserved for the financial
history pipeline and the Q1 cash-flow anchors. Original filing/issuer documents
and their passage extraction remain pending. See `financial_history.md`.

## Fleet and schedule boundary

Reported June 30 mainline inventory totals 1,122: 885 narrowbody and 237 widebody.
Regional contracted inventory totals 430. Families map to two mainline groups and
one regional block. These are inventory counts, not observed available-aircraft
counts: initial availability of all listed aircraft is assumed. Temporary outages
round to nearest integer and reduce service capacity without destroying stock.
Spare capacity is an explicit 2% assumption.

The July investor update guides mainline inventory to 1,145 in Q3 and 1,173 at
year-end, and regional inventory to 434 and 442. Positive/negative family changes
imply **minimum net entries/exits**, not observed gross purchases or retirements.
All events occur at quarter-end here and affect operations the following month.
Family stock conservation is checked. No aircraft growth is extrapolated after
the last endpoint. For later cutoffs, pre-projection movements remain guidance
reconstruction, not new inventory observations.

Separately, the 10-Q expects H2 purchases of 12 widebodies and 51 narrowbodies.
The fleet plan implies 12 widebody and 57 narrowbody net entries before 18
narrowbody exits. The six-aircraft difference is unresolved and is not silently
reconciled as purchases. Without a purchase plan, fleet-entry delays model the inventory plan. Optional
explicit purchase allocations link settlement/funding to that queue; the complete
source-reconciled gross purchase/delivery queue remains pending.

Delays shift selected future mainline net entries originally due during the onset
and duration window. Existing stock, regional contracts and planned exits remain
unchanged. Delayed entries remain queued beyond the horizon. Optional explicit assumed retention now postpones eligible net exits and carries
productivity/fuel/upkeep costs through the same paths; see `retention.md`.
Substitutes and fitted maintenance/crew/hub constraints remain pending.

Committed ASM scales with a configurable fraction of planned group fleet growth,
default 100%. Realized ASM is capped by available aircraft and assumed spare
capacity, then direct capacity shocks. Demand scales with committed ASM at the
reference load factor. Market/fleet assignments, constant productivity, domestic
regional allocation and carrying the 2025 regional ASM share of 6.1% into 2026 are
assumptions. Equal fuel intensity across blocks is assumed until BTS data supports
a split. These mechanisms do not establish historical operational feasibility.

## Accounting

Q2 market ASM, RPM and passenger revenue / 3 set monthly reference rates; monthly
history is not fabricated. Passenger revenue = served RPM × aggregate yield;
served RPM ≤ ASM. Cargo revenue scales with capacity; other revenue is fixed.
Fuel gallons scale with operated ASM at consolidated Q2 intensity.

Q2 regional capacity purchase expense of $743M / 3 is partitioned from nonfuel
expense. Its fixed/variable split is assumed and scales with regional capacity.
Remaining nonfuel costs scale with mainline capacity using a separate assumed
fixed/variable split. This residual still includes some regional-related expense.
Regional fuel is partitioned from consolidated fuel and counted once.

Operating income = passenger + cargo + other revenue − fuel − nonfuel expense.
The opening zero-noise reference reproduces Q2 operating totals within rounding.
Later reference months follow assumed schedules based on fleet inventory guidance.

Known structural sensitivity: with a 40% widebody outage, no noise or fuel shock,
and frozen pricing, 70% fixed residual nonfuel costs produce a +$614.3M 12-month
incremental FCF estimate; 95% fixed costs produce −$400.1M for the same fleet path.
Variable-cost savings can exceed lost revenue in this aggregate model. This sign
reversal is documented and tested; it is not evidence that groundings benefit United.
Historical cost fitting and disruption validation are required.

Reference Q2 OCF = (H1 $6,409M − Q1 $4,799M) / 3 = $536.7M per derived month.
Reference Q2 cash capex = (H1 $3,015M − Q1 $1,672M) / 3 = $447.7M per derived month.
OCF = reference Q2 OCF + (OI − reference OI) × (1 − assumed marginal cash-tax fraction).
Without a purchase plan, cash capex stays at the reference rate, unchanged by
fleet-entry delays. With a purchase plan, explicit residual monthly capex plus
new deposits and delivery payments replaces that reference in both paired cases.
Without either financial plan, FCF = OCF − cash capex and cash next month = prior cash + FCF.
With an optional cash plan, explicit cash-related working-capital changes and
incremental cash interest adjust OCF; capex events adjust cash capex. Financing,
other investing and restricted transfers reconcile spendable cash separately.
Debt principal, revolver availability and working-capital stock limits are checked.
All events are incremental analyst assumptions over the retained reference, with
both-path or shock-only scope. See `cash_bridge.md` for the event contract and limits.

Purchase prices, deposits and financing terms can now be explicit analyst
assumptions. Mapped delays move settlement/borrowing/fees while earlier deposits
stay paid; exact counts can split a batch. Allocated deposits and acquired assets
are subsets, not consolidated stocks. Loan interest begins the next month.
Complete gross queue reconciliation, PPE, leases and fitted retention costs remain
unresolved; conditional loan availability and residual capex are not fitted. See
`purchase_bridge.md` for validation and accounting boundaries. This provisional
cash bridge now aligns Q2 operations and Q2 cash flow by differencing the reported
YTD totals. It remains a constant reference run rate with no fitted monthly
cash seasonality. Advance sales, loyalty, depreciation, taxes, debt service,
interest and discretionary financing are not fitted. There is no automatic
financing; short-term investments remain separate. Negative cash indicates model
stress, not a legally modeled bankruptcy.

## Policies and uncertainty

Frozen policy holds yield response at zero. Assumed constrained policy adjusts
yield toward an assumed fraction of incremental fuel pressure, subject to lag,
half-life and a maximum monthly step. Demand uses an assumed elasticity. Fitted
and feasible-policy modes are rejected.

A third policy, the recovery curve, is used by the lab (`app/lab/scenario.py`,
`simulate_recovery`). Every monthly change in the fuel price starts its own clock,
and a curve gives the share of that change, times gallons burned, that returns as
revenue by quarter after the change. One curve is drawn per path. The lab plugs in
three curves on the same simulated worlds: no response, United's own stated path,
and the curve measured from United's reported quarters (`app/lab/fare_clock.py`).
The response is symmetric and is defined on revenue net of volume effects, so no
elasticity, pass-through cap or step limit is applied on top of it. The measured
curve's one-year share is the stable number (about 62% at the middle of the draws,
49% to 91%; the single best fit is 75%); its speed is not pinned down, and the
pre-specified five-lag version lost its locked test. The
two earlier policies are unchanged byte for byte.

Pre-cutoff EIA return volatility is scaled by sqrt(21). Mean reversion, demand
noise and correlation are assumptions. Spot changes transmit dollar-for-dollar
to Q2 average paid fuel. Quantiles are calculated after each path is accumulated;
pointwise quantiles are never summed. Cash-floor fractions are conditional
simulation fractions, not calibrated probabilities.

Repeated-seed Monte Carlo error and bounded convergence checks are implemented in
`docs/convergence.md`, with fixed production assumptions and separate policy results.
Exact incremental batches extend the study budget to one million paths per repeat.
The retained-aircraft example meets all 200 checks at 500,000 and 750,000 paths,
with unchanged tolerances and two passing counts required. Its original 10,000-path
run still fails 45 checks. Parameter uncertainty, structural uncertainty and interval
calibration remain pending. Numerical sampling stability does not establish
forecast accuracy.

## Time, provenance and replay

Only verified records published by the cutoff enter the model. Projection months
start after the cutoff month. June 30 cash is carried unchanged into the projection;
intervening cash movements are unknown. EIA availability is reconstructed from
observation dates; historical release timestamps and revisions are unavailable.
This is not a point-in-time historical backtest.

Parquet snapshots are content-addressed. Run records preserve evidence, fuel,
configuration, seeds, code digest and Python/NumPy versions. API/CLI exports now
also reference a locally registered executable source bundle. Replay uses that
bundle in an isolated process and checks every deterministic result field.
See `model_code_archive.md` for archive transfer, environment checks and older
unavailable versions. No full-engine historical fit, alpha or business outcome
coverage is claimed.

Manifest hashing treats integral JSON values such as `1` and `1.0` equivalently,
so browser-exported runs retain the same ID. Changes to numeric value, source,
configuration invalidate replay. Later working-tree implementation changes do
not invalidate a run whose original executable bundle has been preserved;
altering that bundle or losing it does.

## Joint-driver failure workflow

`failure_search.md` describes the implemented finite-grid What breaks? workflow:
absolute cumulative FCF or path-minimum cash thresholds, common worlds, two pricing
modes, exact failing tested-point runs, full-scenario witnesses and preserved-input
replay. This extends sensitivity exploration; fitted behavior and historical
validation remain pending. Grid counts are not event probabilities.

## Operating targets and optional valuation

`target_inversion.md` describes the implemented What must be true? workflow:
explicit absolute FCF/revenue or horizon-weighted margin targets, optional cash
floors and a fully supplied FCFF operating-asset valuation scenario. It returns
all compatible tested combinations with actual joint matching path examples.
Valuation uses a separate assumed bridge; it does not alter monthly cash or imply
an equity price. This is sensitivity on the current assumed model, pending fitted
behavior, issuer cash reconciliation and held-out evaluation.

## Required next steps

SEC financial and BTS DD/FD operating-history pipelines now preserve raw sources
and dated normalized panels. BTS history includes scheduled operating-carrier
traffic, not reconciled issuer or United Express totals; it does not yet feed the
simulation. See `operating_history.md` for schema, geography and release boundaries.
The Form 41 importer now preserves P12/P52 quarterly cost/operations and P12A
observed monthly fuel history. See `form41_history.md`: these are acquisition-dated
current downloads, with separate certificate/financial-region/aircraft grains,
not historical publication vintages or automatically reconciled SEC issuer totals.
They do not yet feed the simulator's costs, productivity or delivered fuel basis.

See `proposal_delivery.md`: complete remaining Form 41 schedules/fares/weather and carrier-boundary
reconciliation; fitted fleet productivity
and regional mappings; gross purchase/delivery and financing queues; detailed cash
accounting; fitted policy/demand/competition; extend the inverse workflows to those
models and validate the implemented one-page thesis handoff; reviewed AI patches; uncertainty and
the historical scorecard.

## Separate fitted utilization study

`docs/utilization.md` records the first quarterly P52 component, fitted on 2013–19
and compared on eligible 2020–23 observations. It remains a research estimate
with missing/sparse samples, no locked-test score or calibrated intervals, and no
change to this simulator's capacity assumptions. The browser shows its source
and scorecard separately from the paired scenario outputs.

## Explicit retirement extensions

Optional retention links can postpone inferred mainline net exits when mapped
replacements are delayed. Enter notice, extension and simultaneous count limits,
explicit assumed maintenance/crew support, additional maintenance/carrying costs,
productivity and extra fuel burn. Limits can leave requests partially applied or
unapplied, with reasons. Existing aircraft are conserved; lower productivity caps
operated capacity without adding committed schedules. Upkeep is charged while
retained, including idle/outage months; extra fuel scales with productive operated
share and enters the assumed pricing-pressure rule. Costs affect operating profit
and the same provisional OCF/tax bridge. Purchase cash timing stays unchanged.
These fixed assumed actions are shared across pricing modes; crew rosters, verified
maintenance eligibility, optimal/fitted retirement behavior and historical
intervention validation remain pending. See `retention.md`.

## Monthly traffic research component

The separate `monthly-traffic-0.1.0` component estimates served certificate traffic
with release-aware expanding state-space fits. It does not change this financial
simulator or its assumed pricing. On 1,800 common selection targets per model,
its best state candidate loses to a fixed initial seasonal mean; peer pooling adds
no skill. Native intervals are uncalibrated and the selected source archives leave
6–20-month information gaps. Source/statistical reconstruction passes 86,733
independent checks. Joint fare/demand, pricing/competition and supported integration
remain pending. See `monthly_traffic.md` for methods, immutable records and results.
