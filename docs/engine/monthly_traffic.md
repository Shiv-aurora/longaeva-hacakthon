# Release-aware monthly traffic estimation

The proposal's page 3 asks for a parsimonious state-space demand model with partial
pooling and explicitly assumed elasticities where identification is weak. The first
monthly component now estimates **served scheduled-passenger certificate RPM** for
UA, DL and AA across the four declared markets. These observations provide a demand
proxy. They do not identify unconstrained demand, fare elasticity, realized yield,
regional marketing boundaries or a management response. Pricing in the financial
simulator remains an assumption.

**Source review pending:** a subsequent [rolling-release audit](traffic_vintages.md)
found material AA January 2021 revisions. The final certificate targets imply Q1
RPM above American's reported consolidated total, including regional flying.
These same targets affect this annual-only study. Its scores remain immutable,
but their economic interpretation is provisional until the source and reporting
boundaries are reconciled. A subsequent [original-record trace](traffic_revision_trace.md)
confirms complete prior-year payload reuse in the later DD cohort and temporary
FD release, with independent quarterly T2 support for the earlier domestic data.
Source arithmetic checks do not establish validity.

## Data and availability

The immutable source is operating snapshot
`bc4ee21c76691e623cea52e775808bde23689acdf9c763da9c2e22fb2f1979fc`.
There are 1,584 positive eligible carrier-market-month observations through 2023,
including 84 initial months per series (2013–2019). Unmapped/cross-region rows are
retained as exclusions; zero log targets are excluded explicitly rather than filled.
Inputs must satisfy nonnegative RPM/ASM and RPM ≤ ASM. No financial series is
divided into fabricated monthly observations or used as a fare denominator.

A target is complete only after **both selected DD and FD archive releases** are
available, including a scope with no contributing segments. Its usable date is
the latest contributor/scope publication date. This prevents absent contributions
from being interpreted as zero before the complete source arrives. Original
contributor dates, scope hashes/dates and reporting entities remain in the record.

The initial sample becomes fully available on June 2, 2020, so January–May 2020
issue dates are withheld. The preserved archive set contains annual backfills:
the newest available observation at an issue date is 6–20 months old. This is a
limitation of this preserved source set, not an assertion that BTS always releases
traffic with that lag. Missing intermediate vintages prevent historical point-in-time
performance claims. The exercise is a release-gated reconstruction. In particular,
2020's later-released traffic cannot inform its own earlier forecasts.

Foreign-to-foreign flying and consolidated marketed regional operations remain
outside the source boundary. Their absence is not evidence of zero operations.

## Declared model and evaluation

`data/monthly_traffic_spec.json` and a source/environment hash are persisted in a
local design declaration **before** fitting. This is not external preregistration.

