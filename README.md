# UAL Counterfactual Engine · V1

The main application is a monthly analyst workstation for testing how aircraft
delivery delays and fuel changes affect United's operating capacity and cash flow.
It compares explicit scenarios, searches tested failure boundaries, finds
assumptions compatible with operating targets, and exports a one-page thesis.
Pricing, cost flexibility, financing and retention decisions are assumptions;
simulated outcome ranges are not calibrated forecasts or investment signals.

Open the server root (`/`) or `/monthly` for the main application. The original
daily fuel sensitivity pilot remains available at `/fuel-pilot`.
See [submission brief](docs/submission_brief.md),
[demo walkthrough](docs/demo_walkthrough.md), and
[case verification](docs/demo_case_verification.md).

The current fare-source follow-up adds a separate original airport/city/WAC
scanner. Its first real quarter passes an independent audit over 3,046,411 tickets
and 11,444,839 endpoint occurrences; every one of that quarter's 177,699 fare cells
has exact geographic endpoint evidence. Both full 44-quarter builders have now
published outputs over 166,499,254 ticket records. Full-history audits and joint
fare/demand estimation remain open; import completion is not model validation.
The latest pre-redesign full suite passed 385 tests.
See [original fare geography and scope](docs/fare_geography.md).

## Monthly proposal implementation (in progress)

The new `/monthly` screen runs four-market monthly operating sensitivity with
10,000 paired paths, a frozen/assumed pricing comparison, quarterly outputs and
a dated evidence ledger. It is an initial foundation for the Phase 1 proposal;
two mainline fleet groups, a regional contract block, aircraft outages, net fleet-entry
delays and schedule capacity limits are now implemented. Explicit purchase allocations now link deposits, delivery settlement and assumed
financing to the fleet queue, including exact aircraft counts and partial batches.
Fitted policies, full gross purchase reconciliation and fitted collections remain pending. The optional
incremental funding/working-capital ledger and one-page thesis export are implemented.
See `docs/proposal_delivery.md` and `docs/monthly_model_card.md` for exact coverage.

After installing dependencies and starting the server, open **http://127.0.0.1:8000/monthly**.
The default historical information cutoff is July 16, 2026, when both baseline
documents were published. It is a reconstruction, not a historical as-of backtest.

```bash
python scripts/build_monthly_data.py   # immutable reviewed evidence -> Parquet
python scripts/build_history.py        # SEC UAL/DAL/AAL quarterly financial versions
python scripts/build_operating_history.py --downloads data/bts_downloads.json --wac-sha b1efb1ef88ca3ad26f0bcabc3d215f48f1babe88c6cb76a64f99906b53143393
python scripts/build_form41_history.py --downloads data/form41_downloads.json
python scripts/build_reporting_panel.py # audited entity-quarter join; scope gates retained
python scripts/audit_reporting_panel.py # independent original ZIP traffic sums
python scripts/fit_utilization.py       # frozen 2013–19 fit, 2020–23 selection; 2024+ locked
python scripts/audit_utilization.py     # independent P52 target/score arithmetic
python scripts/build_t2_history.py      # aircraft capacity targets; two-way exposure reconciliation
python scripts/audit_t2_history.py      # original T2/P52 Decimal arithmetic and source-line checks
python scripts/fit_capacity.py         # direct vs utilization × productivity; frozen 2013–19 fit
python scripts/audit_capacity.py       # independent raw targets, fits, paths, baselines and scores
python scripts/run_monthly.py          # save input snapshot, configuration and results
python scripts/run_monthly.py --replay data/runs/<run_id>.json
python scripts/archive_model_code.py --verify-runs # preserve executable versions; audit saved runs
python -m pytest -q
```

The cash-flow view uses a provisional aggregate cash bridge. It is not yet the
proposal's detailed cash accounting model. Every assumption is displayed and
the complete run can be downloaded from the monthly screen.

The **What breaks?** section searches two operating-driver ranges against absolute
FCF or minimum-cash thresholds, using common worlds across every tested point and
both pricing modes. It shows exact tested-point failure regions, links points to
full scenarios, and exports replayable searches. Counts are not event probabilities.
See `docs/model_code_archive.md` for executable source preservation and archive
transfer. API/CLI exports reference verified local code bundles; replay checks
all deterministic output without importing the current model. Older versions
whose source was not retained remain unavailable.

