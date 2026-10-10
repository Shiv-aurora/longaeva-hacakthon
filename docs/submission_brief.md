# UAL Counterfactual Lab — submission brief

## Submission summary

UAL Counterfactual Lab is a working, reproducible airline scenario workstation.
It asks: **if expected aircraft arrive late while fuel becomes more expensive,
what happens to United's capacity and cash, and what assumptions could overturn
the conclusion?**

The prototype connects four geographic markets, two mainline fleet groups and a
separate regional contract block to monthly operations and a provisional financial
bridge. Explicit purchase allocations connect delivery timing to deposits,
settlement payments and assumed financing. Optional retirement extensions expose
the cost and capacity tradeoff of keeping older aircraft. Paired seeded paths
compare each intervention with its no-shock reference in the same simulated
worlds.

Three investor workflows turn the calculation into a reviewable decision:
**What if?** compares outcomes, **What breaks?** checks bounded combinations
against a financial threshold, and **What must be true?** finds tested assumptions
compatible with a target. A one-page thesis export records the scenario, sources,
assumptions, next observation and evidence that would overturn the analyst's view.

This is conditional scenario analysis, not an empirically validated forecast.
The financial simulator uses assumed demand, pricing, cost flexibility, retention
and financing responses. Its simulated P10–P90 ranges are uncalibrated; they do not
establish the probability of real-world outcomes.

## Why this fits the host's challenge