The observation equation is log RPM = carrier-market offset + market calendar-month
effect + local-level state + measurement noise. Twelve local levels follow a
Gaussian random walk. A market's three carrier innovations may correlate; the
declared candidate correlations are 0, 0.5 and 0.9. All have the same marginal process
variance. Calendar effects are shared within markets, with ridge shrinkage;
carrier-market offsets are unpenalized. Initial state mean is zero and covariance
is identity. Missing or unreleased dimensions receive no update. This follows the
linear Gaussian filtering/innovation-likelihood framework described by
[statsmodels](https://www.statsmodels.org/dev/statespace.html).

At every eligible month-end issue date, the fit expands using only observations
available by that date. Ridge regression estimates seasonal/offset coefficients.
Conditional innovation likelihood selects process variance from
0.0001 / 0.001 / 0.01 / 0.1 / 1 and measurement variance from
0.0001 / 0.001 / 0.01 / 0.1, separately for each correlation candidate. This is a
two-stage, finite-grid conditional likelihood fit; it is not a joint continuous
maximum-likelihood or causal estimate. Sixty candidate-origin fits select a grid
boundary, recorded as a limitation rather than silently expanding the design.

Filtering proceeds through the issue month, increasing uncertainty over the
unobserved release gap. Forecasts then propagate another 1, 3, 6 or 12 months.
The point prediction is the conditional lognormal **median**, not its mean. Native
P10/P90 intervals condition on fitted coefficients and variances. They omit parameter
uncertainty and are uncalibrated. Process covariance permits same-market peer
information sharing; it does not represent a fitted competitive pricing response.

The same 1,800 forecast comparisons are used by all three state candidates and
three declared baselines:

- Seasonal persistence uses the most recent released observation in the target's
  calendar month, with its exact source period/date recorded.
- The fixed initial seasonal mean uses the initial sample only.
- The regularized log autoregression expands with released data, using lag 1,
  lag 12, carrier-market offsets and shared market seasonality. It recursively
  bridges the release gap without future target values.

Each carrier/market has 42 / 40 / 37 / 31 targets at 1 / 3 / 6 / 12 months.
Candidate selection minimizes the equal-weight mean MASE across 48 cells. Each
series is scaled by its initial mean absolute lag-12 RPM difference. Raw RPM MAE,
RMSE, bias, sample counts and disruption/recovery scores are also saved. Horizons
overlap, so these comparisons are not independent samples. 2024+ quantities are
excluded before target access and remain locked for forecast scoring.

## Results: complexity does not beat the best baseline

Verified snapshot:
`a0b033bc04cc45408d4e01f94bf0a8d5776200322f71edf95ec95b5248e0521b`.

| Model | Equal-cell MASE | Equal-cell MAE (million RPM) | Comparisons |
|---|---:|---:|---:|
| Independent state levels; best state candidate | 9.863 | 1,954.569 | 1,800 |
| Moderate peer pooling | 9.881 | 1,959.054 | 1,800 |
| Strong peer pooling | 9.924 | 1,971.350 | 1,800 |
| Released seasonal persistence | 10.841 | 2,140.013 | 1,800 |
| Fixed initial seasonal mean; best comparison | 6.980 | 1,158.769 | 1,800 |
| Expanding regularized autoregression | 7.843 | 1,593.939 | 1,800 |

The state model improves on persistence but fails the stronger initial-mean baseline.
Pooling adds no selection-period improvement. This evidence does not support
deploying peer-state complexity in the financial simulator. The research view shows
that failure prominently. Recovering earlier vintages and adding a reconciled
fare/capacity/disruption observation system are next requirements; these results
are not a reason to tune against the locked period or claim that fitted demand is
complete. No forecast accuracy, causal policy or investment claim follows from a
passing implementation audit.

The independent audit also scores native conditional intervals with 80% interval
coverage/width/score and lognormal CRPS for each carrier, market, horizon and regime.
The CRPS formula is checked against the original
[scoringRules implementation](https://github.com/FK83/scoringRules/blob/master/R/scores_lnorm.R)
and a separate numerical CDF integration test. Best-state native coverage averages
**67.3%**, below its nominal 80%; by horizon it is 68.1% / 66.9% / 66.2% / 68.0%.
These are descriptive equal-cell selection scores without block uncertainty or
baseline distribution comparisons, not calibrated coverage guarantees.

## Verification and commands

```bash
python scripts/fit_monthly_traffic.py
python scripts/fit_monthly_traffic.py --operating data/operating_history/SOURCE_ID --spec data/monthly_traffic_spec.json
python scripts/audit_monthly_traffic.py
```

The complete offline rebuild produces the exact same artifact and snapshot ID.
Immutable JSON, two Parquet tables, source bytes and the design declaration are
saved before publishing the latest pointer. Status and downloads verify manifest,
artifact, source, declaration, row JSON and both Parquet readbacks; corruption fails
closed. This is current-code reproducibility. Full original executable/dependency
preservation for this new module remains broader R01 work.

Twenty new tests cover physical/release gates, missing-versus-zero scope evidence,
future and locked isolation, scalar filtering, peer update direction, synthetic
process/measurement variance recovery, release-gap uncertainty, recursive AR paths,
expanding/common samples, immutable JSON/Parquet, rehashed coefficient/likelihood
tamper, independent CRPS integration and unavailable/corrupt API states.

The independent audit imports no traffic estimator or source parser. It independently
sums **1,049,207 original segment records in 22 archives**, verifying all 1,584
target RPM/ASM totals, reporting entities, source hashes and complete-scope dates.
It solves an augmented ridge design separately from the production normal equations,
then checks every declared grid likelihood and posterior with sequential scalar
updates instead of the joint Cholesky/Joseph filter. Every forecast, baseline,
quantile, point score and selection rule is inspected. **86,733 checks pass**.
Audit record:
`data/monthly_traffic_checks/df60f1ecd8bb2beb539f75dd44b3b7782791d583487e5fea3c21cb21b8faac8b.json`.

The browser exposes the six-model comparison, all horizon counts, limitations and
complete fitting/release record. API routes are `/api/traffic/status` and
`/api/traffic/record`. Visual proof: `screenshots/monthly-traffic-study.png`.

At the original study milestone, the complete software suite passed **323 tests**
(one existing Starlette/httpx
deprecation warning). The browser download exactly matches the saved artifact
bytes and the offline rebuild. Its file SHA-256 is
`4b85ac29586dee9e9d7054b91014b8b910131f1c0813652846c594dfc18e62ef`;
its canonical artifact digest is
`ca8fc3c7fbaca8cef54ad76ebef5bcd23d412d880b127ce24c21ebc0c937c05d`.
The six score rows and final source-lag scope wording were inspected in the
research view, with no browser warning/error logs.
