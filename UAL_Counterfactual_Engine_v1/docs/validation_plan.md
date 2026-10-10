# Validation against the Phase 1 proposal

The monthly four-market airline model is the target. Daily offered-fare snapshots
can support a pricing diagnostic, but are not a prerequisite for the proposal's
monthly financial model. The earlier daily-fuel pilot remains a separate artifact.

## Data and reporting boundaries

Preserve raw sources, hashes, retrieval dates, effective periods, release/filing
availability, units and transformations. Audit UAL consolidated/mainline/regional
boundaries and rival/residual capacity. BTS operating-carrier totals must not be
mistaken for consolidated marketed service. Missing history remains missing;
quarterly totals divided into months must be labeled derived run rates.

The SEC importer currently preserves standard entity-wide financial versions and
filing-date reconstructions. See `financial_history.md` for its definitions/gaps.
Traffic, Form 41 and the reporting-entity join are now imported and audited;
`utilization.md` records the first same-schedule fitted component and baseline
selection comparison. Fares, custom airline concepts, weather, historical fuel
vintages and the complete carrier mapping remain pending. Release dates alone are not proof of
original point-in-time availability; reconstruction results must say so.

## Temporal design

Use 2013–2019 for fitting, 2020–2023 for model/structural selection, and a locked
2024+ retrospective test after selection is frozen. Preserve a prospective forecast
log with configuration, data versions and resolution dates. Forecast at 1/3/6/12
months; treat months 13–24 as exploratory. Purge/embargo overlapping targets and
use time blocks for uncertainty estimates. Do not repeatedly tune on the locked
period. Data partition labels alone do not demonstrate a locked evaluation.

## Forecast baselines and scores

Compare seasonal persistence, a fixed-policy driver model, and a regularized
statistical forecast. Report target/sample/regime/horizon counts, MAE, CRPS,
interval coverage and width. Use quarterly financial reporting and observed
monthly traffic/fare outcomes where available. Baselines and the proposed model
must share target definitions, availability constraints and scoring dates.

Evaluate separately during fuel shocks, demand disruptions and ordinary regimes.
Regime-aware conformal calibration requires enough resolved, appropriate residuals;
otherwise display an uncalibrated conditional distribution. Report parameter and
structural sensitivity separately from world-path noise and Monte Carlo error.

## Mechanism checks and ablations

Check paired zero-intervention placebos, geography isolation, fleet/queue
conservation, stock-versus-flow identities, capacity/crew/hub constraints, delayed
entry cash timing and independent cash reconciliation. Test parameter recovery
and historically held-out disruptions; software identities cannot establish
behavioral validity. Ablate feedback, text evidence, uncertainty layers and
calibration. Check robustness to cost flexibility, policy response and carrier
mapping; profit-sign reversals must be explained, not hidden.

## Investment and product evaluation

Freeze a paper investment rule before evaluation, include trading costs and
no-signal/sector benchmarks, and separate investment performance from forecasting
accuracy. Test unseen scenario prompts, unsupported cases, stale inputs, latency,
analyst task completion and the one-page thesis export. AI extraction must produce
source-linked typed patches, deterministic validation and a reviewed diff outside
the simulation path loop.

Passing the current software tests is a mechanism/source check. Forecast accuracy,
calibrated coverage, causal policy identification and investment performance remain
unproven until this scorecard is completed with appropriate held-out observations.
