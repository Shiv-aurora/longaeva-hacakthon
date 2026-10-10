# What must be true: operating targets and valuation scenarios

The Phase 1 proposal (page 4) asks for explicit revenue, margin or cash-flow target
inversion, followed by disclosed valuation assumptions and a set of compatible
operating paths. `app/target_inversion.py` implements that workflow on the current
assumed monthly model. Fitted pricing, competitor feedback and the detailed cash
bridge remain requirements; this is conditional sensitivity, not historical validation.

## Target search

`POST /api/monthly/target-inversion` accepts `base_scenario`, two bounded `x`/`y`
axes, an operating `target`, and optional `valuation`. Axes support the same six
operating drivers as failure search. Ranges, discrete delay months, 2–7 points per
axis, 10,000-path maximum and 6 million path-month-grid budget are validated before
simulation. Every tested point and both pricing modes reuse identical seeded worlds.

The target metric is absolute horizon cumulative FCF, absolute horizon cumulative
revenue, or operating margin in percent. Margin is computed pathwise as total
operating income / total revenue × 100, not a mean of monthly margins. It supports
inclusive `at_least`, `at_most` and `within_band` conditions. Band tolerance is in
USD millions, or percentage points for margin. P10/median/P90 is chosen explicitly.
A minimum-cash condition can also be supplied; path minima include opening cash.

The operating-statistic test is evaluated first. Optional cash and enterprise-value
statistic conditions are then evaluated. A compatible grid point must pass every
requested statistic test **and** contain at least one actual path jointly meeting
all path-level constraints. A matching interpolated median with no matching path
is not declared compatible. Counts show operating-target points separately from
points meeting the joint tests. Empty results mean no tested compatible assumptions;
they do not exclude a solution between points or outside the chosen ranges.

All compatible driver combinations are returned. Each cell retains up to five
actual matching path examples with stable zero-based path indexes, monthly
operating/cash trajectories and optional FCFF. The first matching indexes are
examples, not likelihood-ranked or representative trajectories. Path fractions
remain uncalibrated conditional simulation fractions; the grid has no probability
weights. A selected quantile can fail even when individual example paths meet the
conditions. No unique inverse solution or forecast is claimed.

The UI uses the current What if? settings, overrides its two tested drivers, shows
both policy matrices, opens individual full paired scenarios and displays sample
path rows. The chart continues to show relative FCF impact; target quantities are
absolute. Imported guidance, analyst consensus and observed market prices are not
substituted for the user's explicit target.

## Optional operating-asset valuation

Valuation requires all eight inputs: effective annual cost of capital, terminal
annual growth, first full terminal-year FCFF, annual noncash charges, annual
working-capital investment, cash operating tax rate, enterprise-value target and
its tolerance. The UI leaves these assumptions blank until supplied. They are not
reported UAL figures or silently inferred market inputs.

Use a separate FCFF bridge, rather than discounting the aggregate OCF-minus-capex
metric at WACC. This follows the [cash-flow/discount-rate matching principle](https://pages.stern.nyu.edu/~adamodar/New_Home_Page/lectures/val.html)
and [FCFF valuation framework](https://pages.stern.nyu.edu/~adamodar/pdfiles/eqnotes/fcff.pdf):

```
monthly cash operating tax = max(monthly operating income, 0) × supplied tax rate
monthly FCFF = operating income - cash operating tax + annual noncash charges / 12
               - modeled cash capex - annual working-capital investment / 12
explicit PV = sum(FCFF_month_t / (1 + annual discount rate)^(t/12))
terminal value at horizon end = first full terminal-year FCFF / (discount rate - growth)
operating-asset value = explicit PV + terminal value / (1 + discount rate)^(horizon/12)
```

Growth must be below the discount rate. No immediate cash tax credit on losses,
month-to-month tax netting or tax carry-forward is fitted. Noncash charges and
working-capital investment are held constant as supplied annual rates. The terminal
input is already the first full future year's FCFF; it is not multiplied by growth
again or inferred from the final simulated month. Stable growth begins immediately
after the selected horizon, an explicit strong assumption. Months 13–24 remain
exploratory. Terminal value and its discounted contribution are shown separately.

This value covers operating assets. Cash, nonoperating assets, debt, leases, other
claims and shares are not mapped into an equity price. It is not a stock-price
target. These valuation assumptions do not alter the simulation's cash state;
reconciling FCFF, full working capital and financing remains required. Negative
calculated value is retained as a model inconsistency/stress result, not floored
into a positive recommendation.

## Run and replay

```bash
python scripts/run_target_inversion.py
python scripts/run_target_inversion.py --config target.json
python scripts/run_target_inversion.py --replay data/target_inversions/<run_id>.json
```

Config files contain `base_scenario` and `inversion`. The latter contains `x`, `y`,
`target` and optional `valuation`, matching the saved manifest. Manifests preserve
all settings, versions, code hash, Python/NumPy versions, cutoff, seed and source
hashes. Evidence and cutoff-filtered fuel accompany every exported record for
network-free replay. Replay verifies saved result integrity, recomputed results and
the manifest; runtime is excluded from result hashes.

The default reference run `0f996887e1b129a694a3a472842f96018f6296c05568b88a8caf040c6718b646`
uses a median 12-month absolute FCF target of at least $1,000M, fuel 0–40% and
demand -10–10%, with 25 points and 10,000 paths. Each pricing mode has nine tested
compatible points. Initial engine time was 2.374 seconds on this host. These are
sensitivity/software results, not forecast-performance statistics.

Browser verification reproduced that reference run and replayed its downloaded
record exactly. Selecting the assumed-policy fuel 0% / demand 0% point showed
five actual matching paths; the separate full scenario
`694a2853ef673963dd388ee80c06a88337921ffc143d824117b59707c446a415`
also replayed exactly.

An **illustrative, user-entered sensitivity** used a 10% cost of capital, 2%
terminal growth, $4,000M first terminal-year FCFF, $3,000M annual noncash charges,
$500M annual working-capital investment, 25% operating cash tax, $50,000M
operating-asset value target ± $2,000M and $5,000M minimum cash. These are not
reported UAL inputs or recommended valuation assumptions. Browser record
`f31c19a2dd1e6f2d807aa80a278e8fe1961b6f068e030836a79e6f36d56e7e81`
is preserved in `data/target_inversions/` and verified by offline replay. Of nine
operating-target matches per mode, two frozen and three assumed-policy points
meet all tests with actual joint paths. Terminal value is $50,000M and its
12-month discounted contribution is $45,454.5M; the UI discloses those separately.
The assumed-policy fuel 20% / demand 10% point has 4,752 joint matching paths.
Its P50 operating-asset value is $48,072.3M, with $2,617.7M explicit FCFF PV.
Full paired witness `2d6c99bc25a67cc55207cab8d3be2f42b475bbdfc0f9464d173941389a7b68c6`
replayed exactly and matches the search's ending-cash quantiles. These verification
examples are software evidence, not a likelihood ranking or forecast validation.
See `screenshots/monthly-target-inversion.png` for the verified screen.

Tests independently check monthly FCFF, loss taxes and discount timing; horizon
margin weighting; inclusive bands; multiple/empty compatible sets; actual joint
path requirements; full paired scenario witnesses; source cutoffs; API validation;
valuation assumptions and preserved-input/result replay.