See `docs/failure_search.md`; `python scripts/run_failure_search.py` runs the default
25-point, 10,000-path search locally.

The **What must be true?** section inverts explicit revenue, margin or FCF targets
on two tested driver ranges, with optional minimum cash and disclosed FCFF valuation
conditions. It returns multiple compatible combinations and actual matching paths.
Every valuation assumption must be supplied; operating-asset value is kept separate
from equity price and the simulator's provisional cash state. See
`docs/target_inversion.md`; `python scripts/run_target_inversion.py` saves the default
search, and `--replay <downloaded-record.json>` verifies browser exports offline.

The SEC pipeline covers 54 quarterly rows each for UAL, DAL and AAL from 2013-Q1
through 2026-Q2. It preserves raw snapshots, filing dates, unit conversions and
YTD-difference source legs, and reports variable gaps rather than filling them.
See `docs/financial_history.md` for offline rebuilds, definitions and vintage limits.
The simulator's Q2 cash reference now derives H1 minus Q1, replacing the mismatched
H1 average. The BTS importer preserves official DD/FD segment archives and builds
monthly ASM, RPM, passengers, departures and weighted load factors. The verified
October 9 snapshot has 163 months each for UA, DL and AA (January 2013–July 2026),
with feeder and foreign-to-foreign coverage still excluded. See
`docs/operating_history.md` for scope, rebuilds and release-date limits. The verified
Form 41 P12/P52 imports retain 54 quarters each for UA/DL/AA, while P12A retains
162 observed fuel months (2013-01–2026-06). Their current-download acquisition
date is not a historical publication vintage. Source grains, units, blanks, signed
amounts and quality flags survive; see `docs/form41_history.md` for offline rebuilds.
An audited reporting panel now joins traffic/P12/P52 by exact carrier/entity/quarter,
keeps financial-versus-geographic crosswalks visible and retains P12A certificate
fuel separately. It matches 216 entity-quarters each for UA/DL/AA (54 quarters),
with economic/service-scope gates still withheld before cross-schedule fitting.
See `docs/reporting_panel.md` and `docs/proposal_status.md` for verified coverage
and the current proposal gap assessment.
The first utilization component is fitted within P52's own reporting scope.
Its eligible 2020–2023 selection error is 1.12 airborne hours/aircraft-day versus
2.07 for seasonal persistence and 1.52 for a fixed seasonal mean. Source gaps
exclude important disruption quarters; some comparisons have only four targets.
The app displays these limits beside the cached scorecard. See `docs/utilization.md`;
locked 2024+ testing, calibration and simulator integration remain pending.
The T2 covered-aircraft capacity component now compares direct forecasts with
utilization × productivity on 191 common 2020–23 forecasts. The selected model's
equal-weight MAE is 129.6 thousand ASM/assigned day versus 136.4 for the best direct
model and 187.4/280.2 for the two baselines. United narrowbody has only one usable
12-month comparison, so this does not establish reliable United capacity forecasts.
The app displays this warning and all carrier/group sample counts. Original CSV
targets, 12 regressions, recursive paths and every score are independently audited;
see `docs/quarterly_capacity.md`. Whole-fleet/monthly integration and the locked
2024+ evaluation remain pending. The full software/source suite passes 376 tests.
Fares, full carrier-boundary reconciliation, historical fitting and forecast validation
remain pending; imported history does not yet replace the simulator reference.

## One-page analyst handoff

The monthly screen now exports a one-page thesis PDF plus the complete thesis/run
JSON. Enter a hypothesis, choose the paired no-shock or frozen reference, and name
the next observable checkpoint and evidence that would overturn the view. Export
independently replays the saved run before deriving its distributions and constraints;
changed scenario inputs block export until the displayed run matches. The record
keeps analyst notes separate from source/model evidence and discloses the current
sensitivity/validation limits. See `docs/thesis_export.md` for commands, sample
files, preserved outputs and replay limits. The complete suite passes 376 tests.

