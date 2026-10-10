# Incremental cash timing and funding ledger

Monthly core `monthly-fleet-0.4.0` adds an optional transaction ledger, shared by
What if, What breaks and What must be true. It extends the existing aggregate
operating cash reference; it is not the proposal's complete or fitted financial
model. Leave the cash plan disabled to retain the previous numerical behavior.

## Source definitions

The UAL June 30, 2026 filing, published July 16, supplies reported opening stocks:
cash 10,166; restricted cash 142; short-term investments 6,471; contractual debt
principal 23,081; undrawn revolver 3,000; advance ticket sales 10,752; frequent-flyer
deferred revenue 7,971 (3,939 current + 4,032 noncurrent); receivables 2,473;
fuel/parts/supplies 1,795; prepaids/other 759; accounts payable 5,772. All amounts
are USD millions. The principal measure excludes 157 of unamortized discount,
premiums and issuance costs; it is not the 26,464 balance-sheet sum including
finance leases and other financial liabilities.

Official filing:
https://www.sec.gov/Archives/edgar/data/100517/000010051726000139/ual-20260630.htm

The new records in `data/monthly_anchors.json` are reviewed normalized numeric
transcriptions of the official filing rendered through the web tool. Standard
cash/restricted, prepaids and payable quantities were independently checked using
the preserved original SEC Company Facts source
`6621f43f61efe96e02f2aada43fbed7ea989332e4fad2c6c453f9abe139e6d32`.
Direct original-HTML downloading returned HTTP 403; no original HTML preservation
is claimed. Custom debt, advance-sales and loyalty quantities are not mislabeled
as standard Company Facts tags. Same-date reported stocks, verified review status,
publication cutoff, units and unambiguous values are required; blanks are not zeros.

Historical cash statements use cash **including restricted cash**: H1 operating
6,409 + investing -3,354 + financing 1,172 = total-cash increase 4,227;
6,081 + 4,227 = 10,308. Unrestricted cash increases 4,224; restricted cash rises
from 139 to 142. The projection separately retains spendable cash. No H1 cash flow
is presented as an observed monthly series or an automatic forward funding schedule.

## Event contract

`MonthlyScenario.cash_plan` / `MonthlyPayload.cash_plan` is either null or an object
with a required analyst `basis` (1–600 characters) and at most 96 dated `events`.
Each event has a simulated `YYYY-MM` month, `kind`, finite `amount_musd`, and `scope`
of `both` or `intervention`. Out-of-horizon events fail instead of disappearing.
Both-scoped events apply to control and shocked paths. Intervention-scoped events
apply only to the shocked scenario, in both frozen and assumed pricing policies.

Kinds: debt borrow/repay, revolver draw/repay, additional cash interest, financing
fees, additional cash capex/refund, asset-sale cash proceeds, restricted transfers,
investment trades, and cash-related changes in advance sales, loyalty deferred
revenue, receivables, inventory, prepaids or payables. Most transactions require
nonnegative amounts. Balance changes, investment trades and restricted transfers
are signed: a positive investment trade buys investments; a positive restricted
transfer moves cash into the restricted pool.

OCF = provisional OCF + cash-related liability changes - cash-related operating
asset changes - incremental cash interest. FCF = OCF - (reference net cash capex +
additional capex - capex refunds). Other investing cash = asset-sale proceeds -
investment purchases. Financing cash = debt/revolver proceeds - principal
repayments - financing fees. Spendable cash changes by FCF + other investing +
financing - transfer to restricted cash.

Debt principal and revolver balances move with the entered principal transactions.
Revolver gross draws cannot exceed opening-month available credit; same-month
repayments do not expand that month's gross draw allowance. Revolver repayment
cannot exceed drawn principal; ordinary repayment cannot consume revolver principal.
All tracked stock balances except cash must remain nonnegative. Each month settles
atomically. Negative cash is retained as an exposed funding shortfall, with no
automatic borrowing. There is no intra-month liquidity ordering forecast.

Historical OCF already includes interest, working capital and loyalty cash effects.
Events are **incremental departures** from the retained reference, not a second
addition of historical flows. Unchanged balances are held fixed as an assumption;
the embedded historical OCF residual is not decomposed into an observed monthly
stock-and-flow forecast. Reported miles earned/redemptions are not gross cash
receipts. No immediate tax benefit is assumed for incremental cash interest.

