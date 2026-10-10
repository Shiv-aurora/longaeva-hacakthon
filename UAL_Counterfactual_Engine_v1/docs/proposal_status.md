# Phase 1 proposal status — October 9, 2026

The repo is a functioning simulation prototype with a substantial data foundation.
It is not yet the empirically fitted and validated system promised by the Phase 1
proposal. A completion percentage would obscure that difference: visible workflows
are much further along than calibration, financial detail and validation.

| Area | Current evidence | Remaining proposal work |
|---|---|---|
| Investor workflows | What if, bounded What breaks and What must be true; actual matching paths, explicit optional FCFF assumptions and one-page PDF/JSON thesis export from an independently replayed run | Full policy/driver coverage, reconciled valuation and analyst testing of the handoff |
| Monthly simulation | Four markets, 10,000 paired paths, two mainline groups, regional contract block, entries/outages, provisional OCF, optional funding/working-capital events and explicitly allocated purchase/deposit/funding cash schedules | Fitted demand/pricing/competition, crew/hub feasibility, complete reconciled gross delivery/retirement actions, fitted retention policy/costs and cash/debt/working-capital dynamics |
| Historical inputs | SEC quarterly financials; BTS monthly traffic; Form 41 quarterly costs and monthly fuel; audited reporting-entity join; T2 aircraft capacity targets and two-way P52 exposure comparison; 132 traffic archives reconstruct 48 release-specific histories with 3–6-month information gaps | Remaining fare/weather and schedule inputs, FF/service-scope reconciliation, regional/issuer mappings, independently recovered historical bytes/timestamps and remaining AA reporting-boundary reconciliation |
| Behavior and uncertainty | A fitted monthly served-traffic proxy remains research-only after failing the strongest simple baseline; assumed constrained/frozen policies and correlated fuel/demand worlds and repeated-seed numerical error studies. Retained example passes all 200 sampling checks at two consecutive budgets (500,000 and 750,000), while its original 10,000-path run still fails 45 | Observed-like fitted policy, parameter/structural uncertainty and calibrated intervals |
| Evaluation | 376 software/source/accounting/export tests; independently checked traffic/utilization/capacity targets; quarterly utilization/capacity fits and a monthly traffic study with the original 1,800-comparison annual study and a separately audited four-case source comparison on 1,763 common supported keys; 735,982 independent numerical/input checks for the new study | Full-engine 2013–19 estimation/2020–23 selection, frozen 2024+ scoring, baseline/ablation scorecard, intervention evidence and paper investment evaluation |
| Evidence/product delivery | Source hashes, dated inputs, inspectable assumptions and verified executable code bundles and archived replay | Source-passage AI review patches, runtime binary preservation, React implementation, analyst testing and finals package |

The first P52 utilization component is now fitted and compared with simple baselines
on the proposal's selection period; see `docs/utilization.md`. Its source exclusions
and sparse samples limit the result. T2 covered-aircraft capacity now has a frozen
2013–19 fit and direct-versus-utilization × productivity comparison on 191 common
2020–23 forecasts. Independent original-CSV, regression, recursion and metric checks
passed. The selected model has lower average selection error than the direct model
and two baselines, but United narrowbody has only one usable 12-month comparison.
Exposure gaps, whole-fleet boundaries and monthly integration remain unresolved;
this is not full-engine or locked-test validation. See `docs/quarterly_capacity.md`.
The recovered release-specific histories and a narrow AA whole-cohort correction
now have an independently audited four-case fitted comparison. The next model
milestone is matched fare observations, remaining exposure/reporting support and
pricing/demand evaluation before integrating supported fitted behavior. Import coverage and passing tests cannot substitute for forecast
evidence. The reporting panel now exposes the financial/geographic differences
that must be handled before interpreting cross-schedule coefficients.

The uploaded **Longaeva Hackathon — Datasets.pdf** is a catalog of available sources
and access options. It does not itself contain the observations or establish access
to each listed provider. The current imports are separately acquired public inputs.
Actual organizer-provided files will need their own schema, coverage, timestamp and
boundary assessment before they can replace or supplement these inputs.

