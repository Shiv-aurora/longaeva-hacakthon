# Three-minute demonstration — delivery, fuel and cash

## Before presenting

- Open the monthly workstation. Use the saved information cutoff of July 16,
  2026, a twelve-month horizon, 10,000 paths and the same seed across comparisons.
- Load the explicitly illustrative purchase and retention examples. Confirm their
  assumptions are visible: aircraft prices, financing, residual capex, replacement
  links and operating eligibility are not reported United forecasts.
- During rehearsal, select **Fuel only**, **Delivery delay**, **Combined** and
  **Combined · no retention** in **What if?**, then press **Run scenario** after
  each selection. Their inputs
  are fuel only (+25% for six months), delay only (thirty mapped
  narrowbody aircraft delayed nine months), and both together. Keep all other
  settings identical except for the chosen shocks/retention. The fuel intervention
  is +25% for six months, then decays with the model's two-month half-life; it does
  not drop abruptly to zero after month six. Save the verified comparison results
  outside the live form: selecting a preset makes earlier results stale rather
  than restoring a saved run. Use **Evidence** for the detailed assumptions and
  **Export thesis** for the handoff. The other two tabs are **What breaks?** and
  **What must be true?**.
- In the same browser tab, restore **Combined** and run the case. Before presenting,
  run the three-by-three fuel-versus-delivery-delay failure and target grids,
  using their displayed saved-input labels. Default axes test fuel 0–50% and delay
  0–12 months. The failure criterion is median absolute cumulative FCF below $0M;
  the target is median absolute cumulative FCF of at least $1,000M. These are
  analyst thresholds, not insolvency or consensus claims. Inspect a compatible
  target witness during rehearsal
  if one exists, then restore and rerun **Combined**. Prepare the notes below and
  export/verify its PDF and JSON. Preserve the prepared search results and downloads;
  do not reload the tab. Explain when showing rehearsed/saved results.
- During the presentation, perform one live **Combined** scenario run. Show the
  already-verified comparison table and prepared search/target results instead of
  executing several grids inside thirty seconds. Do not select other presets or
  click grid witnesses during the timed demo; those actions can change the current
  scenario and invalidate its export. Keep the verified PDF as a fallback. Rehearse
  the live run/export on the presentation machine; the segment timings below are
  a speaking outline, not a runtime guarantee.
- Do not run a large convergence study during the three-minute demonstration.
  Ten thousand paths support an interactive sensitivity display, not a claim that
  every reported quantile has passed numerical precision checks.

## Spoken walkthrough

**0:00–0:25 — the investor question**

“An airline can lose aircraft capacity while temporarily preserving cash because
purchase payments move later. Higher fuel and the costs of keeping older aircraft
can change that tradeoff. Our question is: which effect dominates under explicit
assumptions, and what evidence would make us change our view?”

**0:25–1:10 — What if?**

“This is United's monthly scenario workstation. The baseline uses dated company
disclosures and public fuel evidence. I am testing thirty mapped narrowbody
purchases delayed nine months and fuel prices twenty-five percent higher for six
months. The purchase and retention terms are illustrative assumptions.”

Run **Combined** live and show the rehearsed three-case comparison from
[submission_brief.md](submission_brief.md), clearly identifying its saved settings.
Point to **FCF impact**, **Capacity
change**, **Ending cash** and the cumulative FCF-impact chart. Under the verified
example's assumed constrained policy and 70% fixed nonfuel costs, median paired
FCF impacts are −$1,961.4M fuel only, +$355.7M delay only and −$1,608.5M combined.
Use the displayed result if settings have changed; do not read these values for
a different run.

“The comparison uses the same simulated worlds with and without the shock. The
chart shows the change from that reference, not United's total cash flow. Payments
and borrowing move together; previously paid deposits remain paid. In this example,
retention and our spare-capacity assumption preserve operated capacity, while
older aircraft add upkeep and fuel expense. Borrowing changes cash and debt, not
free cash flow. The ending-cash number is an absolute financed balance, not the
change in free cash flow.”

**1:10–1:40 — reveal the assumption that matters**

Point to the chart's frozen-pricing and assumed-response comparison. Explain the
rehearsed **Combined · no retention** result without changing the live scenario.
With otherwise identical
settings, the verified no-retention case loses 0.357% operated ASM and has median
paired FCF impact −$1,488.8M, versus −$1,608.5M with retention. Say that the
comparison was run and verified before the presentation.

