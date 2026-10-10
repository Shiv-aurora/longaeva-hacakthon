# AA January 2021 source revision trace

The traffic vintage review found that final AA Q1 2021 DD/FD certificate RPM
exceeds the issuer's consolidated reported RPM. The source investigation now
establishes literal reuse of a different year's records in preserved BTS archives.
It does not establish whether the carrier report, export or another upstream
process introduced it. All original observations and model scores remain immutable.

## What the source bytes show

The comparison retains every literal pipe-separated field and pairs records
one-to-one, excluding **only year and month**. Counts, routes, reporting entities,
aircraft, distance, passengers, seats, freight, mail and flight minutes must all
match; duplicates retain their multiplicity. Empty cohorts cannot prove equality.
The dates and quantity definitions were checked against BTS's
[official segment record description](https://www.bts.gov/sites/bts.dot.gov/files/docs/explore-topics-and-geography/topics/airlines-and-airports/230176/reference-file-db28-segment-data-product.pdf).

| January 2021 source set selected at | DD AA records | Payloads also found in January 2020 DD | FD AA records | Payloads also found in January 2020 FD |
|---|---:|---:|---:|---:|
| January 31, 2022 | 1,224 | 0 | 491 | 0 |
| February 28, 2022 | 1,975 | **all 1,975** | 659 | **all 659** |
| April 30, 2022 | 1,975 | **all 1,975** | 491 | 0 |
| Final acquired evaluation set | 1,975 | **all 1,975** | 491 | 0 |

The changed February sources were released February 1, 2022. The April FD release
restores all 491 earlier January 2021 payloads exactly. DD retains the 1,975
January 2020 payloads with the year changed to 2021. The finding covers scheduled
AA passenger/combination records in these DD/FD cohorts; it is not an all-carrier
or all-month audit for this defect.

## Independent quarterly support and remaining differences

Actual Q1 source histories selected at January 31, 2022 are compared against the
preserved original T2 **F** scheduled-service CSV, by reporting entity, aircraft
group/type/configuration. Z and other service totals are not added to F.
All ten domestic entity/aircraft grains match exactly in RPM, ASM and departures.
Across all regions, 19 of 28 grains match all three quantities. The later/final
Q1 source cohorts have 33 grains and nine exact matches; residuals and aircraft
grains absent from T2 remain explicit.

Latin and Pacific differences remain. DD/FD omit foreign-to-foreign segments,
while T2 has a broader geographic reporting scope. This explains a possible
boundary to investigate, not a proved cause of each residual. The trace retains
missing quantities and unmatched grains; they are never filled with zero.
Issuer consolidated reporting includes regional flying and is a separate boundary.
No quarterly T2 or issuer total is allocated into invented monthly observations.

The complete trace contains 23,136 original segment rows from 13 preserved archives,
28 original T2 F rows, source members/line numbers, full text and fields, every
matched pair, quarterly selections, per-grain residuals and entity/route/aircraft
revision details. These are repeated source versions, not independent economic
samples. The trace and its source code are saved together in
`data/traffic_revision_traces/01d985eb3066862d5fcfc98dd25f0c4c230afc264385819eb560e0115b45777c/`.

## Verification and estimation treatment

```bash
python scripts/trace_traffic_revisions.py
python scripts/audit_traffic_revision_trace.py data/traffic_revision_traces/01d985eb3066862d5fcfc98dd25f0c4c230afc264385819eb560e0115b45777c/record.json
python -m pytest -q tests/test_traffic_revision_trace.py
```

The offline rebuild reproduces the exact trace identity and summary. The independent
audit imports neither the trace builder nor operating/model parsers. It reads
original ZIP lines, verifies complete retained row sets, selections, payload
multisets and one-to-one pairs, then recomputes integer quarterly sums and Decimal
T2 residuals. **46,525 checks pass**. Descriptive route-delta rankings are not
separately covered by that audit. Its record is
`data/traffic_revision_checks/0b8621c61bf34fa904785ace2e44b69bdb3ffce6c156e29577865a83416a118e.json`.
Six tests cover exact payload matching, multiplicity, scope isolation, checksums,
locked-quantity isolation, quarterly identity, residuals, missing values, incomplete
quarters and duplicate T2 grains. Two existing T2 acquisition fixtures now use a
fixed synthetic clock after UTC midnight exposed their dependence on the test date.
No production acquisition date or availability gate was changed. The final full
suite passes **343 tests** in 91.50 seconds, with one existing Starlette/httpx
deprecation warning. A rehashed forged source-row quantity was also rejected by
the independent original-line audit.

The app shows the concrete source finding beside the original scorecard; visual
proof is `screenshots/monthly-traffic-revision-trace.png`. Browser warnings/errors
are empty. Financial simulation behavior and previous scores are unchanged.

The next step is a separately declared correction design: bind replacement source
records to this proof, preserve rejected versions, and disclose that the review
was made today. Historically issued information and a present-day corrected
evaluation target are distinct. Keep uncorrected vintage comparisons; do not
retroactively hide the bad releases or use future review knowledge without a label.
Reconcile the remaining boundaries and refit every candidate/baseline on identical
declared targets before accepting the fitted component. The full demand/pricing,
calibration and proposal validation requirements remain open.