The [official Longaeva challenge](https://www.longaeva.com/hackathon) asks for a working
forward simulation anchored in observable signals, with interpretable driver effects
and a repeatable framework. It also calls for a live demonstration, investment-team
testing and an implementation/scaling plan at finals. Our prototype addresses the
simulation/interface foundation and now implements a one-page analyst thesis handoff; the full proposal's fitted behavior, validation
and delivery commitments remain active work.

`docs/proposal_delivery.md` is the complete requirement-by-requirement checklist;
`docs/reporting_panel.md` records the data-alignment milestone and `docs/utilization.md`
records the first fitted component. `docs/t2_history.md` records the capacity/exposure
source milestone and its remaining deployment boundary; `docs/quarterly_capacity.md`
records the audited capacity comparison, sparse sample counts and deployment limits.

The handoff exports scenario distributions, modeled constraints, paired-reference
disagreement, source links, a user-supplied checkpoint and falsification evidence.
Both downloaded formats matched their immutable hashes; the 10,000-path browser
export and CLI produced the same artifact, and run/derivation/PDF replay passed.
It uses the current assumed model; fitted/validated inputs and investment testing
remain required. See `docs/thesis_export.md`.

The capacity exposure follow-up found positive recorded traffic with missing
assigned days/hours and no same-key P52 replacement (UA 2019-Q4, T2 line 214782).
The exclusions remain intact; they cannot be repaired by treating blanks as zeros.
The preserved witness is `data/capacity_exposure_checks/bb26714271d80be563ac4d4c2271bed9e74da7b107583449741fa007829a074d.json`.

The optional incremental cash ledger now settles explicit analyst events against
reported opening balances, with debt/revolver limits and separate restricted cash
and investments. Borrowing changes cash and debt, outside FCF and earnings. Both
inverse workflows use the financed state; thesis exports use pathwise FCF sums.
The 10,000-path browser/CLI example matches and archived replay passes. This does
not resolve the aggregate reference OCF, fitted collections, consolidated PPE/lease
accounting, complete gross delivery funding or maturity/covenant forecasting. See
`docs/cash_bridge.md`.

Explicit purchase allocations now connect the same fleet queue to deposits,
delivery payments, assumed borrowing/fees and next-month cash interest. An exact
30-aircraft, nine-month delay can split a batch without duplicating aircraft or
capex. The residual capex assumption replaces aggregate capex in both paths.
A 10,000-path browser/CLI case matches, 30 additional tests pass, and an independent
Decimal calendar/accounting audit passes 1,421 checks. Unallocated entries, future
settlements and retained deposits remain visible. This is a partial analyst
allocation; the complete gross queue, lender feasibility, retention costs and
consolidated PPE remain open. See `docs/purchase_bridge.md`.

Explicit assumed retention actions now postpone linked mainline net exits during
replacement delays, bounded by notice, extension, simultaneous capacity and stated
maintenance/crew support. Productivity and extra fuel affect the operated fleet;
additional upkeep/carrying affect the same OI/OCF/cash paths. Unapplied requests
remain visible. The illustrative case has 146 retained aircraft-months; 27 new
tests and 2,287 independent calendar/accounting checks pass. This is a fixed
analyst sensitivity, not a fitted retirement policy or verified maintenance/crew
clearance. Complete gross retirement reconciliation and operational fitting remain
open. See `docs/retention.md`.

Repeated-seed sampling checks expose numerical error at every supported
1/3/6/12-month checkpoint for both pricing modes. Exact incremental batching now
extends the budget to one million paths per repeat. The retained example passes all
200 checks at 500,000 and 750,000 paths under unchanged tolerances and stops at
750,000. Its original 10,000-path run still fails 45 checks. Complete archived replay
is required before local study reuse. Numerical stability is separate from fitted
uncertainty and calibrated forecasts. See `docs/convergence.md`.


The monthly traffic component now implements the proposal’s release-aware expanding
state-space approach for served certificate RPM, with three peer-correlation
candidates and three baselines. Its best state model loses to the initial seasonal
mean (MASE 9.863 versus 6.980); peer pooling adds no selection skill. The preserved
source set creates 6–20-month information gaps, and nominal 80% native intervals
average 67.3% coverage. It remains a reconstruction and is not integrated into the
financial simulator. Better source vintages and matched fare/capacity/disruption
observations are required; fitting software alone does not close M03 or V04. See
[the complete study](monthly_traffic.md).

The annual-only source gap now has a dedicated recovery pipeline: 110 additional
BTS rolling releases have been preserved, for 132 sources through 2023. The new
module reconstructs the selected revisions separately at each of the 48 issue
dates and records equivalent overlapping releases. Its source/Parquet/rebuild
verification is separate from model estimation; the old traffic scorecard is not
replaced by acquiring better inputs. See [rolling release reconstruction](traffic_vintages.md).

The rolling-release audit passes 312,457 checks and confirms 3–6-month information
gaps. It also reveals large AA January 2021 revisions: the final certificate data
implies Q1 RPM above American's reported consolidated total, which includes regional
flying. This unresolved source/measurement/reporting-boundary discrepancy affects
the earlier annual-only scorecard too. Accepted estimation requires tracing the
changed records and reconciling same-scope T1/T2 and issuer evidence. Faithful source
extraction does not establish economic validity; observations and immutable scores
remain preserved while this review is open.

The source discrepancy now has a concrete original-record witness: all 1,975
later DD AA January 2021 records reuse January 2020's non-date payloads. A temporary
FD release repeats all 659 prior-year records; April restores the earlier FD
cohort. Earlier Q1 domestic records match T2 scheduled-service RPM, ASM and
departures across all ten aircraft types. The independent trace audit passes
46,525 checks. Correction design, remaining boundaries and new fitted evaluation
remain required; see [the preserved trace](traffic_revision_trace.md).


## Current correction and fitting milestone

The previously pending AA whole-cohort correction is now saved separately and
verified against original source lines: 244,351 checks, 23 affected origin histories,
50 historical quantity changes and two evaluation quantity changes. Original
artifacts and scores are preserved. Regional, foreign-to-foreign and consolidated
reporting boundaries remain unresolved.

The separate four-case source study now has 735,982 independent statistical/input
checks over 126 fits. All six models/four cases share 1,763 supported comparisons
out of 1,800 requested; 37 AR failures per rolling case remain explicit. Reviewed
rolling state MASE is 6.833 versus regularized AR 6.069, with native conditional
80% coverage of 77.47%. Every state candidate comparison still loses to its strongest
baseline. These results supersede correction/refitting-pending statements above;
the earlier annual-only metrics remain their historical artifact's results.

The dashboard exposes the separately verified study and audit downloads. This is
hindsight research and conditional, uncalibrated uncertainty. D02/M03/V01–V04 remain
open for fare matching, scope reconciliation, pricing/competitor behavior, full-engine
baselines/ablations, parameter uncertainty, calibration and locked 2024+ evaluation.
R01 remains open for study Parquet/replay/runtime preservation. No requirement is
closed solely by passing software/source/numerical checks. See
[reviewed traffic methods and limits](reviewed_traffic.md).


## Fare acquisition and import in progress

All 44 public DB1B quarters for 2013–2023 are preserved (1.25 GB compressed),
with a separate quarterly US-only fare importer retaining ticketed/operating/
reporting roles, directional markets, overlapping brand/mainline cohorts and
quality/source-line provenance. The first real quarter passes 1,954,698 independent
original-record checks; eleven focused tests cover the parser, independent oracle,
Parquet/offline rebuild and failure gates. The full real import is still running,
and full-history/Parquet source audit, scope matching and joint pricing/demand
estimation remain required. This does not fill Canada/international or monthly
fare coverage, identify realized yield, establish causal elasticity or close M03.
See [fare source methods and current import](fare_history.md).

The complete software suite now passes 376 tests after the fare importer/oracle
changes. This is software evidence; the real 44-quarter import and full-history
source audit are still in progress.

## Original fare geography follow-up

A separate 44-quarter endpoint extraction is now running against the preserved
raw archives, with its design and original geographic convention frozen before
extraction. The independent first-quarter oracle checks all 3,046,411 tickets,
11,444,839 endpoint occurrences and 450 airport/city/WAC combinations. All 177,699
independently audited fare cells have exact endpoint evidence; Pacific-territory
and Latin America subsets remain separate from complete international coverage.
Unknown/conflicting mappings withhold assignment, and cohorts remain overlapping.
The full suite then passed 382 tests; three subsequently added coverage tests
also pass. Full real fare/geography history audits, travel-object/carrier/mix
alignment, joint mixed-frequency fitting and financial integration still remain
required for D02/M03. See [the source evidence and live handles](fare_geography.md).
