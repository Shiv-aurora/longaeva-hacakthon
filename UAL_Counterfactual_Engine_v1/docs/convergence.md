# Numerical sampling error and convergence

The Phase 1 proposal (pages 2 and 5) requires repeated simulation until relevant
quantiles stabilize and Monte Carlo error is reported separately. The monthly
screen now offers **Check sampling stability** for its saved, verified scenario.
The production operating/cash engine and its fixed assumptions are unchanged.
This is numerical evidence conditional on that model, not fitted uncertainty,
historical forecast validation or an investment result.

## Contract and stopping rule

`ConvergenceSpec` accepts three to ten strictly increasing path counts, each
20–1,000,000, including the displayed run count; 8–24 seed repeats; positive absolute
currency, margin and breach-fraction tolerances; a relative quantile tolerance;
two or three consecutive passing counts; and 20–10,000 paths per rollout batch.
Worst-case requested work is capped at 300 million generated path-months before
adaptive stopping (largest count × repeats × months). Exact temporary outcome
files are limited to 12 GB and require a further 1 GB free-disk reserve. Unknown
fields, booleans used as numbers, nonfinite controls and unsupported budgets fail explicitly.

Defaults are 1,000 / 5,000 / 10,000 / 20,000 / 50,000 paths with 12 repeats. Currency
quantiles use the greater of $25M and 1% of the replica mean quantile; margin
quantiles use 0.05 percentage points or 1%; cash-floor fractions use 0.5 percentage
points. These are declared numerical stopping tolerances, not acceptance thresholds
estimated from forecast outcomes. Passing requires every monitored check at two
consecutive counts, at or above the displayed count. Otherwise the result is
**path limit reached**, with the failing checks retained. Tolerances are never
silently widened to obtain a pass.