## Limits and next integration

The fixed reported revolver limit is a scenario assumption, not future covenant
compliance or a credit approval forecast. General debt borrowing requires an
explicit analyst event and does not represent available funding evidence. The
reported principal maturity buckets are not evenly divided into invented monthly
payments. Timing, coupon/fees, taxes and funding availability need their own evidence.

Reference capex is net of equipment deposit returns; it cannot silently be turned
into gross PPE additions. Additional capex/refunds and asset-sale proceeds here
are explicit cash amounts only. No PPE/deposit/lease roll-forward, disposal gain,
gross delivery funding, purchase price or automatic link from a net fleet-entry
delay is claimed. Those remain required M02/M05 work. Events do not fit demand,
collections, loyalty behavior or financing policy.

## Reproduce and inspect

The browser has optional event rows under What if, plus a saved cash/funding table.
The table labels monthly medians and saved run identity. Scenario changes disable
thesis export; failure/target witness inspection restores the saved cash plan.
Run JSON includes opening stocks/source IDs, event basis/timing/scope, all monthly
stocks/flows, quarterly flows/ending stocks and true pathwise cumulative-FCF
quantiles. Thesis version 0.2.0 separates FCF from financed cash and includes the
fixed event totals in its one-page PDF.

```sh
.venv/bin/python scripts/run_monthly.py --cash-plan data/cash_plan_example.json
.venv/bin/python scripts/run_monthly.py --replay data/runs/7c6ce50f49eac423f92fea986439ec683f48ac249c6662f437754eb4602db7fc.json
```

The clearly illustrative 10,000-path browser and CLI run above uses a shock-only
August 800 revolver draw, 5 financing fee and -100 advance-sales balance change.
Cash is 695 higher than the same fuel scenario without those events; FCF is 100
lower, with unchanged operating income and pricing-response contribution. Net
financing is +795; the separate collection change affects OCF once. Cumulative
scenario FCF P50 is -496.7411 and ending cash P50 10,464.2589; the 795 financing
difference explains why ending minus opening cash is not FCF.

Browser downloaded JSON replays independently in the isolated archived core.
New archives include `cash_bridge.py`; the fixed legacy layout remains allowed
only through existing local registration and file/environment checks. Prior monthly,
failure-search and target-inversion records still replay using their original core.
No arbitrary run-supplied module list or code is executed.

Nineteen new tests cover source/date gates, all-path accounting, scopes/timing,
credit and stock limits, atomic failures, negative cash, baseline equivalence,
solver integration, financed thesis/PDF semantics, API rejection and archived
replay. Full suite: 197 passed; the existing Starlette/httpx deprecation warning
remains. Passing these tests is software/accounting evidence, not calibration or
historical investment validation.

The filing’s MD&A page 26 independently confirms the total $3,000M facility and
no outstanding borrowings at June 30, supporting the zero opening revolver draw.
`scripts/audit_cash_example.py` separately replays the old and new registered cores
and checks 2,460 original monthly quantile/event differences without importing
the cash-ledger calculations. Its report is
`data/cash_bridge_checks/92278ab426c80fe0c70177ce319e4847e6e1b44f8e37bd620df8202c5f40ae74.json`.
The 3×3 browser cash-floor search completed on 10,000 paths per point; downloaded
search replay and witness restoration preserve the same cash plan.

The final funded thesis export is
`f34993e9c07af2f7dcf8be7489f3319c3b67bc12d94ce5c15f0c61e6d11a6c4e`.
Browser and CLI export IDs match, both actual downloads match the immutable
files, and run/derivation/PDF replay passes. The one-page PDF was rendered and
visually inspected; the review copy is `output/pdf/illustrative-funded-thesis.pdf`
and companion JSON is alongside it. The final browser screenshot is
`screenshots/monthly-cash-funding.png`. Earlier legitimate run/export versions
remain in their immutable archives.

The purchase bridge added in core 0.5.0 can replace the Q2 capex reference with
explicit residual capex plus mapped deposits/settlements. Cash-event capex changes
then adjust that active purchase schedule. Purchase borrowing, fees and interest
settle through this ledger; all other cash events retain their scope. Generic
principal repayment is rejected with a purchase plan until loan allocation and
amortization are implemented. The full suite now passes 227 tests. See
`purchase_bridge.md`; the 0.4.0 event example above remains an archived milestone.