The monthly screen can now check sampling stability with independent seed repeats
and increasing counts. `scripts/check_convergence.py --run data/runs/RUN_ID.json`
saves a fully replayable study; `scripts/audit_convergence.py STUDY.json` checks its
statistics independently. Exact incremental batching supports studies up to one
million paths per repeat. The illustrative retained-aircraft case passes all 200
checks at 500,000 and 750,000 paths with unchanged tolerances; the original
10,000-path run still fails 45 checks. Completely replayed studies can be reused
from a checked local cache. Monte Carlo stability remains separate from calibration
and model uncertainty. For this exact verified case, 20 warm local API requests
measured p50 0.969 s / p95 1.021 s; broader performance and analyst testing remain
pending.
See `docs/convergence.md` for definitions, exact runs and limits.

The monthly research view now includes release-aware expanding state-space traffic
fits for UA/DL/AA across four markets, alongside three baselines. All six models use
1,800 identical selection-period comparisons. The best state candidate loses to the
fixed initial seasonal mean; peer pooling adds no skill. This is a reconstruction
with 6–20-month source-set information gaps, not deployed demand/pricing behavior.
The independent original-source/statistical audit passes 86,733 checks. See
[monthly traffic methods, results and limits](docs/monthly_traffic.md).

```bash
python scripts/fit_monthly_traffic.py
python scripts/audit_monthly_traffic.py
```

Rolling traffic releases now have their own issue-date reconstruction pipeline,
with 132 preserved archives through 2023 and exact source-version histories for
2020–2023. See [traffic vintage methods and limits](docs/traffic_vintages.md).
The existing traffic scorecard still uses its original annual-only inputs; improved
source availability does not itself establish improved forecasts.

The rolling-release audit passes 312,457 checks and reduces reconstructed
information gaps to 3–6 months. It also exposes an unresolved AA January 2021
revision discrepancy: final certificate Q1 traffic exceeds the issuer's broader
consolidated RPM total. The existing scorecard now shows this source-review warning.
Trace and reconcile the underlying records before accepted estimation; passing
extraction checks cannot establish economic validity. Details and the preserved
review are in [traffic vintage methods and limits](docs/traffic_vintages.md).
The subsequent [original-record trace](docs/traffic_revision_trace.md) confirms
prior-year payload reuse across all 1,975 later DD and temporarily all 659 FD AA
January 2021 records. Its independent audit passes 46,525 checks; earlier domestic
quarterly quantities match T2 exactly across ten aircraft types. That original trace preceded the separate reviewed correction and four-case
source comparison described below. Remaining reporting boundaries still limit
economic acceptance and simulator integration.

```bash
python scripts/build_traffic_vintages.py --workers 4
python scripts/audit_traffic_vintages.py
```

## Original daily fuel pilot

A runnable public-data pilot for the Longaeva AI Hackathon. It models the **operating-income sensitivity of United Airlines to a daily jet-fuel shock when assumed pricing responses have a lag and a speed constraint**.

It is not an earnings forecast, verified management-behavior model, stock-price forecast, or live trading signal.

## Quick start (macOS, Linux, Windows with Python 3.10+)

**Mac shortcut:** On macOS, double-click `start_mac.command` after unzipping. It creates a virtual environment, installs dependencies on first run, and opens the dashboard. You may need to right-click > Open for the first launch because the file is downloaded. This runs Python code locally; inspect the source if you prefer.


