# Fare/traffic matching gate before joint estimation

The Phase 1 proposal requires joint fare/demand estimation where supported,
explicit assumed elasticities where causal identification is weak, release-aware
2013–19 initial estimation, 2020–23 selection and locked 2024+ evaluation. The
DB1B acquisition does not by itself supply a matched pricing/yield target.

Before fitting, verify the following from the completed real fare artifact:

| Question | Required evidence / action |
|---|---|
| Are all declared quarters valid? | Full 44-quarter raw-record and native Parquet audit, not extrapolation from 2013-Q1 or synthetic fixtures |
| What was known at the issue date? | Per-quarter selected-release proxy and complete initial gate. Preserve current-download/hindsight labels; selected revised 2013–15 bytes cannot enter earlier issue dates |
| Is this the same geographic market? | Original airport/city/WAC extraction is running separately; its first quarter is independently audited and every fare-cell endpoint maps. Full 44-quarter geography audit remains required. DB1B is US-only; the simulator's domestic includes Canada, while its crosswalk places WACs 3/4 in Latin America and WAC 5 in Pacific. Pacific territories do not establish complete international coverage |
| Is this the same carrier boundary? | Keep ticketed brand, reporting carrier, mainline operation and regional marketing distinct. Review AA/US carrier transition rather than pretending a constant combined AA panel |
| Is this the same travel object? | A directional O&D fare market can contain several flown coupons. T-100 segment RPM/ASM and DB1B market passengers are different denominators. Even one-coupon matches can differ through international itinerary exclusion and sampling |
| Is apparent price movement a mix change? | Retain route/city IDs, coupon count and fare-basis mix. Declare a supported within-route or fixed-mix estimator, with entry/exit and sparse-cell gates; do not label a raw carrier mean a constant-quality price index |
| Are sample counts interpreted correctly? | DB1B weights are sampled passengers; market counts include multiple directional markets from one ticket. Cohorts overlap and cannot be summed or expanded into census quantities |
| Can quarterly data measure monthly state? | Use an explicit mixed-frequency measurement equation and availability gate. No duplicated quarterly labels, forward-filled monthly fare observations or invented monthly prices |
| Are elasticities identified? | Jointly estimated reduced-form covariance is not causal price response. Demand and price are endogenous; retain a separately declared sensitivity range when no defensible instrument/identification supports a policy coefficient |
| Does added behavior earn integration? | Shared-information seasonal, fixed-policy and regularized baselines; release-specific selection, regime/horizon counts, failures and ablations. Do not deploy solely because source arithmetic or fitting tests pass |

Do not manufacture revenue yield by dividing SEC consolidated passenger revenue
by sample DB1B mileage or certificate RPM. An explicit survey fare measurement can
inform a supported latent state while remaining separate from consolidated yield.
The full four-market/issuer boundary and cash-flow bridge remain proposal work.

Declare estimation and evaluation choices before numerical fitting. The evidence
may require separate measured fare and traffic proxies with uncertainty rather
than an unjustified exact observation match. Publish those limits and test their
incremental contribution; do not erase unsupported geography or carrier blocks.

The [first original endpoint and coverage checks](fare_geography.md) bind separate
raw fare/geography audits. They close the first-quarter endpoint-evidence question
only. All other matching gates remain active, as does the full proposal scope.
