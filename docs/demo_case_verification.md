# Delivery and fuel demonstration: independent verification

Verified on 9 October 2026 against `monthly-fleet-0.7.0`. These are conditional scenario results, not forecasts or calibrated event probabilities. No fitted pricing or cash-collection policy is added by this verification.

## Reproducible setup

All cases use the same information cutoff (16 July 2026), August 2026–July 2027 projection, 12-month horizon, 10,000 paired paths and seed 2026. Shared settings retain the existing assumed-constrained pricing policy: one-month response lag, two-month response half-life, 55% pass-through, 0.55 demand elasticity and maximum 3% monthly yield step. The fuel shock starts in month one, remains at +25% for six months, then decays with a two-month half-life; it does not abruptly disappear after six months.

The purchase plan is `data/purchase_plan_example.json`; the illustrative retention plan is `data/retention_plan_example.json`. Delay cases select 30 mapped narrowbody purchases and postpone their fleet entry, delivery settlement and purchase borrowing by nine months. The chronological selection is 20 September MAX purchases and ten of the 20 December MAX purchases. The remaining ten December purchases stay on time. Prior deposits stay paid. The September delayed batch settles in June 2027; the delayed ten December aircraft settle in September 2027, beyond this horizon. Unallocated fleet entries do not move.

`cash_plan` is null in every case. The purchase plan itself enables the existing cash ledger and assumed purchase financing. The unrelated illustrative $800M revolver draw and advance-sales adjustment are deliberately excluded so they cannot confound the comparison. Opening spendable cash is the reported 30 June 2026 balance of $10,166M; short-term investments are separate.

The full request uses the existing `MonthlyScenario` fields and is accepted by `POST /api/monthly/simulate`. The exact retained combined-case request is saved locally in ignored `tmp/demo_case_verify/request.json`. No API schema change is required. The documented CLI supports purchase/retention delay runs and replay, but has no explicit fuel or fixed-cost flags; these comparisons use the existing Python scenario interface.

## Actual results

All currency values below are USD millions. FCF impact is the median of pathwise paired scenario-minus-no-shock differences. Capacity is the change in total operated ASMs over the full horizon, not passenger demand or revenue. Minimum cash is the median of each path's lowest projected month-end cash; it excludes the historical opening balance and intramonth movements.

| Case | Fixed nonfuel share | Retention | FCF impact P50 | Capacity change | Ending cash P50 | Pathwise minimum cash P50 |
|---|---:|---|---:|---:|---:|---:|
| Fuel only | 70% | Yes | -1,961.4 | +0.0000% | 13,467.7 | 9,984.1 |
| Delay only | 70% | Yes | 355.7 | +0.0000% | 15,541.9 | 10,501.8 |
| Combined | 70% | Yes | -1,608.5 | +0.0000% | 13,568.0 | 10,084.4 |
| Fuel only | 95% | Yes | -1,961.4 | +0.0000% | 13,746.8 | 9,999.3 |
| Delay only | 95% | Yes | 355.7 | +0.0000% | 15,821.0 | 10,503.8 |
| Combined | 95% | Yes | -1,608.5 | +0.0000% | 13,847.1 | 10,097.3 |
| Delay only | 70% | No | 466.0 | -0.3567% | 15,653.8 | 10,502.9 |
| Combined | 70% | No | -1,488.8 | -0.3567% | 13,690.5 | 10,090.1 |
| Delay only | 95% | No | 432.8 | -0.3567% | 15,899.7 | 10,504.3 |
| Combined | 95% | No | -1,522.1 | -0.3567% | 13,936.4 | 10,103.5 |

The retained combined case has FCF impact P10/P50/P90 of -$2,260.0M / -$1,608.5M / -$1,160.0M. Its simulated fraction of paths below the $5,000M month-end cash floor is 1.31%; this is conditional on assumed worlds and policies, not a calibrated probability.

## What the case actually demonstrates

