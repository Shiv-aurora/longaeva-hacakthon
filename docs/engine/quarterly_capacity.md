# Quarterly covered-aircraft capacity study

The first T2 capacity experiment is fitted and independently audited. It is a
quarterly statistical component; the monthly simulator still uses its disclosed
capacity assumptions. The full proposal remains the delivery authority.

## Target and source boundary

The target is T2 all-service `Z` available seat miles divided by assigned
aircraft-days. Sum detailed financial regions and represented passenger/combined
NB/WB aircraft rows within each exact UA/DL/AA certificate before dividing. Two
alternative architectures forecast this target directly or as the product of
reported airborne hours/day and ASM/reported airborne hour. All three targets use
the same source rows and eligible quarters. P52-only exposure and quantity
discrepancies remain visible; they are not silently filled or interpreted as spare
fleet. These targets describe T2-covered aircraft, not whole owned inventory,
consolidated marketed capacity, geographic market capacity or an operational ceiling.

Source snapshot: `7f6abe2ff74d7aecea05156039ef79adac3603b89209d648c14323bb65e5b5aa`.
The [T2 source assessment](t2_history.md) records units, source gates, all-service
aggregation, forward/reverse P52 accounting and raw source preservation. This is a
current-download reconstruction acquired October 9, 2026, not an original
historical forecast vintage. The organizer dataset PDF is a catalog; the T2 inputs
were separately acquired from BTS.

Of 264 possible carrier/group quarters in 2013–23, 236 have all three eligible
components and 28 are withheld. A missing quantity on any represented aircraft
row withholds its entire group-quarter; missing values are never zero-filled.

## Frozen design and estimation

`data/t2_capacity_spec.json` was declared before model implementation and selection
scoring. A local content-addressed design record was persisted before fitting.
Neither record is external preregistration.

Fit 2013-Q1–2019-Q4 only. For each component, use an intercept, lag 1 and lag 4,
five carrier/group offsets and group-specific Q2/Q3/Q4 effects. Standardize using
common training targets only; leave the intercept unpenalized. Compare ridge
penalties 0.1, 1, 10 and 100 for both architectures. Each carrier/group must have
at least eight training targets. All three regressions use the same 107 target
keys: UA NB/WB 18/13, DL 18/21, AA 24/13. Training residual RMSE is retained for
audit and does not represent a forecast interval.

Selection uses rolling origins from 2019-Q4 through 2023-Q3 and horizons 1, 2 and
4 quarters. Coefficients stay frozen; intermediate steps use predicted values,
never post-origin observations. Capacity and productivity predictions are bounded
below at zero; utilization predictions are bounded to 0–24 hours/day. Decomposed
capacity is the exact product at every recursive step. Fixed seasonal means use
training observations only. Seasonal persistence uses target-minus-four-quarter
capacity, which is available at each origin.

Every model and baseline uses the same 191 forecast comparisons; 73 of 264
comparisons are withheld because target, recursive lag or baseline inputs are
missing. Exclusion reasons may overlap: 64 missing recursive states, 32 missing
seasonal references and 17 missing actuals. Selection minimizes the equally
weighted mean of all 18 carrier/group/horizon MAE cells. Declared architecture and
penalty order resolves ties. The loader excludes 2024+ before accessing its target
fields; no locked forecast scores or design adjustment using those outcomes were
performed.

## Selection results

Errors are thousands of available seat miles per assigned aircraft-day. Lower is
better. All rows below have 191 comparisons and 18 score cells; the complete eight
candidate scores, MAE/RMSE/bias cells and paths are saved in the artifact.

| Model | Equal-weight MAE |
|---|---:|
| Utilization × productivity, ridge 10 (selected) | 129.6 |
| Direct capacity, ridge 10 (best direct model) | 136.4 |
| Seasonal capacity persistence | 280.2 |
| Fixed training seasonal capacity mean | 187.4 |

This is selection-period evidence only. In particular, the equal weighting gives
United narrowbody's single 12-month target the same cell weight as AA's 13 targets.
It does not establish dependable United capacity forecasting or an advantage on
unseen 2024+ data.

| Carrier / group | 3 months | 6 months | 12 months |
|---|---:|---:|---:|
| UA narrowbody | 4 | 3 | 1 |
| UA widebody | 9 | 8 | 7 |
| DL narrowbody | 11 | 8 | 8 |
| DL widebody | 16 | 15 | 13 |
| AA narrowbody | 16 | 15 | 13 |
| AA widebody | 16 | 15 | 13 |

The 2020–21 disruption subset has 71 comparisons and only 14 of 18 score cells;
there is no UA narrowbody 6-/12-month or UA widebody 12-month disruption comparison
(and no DL narrowbody 6-month disruption comparison). Recovery has 120 comparisons
and all 18 cells. Their means use different cell coverage and should not be read as
a like-for-like regime comparison. Horizons overlap, so comparisons are not
independent samples. Missing United hours/days in 2019-Q4, 2020-Q1 and other
quarters eliminate many subsequent recursive forecasts.

## Reproduction and audit

From the app directory:

```sh
.venv/bin/python scripts/fit_capacity.py --t2 data/t2_history/7f6abe2ff74d7aecea05156039ef79adac3603b89209d648c14323bb65e5b5aa
.venv/bin/python scripts/audit_capacity.py
.venv/bin/python -m pytest -q
```

The offline rebuild from the preserved T2 snapshot returns the same estimate ID:
`72a29cede2fe2da887110d8ff2f3dfb3a1b0823d64a44ed70cb719734d476bca`.
The estimate saves its artifact, manifest and builder bytes; the design records
specification, code digest and NumPy version. It does not bundle an interpreter,
libraries or a quarterly executable replay environment.

The independent audit does not import the capacity fitting/forecast/score code. It
uses Decimal arithmetic on 5,061 original T2 CSV rows through 2023 to check target
eligibility, ratios and line pointers, independently fits all 12 component/penalty
regressions, and recomputes recursive paths, both baselines, all 1,910 forecasts,
73 exclusions and overall/regime MAE/RMSE/bias. Its immutable report is
`data/quarterly_capacity_checks/72a29cede2fe2da887110d8ff2f3dfb3a1b0823d64a44ed70cb719734d476bca.json`.

18 new tests cover source scope, locked-field isolation, physical/identity gates,
known component dynamics, training-only standardization, future-data poisoning,
recursive bounds, common samples and independent metrics, frozen coefficients,
missing-lag accounting, temporal/minimum-sample gates, deterministic ties,
immutable rebuild/corruption checks and API verification. The full suite passes
161 tests, with one existing Starlette/httpx deprecation warning.

`/api/capacity/status` verifies the cached artifact, builder and frozen design
before returning the scorecard. `/monthly#capacity-evidence` shows the best model
per architecture, both baselines, a prominent sparse United warning, per-series
sample counts and full source/design details. Browser verification confirmed the
estimate ID and rendered values, expanded the sample counts, and saved
`screenshots/monthly-capacity-study.png` without altering existing user tabs.

## Remaining proposal work

Resolve missing assigned exposure and whole-fleet/service-scope boundaries before
monthly deployment; establish supported monthly capacity and availability targets.
Fit demand, pricing, cost and management behavior; add crew/hub ceilings and funded
actions. Preserve a forward-declared frozen specification for locked and prospective
forecasts. Parameter/structural uncertainty, calibrated intervals, full-engine
baselines/ablations, intervention evidence, detailed financial cash bridge, paper
investment tests and analyst/product delivery remain required. This study does not
complete M04, V01, V02 or the overall Phase 1 proposal.
