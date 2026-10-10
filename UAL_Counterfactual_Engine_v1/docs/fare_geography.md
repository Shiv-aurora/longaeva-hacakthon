# Original fare endpoint geography and coverage

This is a prerequisite for proposal D02/M03, not the fitted pricing/demand
deliverable. `app/fare_geography.py` scans every original DB1B header origin and
coupon destination across the preserved 2013–2023 inventory. It reads no fare or
passenger quantity and applies no fare/cohort eligibility filter. Surface coupons,
other carriers and records excluded from fare estimation still contribute original
airport, DOT city-market and World Area Code evidence.

## Frozen inputs and geographic convention

The separate extraction design was saved before this scanner read the archives:
`data/fare_geography/designs/fc15977cdd76d3e9c025cbd9a21805cf4c88ea0616dc4739192e8c09cf8daa33/design.json`.
It binds all 44 source hashes/URLs/releases, the original DB1B field-definition
PDF and preserved BTS WAC lookup
`b1efb1ef88ca3ad26f0bcabc3d215f48f1babe88c6cb76a64f99906b53143393`.
The exact model crosswalk and its generating source text/hash are embedded in the
design. This is a local declaration, not external preregistration.

The [BTS WAC lookup](https://www.transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_jbeYQ_NeRN_PbQRf)
defines geographic codes; our four-market business convention is a separate
model choice. It maps WACs 3/4 to Latin America and WAC 5 to Pacific. The lookup also
has other territory codes above 100, including Guam 826 and American Samoa 800.
The running fare version's `outside_US_WAC` flag literally excludes WAC >=100;
its label must not be read as proof that these codes describe foreign countries.
It is a declared WAC subset of the US-only survey, not complete US/Canada or
international fare coverage. No running producer was changed by this work.

## Original-record index

The grain is quarter/airport/city-market/WAC. Each row retains total, header-origin
and coupon-destination endpoint occurrences, source/member, first/last lines,
two line/endpoint-position witnesses, and an ordered digest of every contributing
raw line and endpoint position. Counts are endpoint occurrences, not unique
passengers, ticket markets or population demand. A repeated airport in the same
ticket can contribute more than once.

All observed WACs for an exact airport/city/quarter remain visible. Conflicting
WACs withhold assignment; there is no majority vote, nearest-quarter fill,
airport-only join or silent alias reconciliation. Unknown codes remain unassigned.
The complete build publishes JSON, native Parquet, immutable source bindings and
producer code only after all quarters and full-column Parquet readback succeed.
Full-history raw-record audit is a separate required step before model use.

## First real quarter checked

The independent oracle imports no fare/geography parser or aggregation code.
For 2013-Q1 it re-reads 3,046,411 original tickets and all 11,444,839 endpoint
occurrences, verifies all 450 airport/city/WAC rows, conflicts, counts, witnesses
and complete contributor digests, and passes 8,107 summary-field/binding checks.
Its immutable report is
`data/fare_geography_checks/8a522fe72b69481934fc3366fd5a5f2ed8a5fe86db63f4ef8a6aa23fd4864600.json`;
auditor bytes are retained under their SHA256. This does not audit later quarters
or the eventual complete Parquet table.

Original WAC 5 endpoints include GUM, PPG, ROP and SPN. The separately audited
fare quarter's entire 177,699 route/cohort cells have exact quarter/airport/city
endpoint evidence with no missing or conflicting assignment. UA has 145 Pacific
mainline cells and 443 Pacific ticketed-brand cells; these overlapping cohorts
are never summed. AA/DL/UA also have Latin America cells through US territories.
This is partial geography, not full international market measurement.
The immutable inspection binds both original-record audit reports and source
hashes:
`data/fare_scope_checks/c483000bba65ac46848c7885804b01a48d9263e07631c87875f200de8c463b31.json`.

Endpoint classification still does not match a directional fare market to a
T-100 flown segment. Connections, international-itinerary exclusions, regional
marketing, carrier transitions, sample composition and observation frequency
require separate treatment before the proposed joint estimator. Geographic
coverage is not a fixed-mix price index, carrier revenue yield or causal response.

## Commands and running work

```bash
.venv/bin/python scripts/build_fare_geography.py \
  --inventory data/fare_sources/ab5a09c49352533af3471b5dca1246826292ca1c918618e3149d948ea8e4b292.json \
  --wac-sha b1efb1ef88ca3ad26f0bcabc3d215f48f1babe88c6cb76a64f99906b53143393
.venv/bin/python scripts/audit_fare_geography.py \
  data/fare_geography/designs/fc15977cdd76d3e9c025cbd9a21805cf4c88ea0616dc4739192e8c09cf8daa33/design.json \
  --quarter 2013-Q1
.venv/bin/python scripts/check_fare_geography_part.py \
  --fare-audit data/fare_history_checks/parts/ff5219d6c59ab3e9426a5d20fe3222edde1364193d0ad7e3b7fa74315074b806.json \
  --geography-audit data/fare_geography_checks/8a522fe72b69481934fc3366fd5a5f2ed8a5fe86db63f4ef8a6aa23fd4864600.json
# After the complete build publishes, pass its manifest.json without --quarter:
.venv/bin/python scripts/audit_fare_geography.py data/fare_geography/SNAPSHOT_ID/manifest.json
```

The full scanner remains in progress under executable session 68782. Its outputs
are `/tmp/ual-fare-geography-build.json` and
`/tmp/ual-fare-geography-progress.jsonl`. The first-quarter independent audit
completed with exit 0 (session 35793); the coverage inspection also completed
with exit 0 (session 94347). Revalidate live handles before restarting work;
progress files alone do not prove liveness. Do not edit a loaded builder during
its extraction. The fare ingestion remains separately running under session
85686/PID 50768. Neither full real source history is accepted by a one-quarter
audit or synthetic rebuild fixture.

Six geography tests cover raw field/locked-date boundaries, Pacific territories,
conflict withholding, original ordered endpoint witnesses, all-quarter synthetic
native Parquet/rebuild, independent rejection of rehashed corruption and atomic
publication failure. Three coverage tests retain quarter/city keys, conflicts,
cross-region ambiguity and overlapping cohort separation. Synthetic data stay
isolated from real observations. See [the matching gates](fare_traffic_matching.md).