Seed zero reproduces the displayed run's original seed. Other replica seeds come
from `SeedSequence([replica, retry, root_seed, 271828])`, reduced to the monthly
integer-seed range; duplicate seeds are retried. This is a recorded practical
pseudorandom-stream design, not a proof of statistical independence. The production
PCG64 generator and fuel/demand law remain unchanged. Increasing counts reuse
nested path prefixes **within** each replica. Version 0.2 advances one generator
per seed in recorded batches: earlier paths are never regenerated. Checkpoint
vectors are appended to private temporary files, then one complete vector at a
time is read for exact linear-interpolation quantiles. No approximate sketch is
used. Both successful completion and exceptions remove these temporary files.
Different batch sizes reproduce the same complete path hashes and numerical
statistics; resource controls remain part of the study identity. Independent-seed scatter is computed
**across replicas at a fixed count**, never across the dependent prefixes or months.
This use of mixed seed/counter inputs follows the approach documented by
[NumPy](https://numpy.org/doc/stable/reference/random/parallel.html).

Both pricing policies share the same worlds, controls and interventions. At each
supported 1/3/6/12-month checkpoint plus the chosen endpoint, the study checks
P10/P50/P90 of pathwise cumulative FCF, paired FCF impact, paired operating-income
impact, ending cash, minimum cash, cumulative revenue, response contribution and
horizon-weighted operating margin. It also checks the conditional cash-floor
fraction. No horizon quantile sums monthly quantiles; no paired impact subtracts
marginal quantiles. Frozen policy's response contribution is zero; the assumed
policy's contribution compares its paired shock impact against frozen pricing.

## Interpretation

For each quantile, sample SD across replica estimates is the estimate of sampling
error in **one** N-path run. It is not divided by the square root of the repeat
count. The displayed 1.96-times-SD half-width is an approximate pointwise diagnostic;
it is neither a simultaneous confidence guarantee nor a calibrated outcome interval.
There are only 12 default repeats, so the dispersion estimate itself is uncertain.
Replica means are averages of quantile estimates, not pooled-path quantiles.

For cash-floor fractions the half-width is also bounded below by the largest
pointwise 95% Wilson distance from each replica's fraction to its bounds. Zero or
all observed breaches therefore retain a sampling bound with stochastic worlds.
The score interval follows the [NIST formula](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm).
This is sampling uncertainty about the **conditional simulation law**, not an
empirically calibrated probability of a real event. With stochastic worlds disabled,
there is no Monte Carlo error.

Every level also compares quantiles/fractions with the preceding prefix count by
paired RMS change across replicas. Both this change and the numerical half-width
must meet tolerance. The first level cannot establish convergence. Stability is a
bounded operational stopping diagnostic; adaptive stopping does not confer interval
coverage or remove model misspecification. The two pricing models are reported
separately, not mixed into a probabilistic structural ensemble.

## Historical first study: the quick budget did not establish convergence

Verified source run:
`e1c222d28454743cc410c851c3d44f9b6a811d555943677a5dbfdd3986215c1e`.
Study: `808d77c64a642134b3f5f9086cd150abe3c6cc80cd2c10bfde257a70ec4b1072`.
Both use the illustrative purchase/retention plans, thirty narrowbody deliveries
delayed nine months, 25% fuel shock for six months, twelve-month horizon and the
same preserved July 16 information set. Replica zero exactly recovers all six
production summary checks, including true cumulative FCF and the breach fraction.

| Paths per seed | Checks outside tolerance (200 total) | Consecutive passing counts |
|---:|---:|---:|
| 1,000 | 200; no preceding-count comparison | 0 |
| 5,000 | 61 | 0 |
| 10,000 | 45 | 0 |
| 20,000 | 37 | 0 |
| 50,000 | 22 | 0 |

At 10,000 paths, assumed-policy twelve-month median cumulative FCF is $2,644.645M
with an approximate numerical half-width of $76.359M, above the $25.973M tolerance.
The P10 FCF half-width is $115.200M versus a $25M tolerance. In contrast, the paired
median FCF impact has $8.763M half-width and meets its $25M tolerance; the cash-floor
fraction's bound is 0.243 percentage points versus a 0.5-point tolerance. Common
world pairing can stabilize a difference while absolute outcomes remain noisier.

At 50,000 paths, P10 FCF still has $66.116M half-width and remains outside tolerance.
The study therefore records **path limit reached**, rather than claiming all relevant
quantiles stabilized. It regenerated 1,032,000 paths across nested counts/repeats;
the largest count represents 600,000 distinct prefix paths, not 1,032,000 independent
worlds. This preserved version 0.1 study remains valid historical evidence. The
new version 0.2 study below increases the budget without changing tolerances.
Parameter uncertainty, structural uncertainty and calibration remain separate
unfinished proposal requirements.

## Batched extended study: declared stopping tolerance met

Current source run (monthly-fleet-0.7.0):
`5b993f1c1f1bf79d53c4f2c1bd3b964484fab870158124ce7e91b429f377a443`.
Every deterministic numerical/data/output field agrees with the prior 0.6 source
run above, excluding model identity, manifest, archive receipt and runtime.
Current study (monthly-convergence-0.2.0):
`9c4c2f724ccd630a8ed5b0eaaad4430f52ebe84a3493d64416c7e9ee8a7c7834`.

The **Use larger path budget** control sets counts of 1,000 / 5,000 / 10,000 /
50,000 / 100,000 / 200,000 / 500,000 / 750,000 / 1,000,000 and batches of 10,000.
It leaves the number of repeats and all stopping tolerances unchanged. The same
funded-retention scenario stops at 750,000 paths per seed after two passing counts:

| Paths per seed | Checks outside tolerance (200 total) | Consecutive passing counts |
|---:|---:|---:|
| 1,000 | 200; no preceding-count comparison | 0 |
| 5,000 | 61 | 0 |
| 10,000 | 45 | 0 |
| 50,000 | 32 | 0 |
| 100,000 | 10 | 0 |
| 200,000 | 4 | 0 |
| 500,000 | 0 | 1 |
| 750,000 | 0 | 2 |

The 50,000-path gate differs from the historical study because its preceding
comparison is 10,000 rather than 20,000; the path outcomes and quantiles agree.
At 750,000 paths the assumed-policy twelve-month P10 cumulative FCF has an
approximate numerical half-width of $11.829M and paired RMS change of $4.988M,
both within the unchanged $25M tolerance. The source-seed estimate is −$1,390.482M.
The screen can inspect each sampling count; its main financial chart continues to
show the original 10,000-path run and its 45 failed checks are still disclosed.
This scenario's observed numerical stability is not a universal sampling guarantee.

Actual generated paths are 9,000,000; 19,392,000 prefix-path summaries are scored
across the levels, with 4,608,000,000 bytes of temporary checkpoint vectors at the
stop. Only 10,000 paths are rolled out together. A single `/usr/bin/time -l` CLI
measurement on this host reported 323.83 seconds wall time (322.441 seconds study
time), 1,257,684,992 bytes maximum resident set size and 370,672,528 bytes peak
memory footprint. These are distinct OS counters for one run, not a measured
latency percentile or a claim that total memory equals batch storage.

## Reproducibility and verification

```bash
python scripts/check_convergence.py --run data/runs/SOURCE_RUN_ID.json
python scripts/check_convergence.py --run data/runs/SOURCE_RUN_ID.json --spec numerical_settings.json
python scripts/check_convergence.py --replay data/convergence/STUDY_ID.json
python scripts/audit_convergence.py data/convergence/STUDY_ID.json
```

The API requires the displayed run record and independently replays it before
studying its exact preserved inputs. Runs from an earlier production core require
a current rerun. The study saves its own executable source bundle, seeds, every
replica quantile/count/path-outcome hash, every gate, stopping result and source-run
identity. Registered isolated replay recomputes all deterministic output, including
the stopping rule; an uploaded record cannot supply executable Python.

The full suite passes **343 tests**. Convergence and cache checks cover production
summary agreement with funding/retention and partial horizons, nested/distinct
streams, the single-run-versus-ensemble SE distinction, Wilson endpoints, pathwise
aggregation, frozen contribution, honest adaptive/limit statuses, invalid controls,
work caps, preserved-input cutoff, API/reference validation, independent-statistic
tamper rejection and archived replay after source changes. Additional checks verify
whole-versus-batched streams, funded-retention quantiles at awkward batch boundaries,
exact work counters, progress callbacks, disk shortage, truncated summary files,
cleanup on exceptions and replay-verified caching with rehashed tamper/stale-input gates.

The historical first study's independent audit passes **23,377 checks**. It imports no convergence functions:
Python scalar statistics and Decimal Wilson arithmetic verify every recorded
sampling calculation and stopping gate. Original-seed and next-seed witnesses at
every count independently aggregate production paths using Python sorting/linear
interpolation, checking all horizons, policies, quantiles, breach counts and outcome
hashes. Its report is
`data/convergence_checks/aae04a0e5f88cdcb29c065f56bdd33cab4bdd6023a5fe52edcc01d751c3f0fc9.json`.
Production paths still use the same financial engine; this audit does not establish
an independently specified financial model or historical forecast accuracy.

The browser's downloaded study is independently replayable and matches the CLI
study's deterministic content. Sampling results expose policy, horizon and quantile
selectors; scenario/tolerance changes invalidate results, and an in-flight response
cannot attach itself to changed inputs.

Browser verification also rejected a count list that omitted the displayed 10,000
paths, disabled the check and hid old results when the fuel input changed, and
restored the exact saved case after that input was returned to 25%. A frozen-policy
three-month P90 view showed zero response contribution; the final assumed-policy
twelve-month P10 view exposed the failed FCF/margin checks. No browser warnings or
errors were captured. Both browser downloads matched the CLI's deterministic
content and archive receipt; the first was independently replayed in full.
Visual evidence: `screenshots/monthly-convergence.png`.

## Extended-study verification

The new study's complete isolated archived replay passes. Its separate audit
passes **38,427 checks**, using scalar Python statistics, high-precision Decimal
Wilson bounds, and original/next-seed production-path witnesses at every evaluated
count. Witnesses use 4,093-path batches (different from the study's 10,000) and
Python sorting/interpolation, with complete path-outcome hashes and exact byte
counts. Audit record:
`data/convergence_checks/4df1e8d7d6d72040084f6ccfa43928d2c433791e68f42afd04a7ca44bd4983b9.json`.
The full replay plus separate witness audit took 609.46 seconds in one measured CLI
execution; this is verification time, not cached API latency.

The actual browser download
`/Users/shivamarora/Downloads/ual-convergence-9c4c2f724ccd.json`
(file SHA256 `8eda14c7e157459c31f43d46621133ee2a1cd6ce9f35171125abe78d44741623`)
matches every deterministic field and archive receipt of the completely replayed
CLI study. The screen reproduces the original 10,000-path P10 failures, the frozen
three-month P90 zero response contribution, and the passing 750,000-path P10 view.
A count list omitting the original run fails explicitly; changing the batch control
hides results and disables download. Restoring the exact controls reuses the
verified record. No browser warnings or errors were captured.
Visual proof: `screenshots/monthly-batched-convergence.png`.

## Verified-study cache

A local cache entry is admitted only by a **complete isolated archived replay**,
currently performed by `scripts/audit_convergence.py` before its separate statistical
witness audit. A checksum alone cannot admit an uploaded record. The API still
replays the displayed monthly reference before looking up the exact reference,
all numerical controls (including batch size), code and environment. It then checks
the registered code bundle, complete saved-file hash, deterministic-result hash and
receipt. Changed controls miss the cache; changed registered content fails closed.
A successful hit returns the original replayable record unchanged and an HTTP
header. The screen explicitly labels reuse. This avoids repeating a verified study;
it does not certify financial-model accuracy, and does not establish cached latency
percentiles, infrastructure cost or analyst usability.

### One-case cache benchmark

After the independent audit finished, 20 sequential warm localhost API requests
were measured with one excluded warm-up. All 21 responses were verified cache hits
and matched the replayed study's deterministic result and receipt. Recorded
HTTP-post-through-complete-byte-read latency was **p50 0.969 s, p90 0.994 s,
p95 1.021 s** (linear interpolation over these 20 observations). Each response was
1,520,364 bytes. This includes the full 10,000-path monthly-reference replay and
cache/source checks; it excludes client JSON decoding and rendering. It measures
this exact scenario/spec on this host, not a universal service percentile or SLA.
The measurement was made without the large sampling/audit jobs still running.
No broader concurrent/cold/unseen-case, infrastructure-cost or analyst-usability
result is implied. Raw samples, timing boundary, source/environment identities and
script hash are preserved in:
`data/performance/dc443e923911644e755e06a459093c8ce47e059b8cfc5130301214261dc10721.json`.
An independent sorted-order interpolation agrees with the saved percentiles.

```bash
python scripts/benchmark_study_cache.py --run data/runs/SOURCE_RUN_ID.json --spec data/convergence_extended_example.json --samples 20 --port 8001
```

Both the large-study and independent-witness temporary directory sets were empty
after completion. The separate RSS/footprint counters and cold study/replay timings
above remain specific measured cases; this cache benchmark does not replace them.
