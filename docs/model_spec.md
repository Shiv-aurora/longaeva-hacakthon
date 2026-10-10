# Model specification · V1, 2026-10-08

## Research question

For a given daily Gulf Coast jet-fuel cost shock, how large is United's indicative operating income effect **relative to a no-shock counterfactual** when pricing response is delayed and adjustment-constrained?

## Historical anchors

United's Q2 2026 published statement supplies:

- 87,279 million available seat miles (ASM)
- 1,219 million gallons of aircraft fuel consumed
- 18.45 cents passenger revenue per ASM (PRASM)
- 20.25 cents total revenue per ASM (TRASM)
- 18.99 cents operating expense per ASM (CASM)
- 83.4% load factor
- $4.19 actual average fuel price per gallon including expenses reported by the company

The independent EIA Gulf Coast spot series is **not** assumed equal to UAL's actual fuel bill. V1 assumes changes in that spot price transmit dollar-for-dollar to marginal UAL per-gallon costs, holding gallons fixed. That sensitivity assumption is uncalibrated.

Derived daily operating scalars, using 91 days in Q2:

```
Daily ASM (millions) = 87,279 / 91
Daily gallons (millions) = 1,219 / 91
Daily passenger revenue (USD millions) = Daily ASM × 18.45 / 100
Gallons per ASM = 1,219 / 87,279 = 0.013967
```

## Paired fuel scenarios

For each stochastic path `k` and day `t`, a common baseline `P_base[k,t]` evolves on weekdays as a mean-reverting log process with observed 65-trading-day log-return volatility. Weekends reuse the previous simulated weekday spot price solely as a modeling convention. This is **not a new EIA observation**.

```
log P_base[k,t] = 0.978 × log P_base[k,t-1]
                + 0.022 × log(mean(last 20 observed fuel prices))
                + sigma × N(0,1)

P_shock[k,t] = P_base[k,t] × [1 + shock × 2^(-t / shock_half_life)]
Delta fuel cost in USD millions per day
             = Q2_daily_gallons_million × [P_shock[k,t] - P_base[k,t]]
```

The baseline and shocked paths share noise. A nonzero shock is the intervention; its future persistence is an assumption. Clipping limits are numerical guards, not estimated physical constraints.

## Policy response and operating effect

The *required initial gross PRASM adjustment* for holding passenger revenue equal to incremental fuel cost, before any demand response:

```
Required gross PRASM change (%)
  = 100 × (DeltaFuelUSDPerGallon × GallonsPerASM) / PRASM_USDPerASM
```

The assumed target fare change is `pass_through_fraction × incremental fuel cost / daily passenger revenue`, capped for numerical stability. No repricing occurs before the selected response lag. Afterward, fare change approaches the target using a half-life and a maximum **0.6 percentage points per day** adjustment. This is an *illustrative policy rule*, not an estimated United response function.

The demand factor is `(1+fare_pct)^(-demand_elasticity)` and is constrained not to exceed 100% load factor relative to Q2's 83.4%. With ASM held constant, the passenger revenue change is:

```
Passenger revenue delta (USD M/day)
   = Q2_daily_passenger_rev × [(1+fare_pct) × demand_factor - 1]

Incremental operating income (USD M/day)
   = Passenger revenue delta - Incremental fuel cost
```

Aggregate cumulative impact across the selected horizon; report P10, median and P90 *across the assumed spot price processes*. The model holds nonfuel costs, ancillary revenue, capacity, working capital and route mix constant. It does not model taxes, capex, FCF, debt, competitor response, airline disruptions, or aircraft deliveries.

## What the metrics do and do not mean

- **Required fare increase**: arithmetic cost exposure in terms of reported PRASM, not a prediction that fares will rise that amount.
- **Modeled fare change**: policy assumption; without observed fare data, this is not an empirical pass-through estimate.
- **Incremental operating income**: sensitivity relative to paired no-shock path, not real earnings.
- **P10/P90**: conditional fuel-path dispersion, not calibrated business-outcome probabilities.
- **Daily diagnostic**: simple observation-matched public fuel price change versus user-supplied offered airfare quote changes for comparable route/cabin/lead-time; this is not an alpha or profit estimate.

## Technical nonclaims

V1 has no identification strategy for causal pricing elasticity, no empirical management optimization claims, no time-stamped source vintage reconstruction, and no out-of-sample positive financial forecasting result. Those belong to the Stage 2 validation work, not this demo.
