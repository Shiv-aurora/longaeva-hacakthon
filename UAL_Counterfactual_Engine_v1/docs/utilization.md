# First fitted component: quarterly aircraft utilization

`app/utilization.py` estimates **airborne hours per aircraft-day assigned** for
UA, DL and AA, separately for narrowbody and widebody passenger fleets. Both
numerator and denominator come from P52, avoiding the unresolved T100 passenger
traffic versus Form 41 all-service cost boundary. This is a statistical utilization
component, not an identified management policy or causal response.

The [official P52 definitions](https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMK&QO_fu146_anzr=Nv4+Pn44vr4+Sv0n0pvny)
define airborne hours and aircraft days assigned and supply aircraft lookups.
`data/aircraft_classification.json` explicitly maps their type descriptions to
NB/WB. That is our analyst classification, not a BTS lookup column. E190 and A220
are narrowbody mainline in these certificate carriers; no feeder attribution is
inferred. Type codes can combine variants, so no variant-specific performance is
estimated. Preserved lookup SHA-256 values are:

| Reference | SHA-256 |
|---|---|
| Aircraft types | `4fdb4909c671582f0267a730b7a486cab218d2405076d95c344e4d4b2f77d4c4` |
| Configurations | `c7dce259005536459b7f9886da5daf2d46143c0688322f54b807c3f7c54c0c4f` |
| Aircraft groups | `811f84eb347087c543ade43bb3fb17ed5a3e098fd052073ac176f20655c6abae` |

## Target and source gates

Sum P52 hours and aircraft days across detailed D/A/P/L regions, passenger/combined
configurations 1/3 and explicitly mapped types, then divide the totals. Do not
average region/type ratios or combine system/international aggregates with their
components. Type/configuration 999/9 equipment expense records are excluded.
Every retained target contains its original rows and source-line pointers.

Unknown aircraft types invalidate the certificate-quarter grouping rather than
silently shrink its fleet. Missing or negative hours/days withhold the entire group
quarter. Null is not zero. Require positive summed days and at most 24 airborne
hours/day. Individual zero-day rows are aggregated before division; two-decimal
thousand-unit source precision can round small exposures to zero. The artifact
records sensitivity to ±5 hours/days per row as quantization bounds, not statistical
confidence intervals or a repair to reporting.

There are 241 eligible 2013–2023 group-quarter observations and 23 exclusions:
17 in fitting and six in 2020–2021. Missing/negative reports exclude important
disruption quarters, including United 2020-Q2 and Delta narrowbody 2020-Q2/Q3.
The complete-observation evaluation cannot establish accuracy for those missing
targets. Fleet days are a reported exposure measure, not an audited inventory or
spare-aircraft count.

## Frozen design and temporal selection

`data/utilization_spec.json` declares the target, candidate grid, baselines,
features, temporal partitions, horizons and selection metric. Each distinct code
design is saved locally **before scoring** in
`data/utilization/designs/<design_id>/design.json`. This is local provenance, not
external preregistration. Subsequent display/status edits changed the code hash
without changing the fitting rules, fitted artifact digest or scorecard.

- Fit 2013–2019 only. Lag-4 requirements make 2014-Q1 the first fitted target;
  earlier observations initialize state. There are 116 eligible fitted targets.
- Regress hours/day on lag 1 and lag 4, carrier/group offsets and group-specific
  quarter seasonality. Ridge shrinks offsets toward common level/lag behavior.
  Standardization uses fitting rows only; the intercept is unpenalized.
- Compare penalties 0.1, 1, 10 and 100 against seasonal persistence and the fixed
  training carrier/group/quarter mean.
- Score rolling origins from 2019-Q4 onward on 2020–2023 targets, at 1/2/4 quarters
  (3/6/12 months). Multi-step predictions recurse through their own intermediate
  values and never use observations after the origin. Training coefficients remain
  frozen throughout selection. Predictions are bounded to [0,24].
- Withhold each target from every model if the actual target, recursive lag or
  baseline input is missing. No model-specific fallback or imputation is used.
- Choose the penalty with the lowest equal-weight mean of 18 carrier/group/horizon
  MAEs; targets within each cell have equal weight. Ties use the first declared
  penalty. Overlapping horizons do not create independent observations.

2024+ targets are excluded before target-value access and remain locked for
forecast testing. Quarterly totals are not divided into synthetic monthly targets;
the one-month proposal horizon needs separate observed inputs and evaluation.

## Verified result and limits

Snapshot `92e36863d2134a6f44f1152684580f41ff7d1f9170f529a908a0f965b76aabba`
preserves coefficients, standardization, source snapshot, specification and
classification digests, training target keys/digest, individual forecasts,
exclusions, source rows, per-cell/regime scores and builder bytes/hash.

| Model | Selection equal-weight MAE, hours/day | Common comparisons |
|---|---:|---:|
| Ridge 0.1 | 1.3683 | 196 |
| Ridge 1 | 1.3036 | 196 |
| **Ridge 10 — selected** | **1.1232** | **196** |
| Ridge 100 | 1.1865 | 196 |
| Seasonal persistence | 2.0712 | 196 |
| Fixed training seasonal mean | 1.5193 | 196 |

The selected component beats both declared baselines on this eligible selection
set. This is not locked-test or whole-engine validation. RMSE, signed mean error
and separate disruption/recovery scores are preserved. The selected penalty is not
best in every regime; the declared combined criterion determines selection.

Training counts range from nine for United widebody to 24 for both American groups
and Delta widebody. Sixty-eight of 264 potential selection comparisons are withheld.
The smallest cell is Delta narrowbody four quarters ahead, with only four targets.
Regime summaries use cells with observations and can have different cell counts.
No intervals, CRPS, calibration, confidence, causal policy or alpha claim is made.

The simulator still assumes ASM productivity and spare capacity. Hours/day alone
cannot establish ASM per aircraft, inventory availability, market allocation,
monthly seasonality, crew or hub ceilings. Integration requires those reconciliations
and executable-version preservation. Fitted demand, pricing and competition remain
separate required modules.

## Commands and verification

```bash
python scripts/fit_utilization.py
python scripts/fit_utilization.py --form41 data/form41_history/<snapshot_id>
python scripts/audit_utilization.py
python -m pytest -q
```

The explicit preserved-input rebuild reproduces the final snapshot without network
access. The independent audit reproduces all 241 targets from 6,472 original P52
records using Decimal arithmetic, checks exact line sets and recalculates all six
model MAE scorecards. Source witnesses and code hash are in
`data/utilization_checks/<snapshot_id>.json`.

Eleven tests cover reporting aggregation, missing/invalid quantities, physical and
identity gates, locked-target exclusion, known-system prediction recovery, recursive
future-data isolation, common-sample scores, frozen training coefficients, sample
requirements, immutable rebuild, corruption and API failures. The full suite has
117 passing tests.

`/api/utilization/status` verifies the saved manifest, estimate, builder and local
design before returning cached scores. Browser requests do not fit models. The
monthly screen displays **Historical utilization study**, with source/sample/model
details in its disclosure. `screenshots/monthly-utilization-study.png` records the
verified display. October 2026 current-download history supports retrospective
reconstruction, not historical point-in-time forecasts.
