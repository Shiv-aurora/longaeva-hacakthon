# Quarterly DB1B fare source milestone

This work addresses the missing fare input in proposal D02 and M03. It is a
source-import milestone, not a fitted pricing policy or validated demand model.
The full historical import is running; estimation and simulator integration remain
withheld. The latest completed quarter is authoritative in the process log, not
this document's static description.

## Preserved sources and availability

The official [BTS product catalog](https://www.bts.gov/topics/airlines-and-airports/origin-and-destination-survey-data-product)
was inspected through the browser. All 44 quarterly ZIP files for 2013-Q1 through
2023-Q4 were downloaded from observed links and preserved by SHA256, with their
original URLs, compressed byte counts and acquisition times. Total compressed
size is 1,253,455,748 bytes. No locked 2024+ archive was downloaded. DB1C's later
monthly 40% sample is not concatenated into the older quarterly 10% sample.

The immutable source inventory is
`data/fare_sources/ab5a09c49352533af3471b5dca1246826292ca1c918618e3149d948ea8e4b292.json`.
`data/fare_acquisition_spec.json` declares the exact observed URLs/quarters before
full ingestion. The official
[field definition](https://www.bts.gov/sites/bts.dot.gov/files/DB1B_Record_Description_Product%20for%20bookstore.pdf)
is preserved under SHA256
`9556ee15a025dbaf2d24e3be69164e5eb4c68e18649421cf72de18eeaa889882`.
The generic catalog says comma-delimited; the product definition and actual legacy
records are pipe-delimited. The parser follows the original record layout: ten
ticket fields and eleven fields per coupon, with up to 23 coupons.

Publication dates are filename proxies for the selected releases, not recovered
first-release dates or independently captured historical bytes. The selected
2013–2015 releases were published again in 2017. A model cannot treat those selected
versions as known in 2013–2015 merely because their observations occurred then.
Retrieval timestamps are retained separately from observation and release dates.
The exercise is a current-download reconstruction.

## Measurement and carrier boundaries

The product covers entirely US itineraries. It does not provide complete fares
for the simulator's US-plus-Canada or international market definitions. US territory
WACs 3 and 4 map to Latin America, and WAC 5 maps to Pacific, in the existing traffic crosswalk; therefore the
fare import remains an unassigned US survey rather than automatically becoming a
"domestic" price target. Original first-quarter WAC 5 records include Pacific
territories. The separate [raw endpoint scanner](fare_geography.md) now verifies
exact quarter/airport/city/WAC coverage for the first quarter; full-history
geography and travel-object matching remain required. Raw itinerary fare is not offered booking price,
monthly airline yield, consolidated passenger revenue or a causal demand elasticity.
No fare observation is synthesized or repeated into three monthly observations.

Reporting, ticketed and operating carriers remain separate. Two overlapping
cohorts are kept for UA/DL/AA:

- `ticketed_online`: every coupon has the same target ticketed carrier; operating
  carriers can differ, including regional operators.
- `mainline_online`: the reporting carrier and every operating/ticketed coupon
  carrier are that same target carrier, a narrower subset.

These cohorts must not be summed. Mixed ticketed-carrier itineraries and other
brands are not assigned to the reporting carrier. AA's pre-merger scope does not
silently acquire US Airways observations; dated merger/carrier continuity requires
its own review. Ticketed-brand prices with regional coupons are not relabeled as
mainline flown traffic.

Markets end at a directional `X` trip break or at the final coupon. Connections
without that break stay within one directional market. Distance-prorated market
fare follows the [DB1B market definition](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FHK).
It is a survey allocation, not an observed allocation of revenue between operators.
Passenger-weighted fare sums and passenger-mile sums remain available; averages
are not unweighted averages of ticket rows. Sample passengers and market
contributions are not expanded into population counts. A round-trip passenger can
contribute to two directional markets; they are not two distinct ticket passengers.

Questionable dollar flags, zero fares, surface coupons, zero distances,
WACs >=100, unsupported trip breaks, unknown fare/coupon types and circular
markets remain explicit exclusions. The flag named `outside_US_WAC` describes
this literal threshold; the official lookup also lists US territories above 100,
so it must not be interpreted as an exact foreign-country definition. No model residual, ad hoc fare cutoff or
imputation picks retained records. Quality flags can overlap; their counts are not
additive populations. Airport labels, DOT city-market IDs, fare-basis mix and coupon
counts remain separate controls rather than disappearing into a carrier mean.

Every route/cohort cell retains its archive/member, source-line range, sampled
line witnesses, ordered complete contributor digest, record/sample counts and
original release proxy. The eventual native Parquet table is bound into the
snapshot manifest. The full import publishes its separate latest pointer only
when all 44 quarters and Parquet count/readback checks succeed.

## Verification completed so far

The real 2013-Q1 quarter independently passes 1,954,698 checks over 3,046,411 raw
ticket records and 177,699 route/cohort cells. The independent oracle does not import
the production parser, eligibility rules or market splitter. It reconstructs raw
partitions, quality/cohort counts, sample weights, Decimal fare allocation and every
contributing-line digest. Its immutable result is
`data/fare_history_checks/parts/ff5219d6c59ab3e9426a5d20fe3222edde1364193d0ad7e3b7fa74315074b806.json`.
Auditor/helper source bytes are preserved by their hashes. This is one quarter's
check, not the full-history or Parquet acceptance report.

The complete suite passes 376 tests (one existing Starlette/httpx deprecation
warning). Eleven focused fare tests pass: raw field/cardinality and locked-date gates, directional
connections and breaks, regional/mainline and mixed-brand boundaries, explicit
quality exclusions, passenger/fare conservation, raw-line provenance, full
44-quarter synthetic native Parquet/offline rebuild, ordinary cache corruption,
independent rejection of rehashed quantity corruption and atomic publication
failure on a locked final-quarter record. Synthetic fixtures are test data; they
are not added to the real history or used as economic observations.

```bash
.venv/bin/python scripts/ingest_db1b_fares.py
.venv/bin/python scripts/audit_db1b_fare_part.py \
  data/fare_history/designs/3d4b015c706ac4adcbfd790ca511fce5a60b3f9c7c78d8371d1f7004db60b9b1/design.json \
  2013-Q1
# Run only after the complete real history publishes successfully:
.venv/bin/python scripts/audit_db1b_fares.py
```

The running ingestion has executable session 85686 / PID 50768 at this milestone.
Its output is `/tmp/ual-fare-history-build.json` and progress/errors are
`/tmp/ual-fare-history-progress.jsonl`. Revalidate the live handle before deciding
that work has stopped; progress files alone do not establish liveness. Cached
quarters remain bound to their source and exact design. Do not edit the loaded
builder while it is running or restart solely because an observation times out.

## Next estimation gate

Finish and independently audit all real quarters, then assess complete carrier/
quarter and within-route support, revision lags and merger/mix breaks. DB1B O&D
markets and T-100 flown segments are different objects. Even matching airport codes
and quarter cannot erase connections, domestic legs of international itineraries,
regional marketing or sampling exclusions. Use only explicitly supported
observations in the joint mixed-frequency demand/fare model. Do not divide SEC
consolidated passenger revenue by this sampled mileage to manufacture fare/yield.

Declare models, as-of samples and matched baselines before fitting. Preserve the
proposal's 2013–19 initial / 2020–23 selection / locked 2024+ split, uncertainty
limits and weak-identification elasticity sensitivities. Economic fitting,
calibration, operator-to-issuer scope reconciliation, source-passage AI updates,
full archived runtime replay and financial integration remain open.

See [the fare/traffic matching gate](fare_traffic_matching.md) before designing the
joint estimator. No fitted policy or locked evaluation is accepted by this source
acquisition milestone.