The [published Longaeva challenge](https://www.longaeva.com/hackathon) emphasizes a
forward company simulation, observable inputs, interpretable driver effects and a
repeatable framework. The prototype's strongest contribution is an inspectable
chain from an operating disruption to cash consequences, with preserved inputs
and executable replay. The demo demonstrates that chain and its sensitivity;
historical fitting, investment-team testing and broader scaling remain roadmap
work. No judging weight, prize outcome or investment alpha is claimed.

## Demonstration case and claim boundary

Use a twelve-month scenario with thirty mapped narrowbody purchases delayed nine
months, and a separate six-month fuel-price shock of +25%. Compare delivery only,
fuel only and combined shocks under the same saved assumptions. Compare frozen
pricing with the assumed delayed pricing response. Show the actual displayed
results rather than a prewritten forecast.

The illustrative purchase plan assumes forty MAX purchases split across September
and December 2026, $40M per aircraft, $4M per aircraft already deposited and $2M
more paid in August. It assumes 75% borrowing against the remaining settlement,
6% annual loan interest, a 1% financing fee and $100M monthly residual capex. The
months, prices, financing availability and residual are analyst assumptions.
The residual replaces aggregate reference capex in both paired cases; it must
cover unallocated capital spending and is not a United capex forecast.

The optional illustrative retention plan links eighteen A319/A320 net exits to
delayed purchases. Its replacement relationships, maintenance/crew eligibility,
notice and extension limits, productivity and additional costs are assumptions.
Neither those links nor the inventory guidance constitute a complete gross
delivery/retirement schedule.

For the verified 10,000-path, seed-2026 example, with 70% fixed remaining nonfuel
costs and the assumed constrained pricing response:

| Scenario | Median paired twelve-month FCF impact | Operated capacity change |
|---|---:|---:|
| Fuel only | −$1,961.4M | 0% |
| Delivery delay, with illustrative retention | +$355.7M | 0% |
| Combined, with illustrative retention | −$1,608.5M | 0% |
| Combined, without retention | −$1,488.8M | −0.357% |

Retention plus the assumed spare capacity preserves operated ASM in the featured
case; do not present it as a served-capacity loss. The retained fleet incurs 146
aircraft-months and $4.38M additional fixed upkeep/carrying expense, plus variable
extra fuel. Preserving capacity does not establish an optimal financial policy.
The combined no-retention case has less capacity but a smaller modeled FCF loss;
aggregate variable-cost savings and the demand buffer make that possible under
these assumptions.

The retained delivery-only case defers $340M capex and $255M borrowing over the
horizon and reduces purchase interest by $31.875M. Its median operating-income
impact is −$20.5M even though FCF impact is positive. The combined retained case's
median ending cash is $13,568.0M. That is an absolute financed cash balance; it is
not the relative FCF-impact number or a minimum-cash statistic.
See [the independent demonstration verification](demo_case_verification.md) for
saved-run references, export/replay checks and numerical precision limits.

Explain the mechanism without assuming its sign: earlier deposits remain paid;
delayed settlement also delays borrowing; capacity losses can reduce revenue;
retained aircraft can recover capacity but add fuel and upkeep costs. Near-term
cash preservation is not automatically better economics, and financing cash is
not free cash flow. Raising fixed nonfuel costs from 70% to 95% does not change
paired FCF impact in the capacity-preserving preset; without retention the
combined impact changes to −$1,522.1M. No sign reversal is demonstrated by these
presets. More disruptive cases can be structurally sensitive, as the model card
documents, but that separate example is not evidence about this case.

## Implemented and remaining

| Implemented in the prototype | Required before stronger financial claims |
|---|---|
| Monthly paired scenarios, fleet queues and four-market financial sensitivity | Joint fitted demand, fare and competitor response |
| Explicit partial purchase/deposit/funding schedules and constrained assumed retention | Complete source-reconciled gross fleet, purchase, PPE/lease and funding schedules |
| Incremental cash/funding ledger and visible limits | Fitted monthly collections, loyalty, working capital, debt service and covenant behavior |
| Finite-grid failure search and target inversion with actual path witnesses | Wider supported operational constraints and intervention validation |
| Source dates/hashes, saved assumptions, archived numerical replay and thesis PDF/JSON | Independent historical vintage recovery, reviewed AI source-passage workflow and complete runtime preservation |
| Software/accounting checks and separate historical component research | Full-engine temporal baselines, locked 2024+ evaluation, calibrated uncertainty and analyst testing |

The fitted traffic study remains research-only: it did not beat its strongest
simple baseline and is not deployed in the financial simulator. Separate
utilization/capacity studies also do not validate the full model. Passing source,
replay, accounting or software checks is not evidence of forecast accuracy.
See [the model card](monthly_model_card.md) and
[the complete Phase 1 checklist](proposal_delivery.md).

## Observation and falsification checklist

Use the next published United quarterly disclosure as an observable checkpoint;
the date is analyst-entered and must follow the saved information cutoff.

| Observation | What to check | What requires revisiting the thesis |
|---|---|---|
| Fleet guidance, purchases and reported fleet counts | Whether aircraft timing and net inventory changes agree with the assumed queue | Different delivery timing, replacement links or retirement decisions |
| ASM, RPM and passenger yield by disclosed geography | Whether capacity, traffic and fare changes are compatible with the scenario | Demand or yield materially outside the assumed response, or capacity unavailable for reasons not modeled |
| Fuel expense and reported paid fuel price | Whether spot-price transmission and retained-aircraft burn assumptions remain plausible | Paid fuel diverging from the assumed spot transmission or retained fuel intensity |
| Cash capex, equipment deposits and debt/lease disclosures | Whether settlement, deposits and funding timing agree with the illustrative plan | Refunded deposits, different payment terms, unavailable borrowing or undisclosed lease effects |
| Maintenance, nonfuel expense and operational disclosures | Whether the modeled cost flexibility and retention eligibility have support | Fixed costs persisting, extra upkeep rising, or crew/maintenance preventing retention |
| Operating cash flow, advance sales and loyalty/working-capital disclosures | Whether the aggregate cash bridge remains a useful sensitivity | Collection or working-capital movements dominating the modeled operating effect |

Recorded notes and links are analyst-authored. Exporting them does not verify their
claims. Differences are prompts to reconcile scope and revise assumptions, not
automatic causal proof or buy/sell signals.

## Roadmap

1. Reconcile issuer, operating-carrier, regional, travel-object and geographic
   boundaries; finish independent full-history fare/geography audits before fitting.
2. Fit demand/pricing and cost responses against declared simple baselines; deploy
   components only when their scope and evaluation support the use case.
3. Reconcile gross fleet/retirement, cash capex, leases, debt, working capital and
   operational feasibility; replace illustrative policies with supported estimates.
4. Run the proposal's release-aware temporal evaluation, frozen 2024+ tests,
   interventions/ablations and uncertainty calibration. Report failures as well as
   successes before claiming forecast or investment performance.
5. Conduct analyst task tests, add reviewed source-passage extraction and extend
   the reproducible company framework to additional firms with explicit scope and
   data-contract checks.