```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
./run.sh                         # Or: python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000/fuel-pilot** for this original pilot. The server runs on your own machine and does not send uploaded quotes to a cloud service. Only run on loopback without authentication.

Run tests:

```bash
python -m pytest -q
```

## What works

1. Interactive dashboard with 190 genuine daily US Energy Information Administration Gulf Coast jet-fuel observations, January 5 through October 6, 2026.
2. Actual UAL Q2 2026 operating-unit inputs: 87.279 billion ASMs, 1.219 billion fuel gallons, $4.19 average purchased fuel price, 18.45¢ PRASM, 20.25¢ TRASM and 18.99¢ CASM.
3. Paired stochastic fuel paths using the same random shocks for baseline and scenario; the difference isolates the *modeled* fuel impulse, not changes in the random environment.
4. Editable fuel shock size/persistence, management response lag, rate of repricing, cost pass-through, and horizon.
5. Forecast charts and P10/median/P90 **conditional simulation** estimates of cumulative operating-income sensitivity.
6. Strict source-backed quote CSV import and descriptive comparison to contemporaneous EIA spot observations. No real fare CSV is bundled or fabricated.
7. Data manifest, limitations, source links, a model specification, and tests.

## Ticket quotes: schema and comparability

Download `data/quotes_template.csv` or use the dashboard template link. Required columns:

```
observed_on,departure_on,carrier,origin,destination,cabin,stops,total_price_usd,source_url
```

Rules: ISO dates, UA-marketed quotes, IATA airport codes, fare in USD including taxes, legitimate public URL, no duplicate snapshots. The route diagnostic currently uses **nonstop economy quotes captured 21-35 days before travel** and requires **eight distinct observation days spanning at least ten days**, with contemporaneous public fuel prints. Prices are descriptive observed offers. They are **not transacted yields** or mechanically equivalent to quarterly PRASM. To compare changing quotes properly, collect the same routes, fixed departure weekday/lead-time buckets, cabin, baggage policy and currency across repeated snapshots; the current 21-35-day filter is a minimal first pass rather than rigorous mix adjustment.

**Do not paste confidential or employer-proprietary data.** The certification requires publicly available sources and source documentation.

## Data sources and snapshots

| Input | Coverage | Source | Status |
|---|---|---|---|
| EIA Gulf Coast kerosene-type jet fuel FOB | 190 public daily prints, Jan 5 to Oct 6, 2026 | https://www.eia.gov/dnav/pet/hist/EER_EPJK_PF4_RGC_DPGD.htm | Observed, static October 7 release |
| United Q2 2026 operating stats | Three months to Jun 30, published Jul 15 | https://united.mediaroom.com/2026-07-15-United-Posts-Q2-Results-Above-Wall-Street-Expectations-and-Raises-Full-Year-2026-Adjusted-EPS-Guidance1-Despite-a-Nearly-6-Billion-Increase-In-Anticipated-Fuel-Costs | Reported |
| Airline ticket quotes | None bundled | User-supplied sourced CSV | Required for descriptive fare diagnostics |
| DOT O&D 40 DB1C monthly fares | Monthly since Jul 2025 | https://www.bts.gov/topics/airlines-and-airports/origin-and-destination-survey-data | Identified, not ingested |

The EIA official daily table was transcribed by week into `data/eia_2026_weekly_source.txt`, and `scripts/build_data.py` generates its CSV, preserving missing observations. The model does not invent weekend spot prices. The initial history is not a **publication-vintage dataset**, so it must not be used as a point-in-time historical backtest without further work.

Optional public refresh **on a network-connected machine**:

```bash
python scripts/refresh_fuel.py
```

Uses the FRED distribution of the EIA `DJFUELUSGULF` series. Records the retrieval timestamp and preserves a snapshot backup. This script has not been network-tested in the restricted build environment; if download fails, it retains existing data.

## Read before interpreting outputs

- **Inputs**: current spot is EIA Gulf Coast wholesale FOB; UAL's average delivered fuel cost includes regional exposure, taxes and timing. Treat one-to-one spot-to-delivered cost as an *assumption*.
- **Reported anchor**: Q2 2026 annualization/quarterly run-rate is not adjusted for route, seasonality, forward schedule, hedging, future gallons or the mix of ticket offers.
- **Response**: assumed behavior only. Nobody has calibrated management or competitor policy functions yet. User-set delay/speed/pass-through do not claim to be United policy.
- **Output**: Δ operating income for a counterfactual *relative to a paired no-shock baseline* in USD millions. It is not operating-income absolute level, stock alpha, EPS, free cash flow or a return forecast.
- **Uncertainty**: conditional stochastic spot-price paths only. P10/P90 exclude parameter estimation error, data errors, and model discrepancy. Not validated prediction intervals.
- **Daily quote gap**: no monetizable signal until actual comparable daily fares and subsequent firm/market outcomes have been collected and independently validated.

Read `docs/model_spec.md` and `docs/validation_plan.md` for assumptions and what must be proven next.

**Fuel-only sanity check:** `python scripts/evaluate_fuel.py` compares a mean-reverting 1- and 5-print EIA fuel prediction to a naive last-price benchmark on rolling late-2026 windows. At one print, it produces $0.1078/gal MAE against $0.1088/gal for persistence, an approximately 1% change. At five prints, it produces $0.2339/gal against $0.2335/gal for persistence, slightly worse. This is not evidence that the simulator predicts fuel or stocks better than a trivial baseline, and it does not test the airline management-response hypothesis. No price/fare alpha performance is claimed.

## Code layout

```
app/data.py           EIA parser, data/firm input manifest, volatility estimate
app/engine.py         pure paired scenario engine
app/quotes.py         provenance-first quote ingestion and diagnostics
app/server.py         local FastAPI service
app/static/           responsive dashboard
scripts/build_data.py reconstruct EIA snapshot CSV from official weekly text
scripts/refresh_fuel.py optional remote public fuel refresh
tests/               behavioral and data-quality checks
docs/                methods and validation roadmap
```

## Hackathon use and next step

The Phase 1 proposal is the implementation authority. Next, reconcile BTS traffic, Form 41 costs/fuel and SEC financial reporting across mainline and regional boundaries, add O&D fare history, then fit demand, pricing and utilization using the stated 2013–2019 / 2020–2023 / 2024+ partitions. Complete the full cash bridge, rival feedback and historical scorecard, and extend the inverse workflows to the fitted model. Daily offered-fare snapshots are optional evidence for the original pilot; they are not a prerequisite for the proposal’s monthly model. Avoid displaying forecast or investment performance claims before held-out evaluation.

Optional cash timing and funding events now reconcile financing, collection-timing
assumptions and spendable cash separately from OCF-minus-capex FCF. The browser
form, both inverse workflows, thesis export and archived replay share this ledger.
See `docs/cash_bridge.md` for the source definitions, illustrative plan and limits.

## Aircraft purchase cash timing

The monthly screen supports explicit priced purchases mapped to existing fleet
entries, prior/future deposits, settlement-linked borrowing and cash interest.
The illustrative 30-aircraft, nine-month delay can be loaded and edited in the UI.
Purchase capex replaces the historical aggregate capex reference with an explicit
residual, preventing double counting. Unallocated aircraft and beyond-horizon
settlements stay visible. See `docs/purchase_bridge.md` for the full assumption
contract, independent accounting audit and unresolved proposal scope.

Optional retirement-extension links now model explicit retention limits, lower
productivity, additional upkeep/carrying and operated-share extra fuel while mapped
replacement purchases are delayed. Requests and unapplied counts are inspectable;
both searches and archived runs retain the full assumptions. See `docs/retention.md`.
Maintenance/crew eligibility and retirement behavior remain assumed, not fitted.


## Reviewed traffic source comparison

A declared whole-cohort AA January 2021 correction now has 244,351 independent
original-source checks. A separate four-case study compares rolling native/reviewed
histories and targets with annual-only availability, with 735,982 independent
numerical/input checks over 126 fits. Every model/case scores the same 1,763 of
1,800 requested forecasts; 37 unsupported AR predictions remain visible in each
rolling case. The best reviewed state model loses to regularized AR (MASE 6.833
versus 6.069); native conditional 80% coverage is 77.47%. This hindsight source
reconstruction is not calibrated demand/pricing or financial-model integration.
The dashboard provides verified artifact/audit downloads alongside the original
annual-only scorecard. See [correction, protocol amendment and full results](docs/reviewed_traffic.md).


## Fare acquisition and published import

All 44 public DB1B quarters for 2013–2023 are preserved (1.25 GB compressed),
with a separate quarterly US-only fare importer retaining ticketed/operating/
reporting roles, directional markets, overlapping brand/mainline cohorts and
quality/source-line provenance. The first real quarter passes 1,954,698 independent
original-record checks; eleven focused tests cover the parser, independent oracle,
Parquet/offline rebuild and failure gates. The full 44-quarter import has published
its output; full-history/Parquet source audit, scope matching and joint pricing/demand
estimation remain required. This does not fill Canada/international or monthly
fare coverage, identify realized yield, establish causal elasticity or close M03.
See [fare source methods and current import](docs/fare_history.md).

The complete software suite passed 385 tests before the focused-interface changes.
This is software evidence; full-history source audits remain pending.