“These are assumptions we can inspect, not behavior the model has learned. Keeping
older aircraft preserves capacity here, but it is not automatically the best
financial response. The result depends on additional fuel and upkeep, demand and
cost flexibility. These outcome bands reflect our assumed worlds; they are not
calibrated forecast intervals.”

**1:40–2:10 — What breaks? / What must be true?**

Show the prepared fuel-versus-delivery-delay grids, preserving their saved base
purchase and retention plans. State the absolute FCF threshold and target from
their saved labels. Show a prepared actual target path if available;
do not click a witness and silently replace the current combined scenario. Explain
that the grid and path were computed during rehearsal; do not claim a fresh run.

“What breaks checks which tested combinations cross my financial threshold. What must
be true asks which tested assumptions support my operating target. We retain an
actual path witness instead of inventing a scenario by combining unrelated
medians. Grid counts are not probabilities, and an empty grid does not exclude
solutions outside these tested points.”

**2:10–2:45 — evidence and thesis handoff**

Open Evidence briefly, then export the current combined scenario using the prepared
notes if the rehearsal established acceptable runtime. Otherwise show the verified
saved one-page thesis and identify it as the previously exported combined run.

“The analyst gets the assumptions and source dates, the scenario results, the next
observable checkpoint and the evidence that would overturn the thesis. The saved
run can be independently replayed. Our next checkpoint is the next published
United quarterly disclosure: deliveries, capacity, yield, fuel and cash timing.”

**2:45–3:00 — honest close**

“This is a working conditional scenario tool. Joint demand/pricing fitting, full
cash and fleet reconciliation, calibrated uncertainty and held-out financial
validation remain. Our contribution today is a transparent way to test an
investment thesis and expose what it depends on.”

## Thesis notes to enter

The values below fit the API field limits and can be copied directly. Select the
paired no-shock reference. Enter a genuine future review date after the saved
cutoff; do not describe it as a confirmed earnings date without checking the issuer
calendar. Analyst notes remain a hypothesis, not an engine-verified fact.

| Field | Copyable value | Characters / maximum |
|---|---|---:|
| Title | Delivery timing and fuel: capacity versus cash | 46 / 70 |
| Hypothesis | Under illustrative purchase, funding, retention and pricing assumptions, delayed aircraft defer payments while fuel and older-aircraft costs can worsen cash flow. | 162 / 220 |
| Observable checkpoint | Compare the next published quarterly fleet guidance, ASM/RPM, yield, paid fuel, capex/deposits, funding and operating cash with the scenario assumptions. | 153 / 180 |
| Checkpoint source | https://ir.united.com/financial-information/quarterly-results | 61 / 180 |
| Falsification | Revisit if delivery timing, retention eligibility, paid fuel, yield, fixed costs, funding, deposits or working capital contradict the assumed mechanism or dominate its cash effect. | 180 / 220 |

## Questions to answer without overclaiming

| Question | Answer |
|---|---|
| “Does this predict United's earnings or share price?” | It computes conditional operating/cash consequences. It has no validated stock-price or alpha result. |
| “Are the distributions real-world probabilities?” | They summarize assumed stochastic paths. Calibration and parameter/structural uncertainty remain unfinished. |
| “Did you fit pricing from historical data?” | Not in this simulator. Pricing and demand responses are assumed; the separate fitted traffic study remains research-only after losing to its strongest baseline. |
| “Why could a delivery delay preserve cash?” | Settlement can move later, together with borrowing; deposits can stay paid. Operations and retention costs can offset that effect. The sign is a result to inspect, not a built-in financial conclusion. |
| “Why is capacity unchanged in the main example?” | Explicit assumed retention and spare capacity absorb the delay. Disabling retention reduces operated ASM; it does not prove identical revenue or unconstrained-demand loss. |
| “Does this example demonstrate a cost-assumption sign reversal?” | No. Moving fixed costs from 70% to 95% does not change paired FCF when capacity is preserved. The no-retention comparison becomes more costly but remains negative in the combined case. |
| “Is the cash floor a default prediction?” | No. It is a chosen threshold in a provisional cash bridge, without complete covenant or bankruptcy modeling. |
| “What is independently checked?” | Software, accounting, source extraction and saved-run replay have checks. Those checks do not establish forecast or investment performance. |

For the complete boundary, use the [model card](monthly_model_card.md) and
[Phase 1 delivery checklist](proposal_delivery.md). The concise written submission
is in [submission_brief.md](submission_brief.md); the demonstration checks and
saved-run references are in [demo_case_verification.md](demo_case_verification.md).
