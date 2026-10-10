# Joint-driver failure search

The proposal’s page 4 asks for combinations that breach negative 12-month free
cash flow or a chosen cash floor, with range and policy sensitivity disclosed.
`app/failure_search.py` adds this workflow to the current assumed monthly model.
It does not make fitted pricing, rival feedback or the full cash bridge complete.

## Contract and interpretation

`POST /api/monthly/failure-search` accepts a `base_scenario` plus two distinct
axes (`driver`, `low`, `high`, `points`), a `criterion`, `threshold_musd` and
`statistic`. The base has the same validated contract as the monthly simulator.
All unsearched drivers and assumptions remain fixed at that saved base.
Supported axes are fuel, demand, direct capacity, aircraft availability, regional
contract cost and net fleet-entry delay. Delay limits must be whole months and
produce distinct rounded month values. Hub and rival shocks remain unsupported.

Each axis has 2–7 equally spaced points. The search supports up to 10,000 paths
and 6 million path-month-grid combinations. Defaults use 25 combinations,
10,000 paths and a 12-month horizon. Reduce grid size for a 24-month search;
months 13–24 remain exploratory. Bounds and invalid requests fail before simulation.

The criterion is either cumulative **absolute** scenario FCF over the selected
horizon or minimum cash within each path, **including opening cash**. The selected
P10/median/P90 is computed after summing a path’s FCF or finding its cash minimum.
It is not a sum of monthly quantiles or a minimum of monthly quantiles. A point
fails when that statistic is **strictly below** the threshold. Equality meets it.
The cash bridge remains provisional; cash breach is not a bankruptcy prediction.

Generate random fuel/demand worlds once. Reuse exactly those worlds at every grid
point and for frozen and assumed constrained pricing. Compare each intervention
to its same-policy no-intervention control. Search axes do not change the world
process or its dependence parameters. Fleet rounding and cost flexibility can
produce non-monotone outcomes; no binary search or monotonicity assumption is used.

Failure-region summaries are exact contiguous runs of **tested x values** for each
fixed y value. A passing point between failing points remains a hole. No continuous
rectangle, interpolated critical threshold or global failure boundary is claimed.
Widen ranges and change grid resolution to assess sensitivity. Policy disagreement
counts show tested points whose failure status changes between the two pricing
modes. The UI provides both matrices and lets a point open its full paired run.

Tested grid points have no probability weights. Counts of failed combinations
are not event probabilities. Within-cell breach fractions describe assumed random
paths only and remain uncalibrated. Parameter/structural uncertainty and interval
calibration are still pending.

## Preserved runs

```bash
python scripts/run_failure_search.py
python scripts/run_failure_search.py --config search.json
python scripts/run_failure_search.py --replay data/failure_searches/<run_id>.json
```

Config files contain `base_scenario` and `search` (including `x` and `y`).
The browser can download an identical-format record. Manifests preserve both axes,
criterion, threshold, quantile, all base settings, model/search versions, code digest,
Python/NumPy versions, input hashes and seed. Raw evidence and cutoff-filtered fuel
observations accompany the result for offline replay. Result hashes include cells,
regions, counts and interpretation labels. Replay checks saved result integrity,
recomputed outputs and the full manifest. Runtime is excluded from result hashes.

## Verified reference run

`07b3a8d600e551dee2407a705f2213d6e4122ef16c0b2313dfe5a5a9fb56425c`
uses fuel 0/15/30/45/60%, demand -20/-15/-10/-5/0%, median 12-month FCF below
$0M, 10,000 paths and seed 2026. It reports 23 of 25 tested points failing under
each mode and zero policy disagreement points. This is a software/sensitivity
result; it is not historical validation. The initial measured engine time was
2.285 seconds on this host, not a latency percentile or performance guarantee.

Tests verify individual search witnesses against full paired scenario runs,
path-minimum cash quantiles, opening cash, strict thresholds, disconnected failure
regions, driver bounds, unsupported requests, compute limits, common seeds,
cutoff exclusion, API contracts and offline replay/tamper detection.

Browser verification also exported and replayed the default search from
`/Users/shivamarora/Downloads/ual-failure-search-07b3a8d600e5.json`.
Selecting fuel +30%, demand 0%, assumed constrained pricing produced full run
`d467719ef1d7a3a1449716e242606cade056ee50352c78e948af4d247086d5dc`;
its browser export replayed, and ending-cash/paired-FCF quantiles matched the
search witness exactly. The interface distinguishes the search’s absolute FCF
criterion from the chart’s relative FCF impact. Separate tests cover fleet-entry,
aircraft availability, direct-capacity and contract-cost witnesses.

The custom $7,000M minimum-cash median search was also verified in the browser
and replayed from its export. Preserved run `f7033b80129cd3f94a0efae5b8a22102f5c3723cbc33437a317f512b5232bae6` reports 19 of 25
frozen points and 18 of 25 assumed constrained points below that chosen floor,
with one policy-disagreement point. This is sensitivity to a user-defined floor,
not a revised estimate of default-event risk. Screenshot:
`screenshots/monthly-failure-search.png`. The complete software suite passes
79 tests; no historical forecast score is claimed.