- **Capacity is preserved in the retained preset.** Assumed retention of up to 18 aircraft plus existing spare capacity absorbs the delayed replacement gap. Do not claim this preset reduces operated ASMs or passenger revenue. Disabling retention reduces horizon ASMs by 1,285.323 million, or 0.3567%, under the same 2% spare assumption. Even then, capacity loss does not imply equal passenger-revenue loss because the simulated demand and capacity buffers matter.
- **Payment deferral is not permanent cost removal.** The nine-month delays reduce cash capex inside this horizon by $340M, purchase borrowing by $255M and cash interest by $31.875M. Those ten pending aircraft and their settlement remain in the queue beyond July 2027. The 20-aircraft September batch is paid inside the horizon, just later.
- **Retention has a cost.** The retained cases include 146 aircraft-months of extensions, $4.38M additional maintenance/carrying expense, 90% assumed productivity and 15% extra fuel burn for retained aircraft. The model does not establish actual maintenance or crew approval. Retention preserves capacity but is not financially optimal in these particular assumed worlds; lower operated capacity can save more variable expense than it loses in served revenue.
- **Fuel pressure dominates the timing benefit.** Fuel alone has median FCF impact -$1,961.4M; delay alone with retention has +$355.7M; their combined median is -$1,608.5M. Do not add medians from separate distributions to infer the combined result.
- **FCF and cash differ when financing is present.** Combined scenario FCF P50 is +$2,644.6451M, while its impact against the paired control is -$1,608.4809M. Absolute ending cash P50 is $10,166M opening cash + $2,644.6451M FCF + $757.35M fixed net financing = $13,567.9951M. Relative net financing changes by -$252.45M (less borrowing partly offset by less financing fees); therefore paired ending-cash impact is -$1,860.9309M, not the FCF impact. Borrowing is outside FCF and earnings.
- **This preset does not exhibit a cost-assumption sign flip.** Moving fixed nonfuel share from 70% to 95% leaves paired FCF impact unchanged when retention preserves capacity. It changes absolute control and scenario cash flows because the planned fleet still grows. Without retention, combined FCF impact changes from -$1,488.8M to -$1,522.1M; both remain negative. Larger outages documented elsewhere can reverse signs, but that claim must not be attached to these presets.

## Verification and presentation boundaries

Ten 10,000-path cases completed using preserved model source. Raw internal path arrays independently reproduced the packaged cumulative FCF-impact and ending-cash quantiles; they also produced the pathwise minimum-cash statistics above. The retained combined request was accepted by the existing FastAPI endpoint and returned the same run identity and FCF quantiles.

The displayed retained combined run was independently replayed using registered archived source. A one-page thesis PDF and companion JSON were exported, verified by their file/record checksums and independently replayed; the renderer produced identical PDF bytes. Short analyst notes were used to stay within the existing one-page limit.

The public simulator currently returns monthly cash quantiles, ending-cash quantiles and a pathwise cash-floor breach fraction. It does **not** return a quantile distribution of pathwise minimum cash. The minimum of monthly median cash is a different statistic: $10,235.3M for the retained combined case, versus the actual median of pathwise minima $10,084.4M. Prefer the existing ending-cash KPI, or explicitly label a derived chart summary “Lowest monthly median cash.” Do not label it “Median minimum cash.”

The public price-response coefficients, demand paths, fixed-cost split, aircraft productivity, spares, purchase prices, residual capex, loans and retention rules remain assumptions. Quarterly operating anchors are converted into monthly run rates. Consolidated PPE/leases, gross delivery reconciliation, detailed collections, crew/hub constraints, rival behavior, fitted policies, parameter uncertainty and calibrated financial validation remain unfinished. Ten thousand paths are a demonstration default; this exercise does not prove all-tail quantile convergence.

Preserved outputs and the reproducibility script are local ignored artifacts under `tmp/demo_case_verify/`; they are not committed numerical proof bundled with the repository. Reproduce with `.venv/bin/python tmp/demo_case_verify/verify.py` and `.venv/bin/python tmp/demo_case_verify/verify_no_retention.py` in this checkout while those local artifacts are present.

Retained combined run: `bbcdd8fdc7fa33241f13e4dec9896d462ecd52439a3985edb6a799f550618431`.

Verified one-page thesis artifact: `b13d67d55d002d2c16fa14c93d975666a802db98cdc1704cbf4a405b4cca9f3c`.
