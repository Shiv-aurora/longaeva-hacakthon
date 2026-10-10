# Rolling-release traffic reconstruction

The annual-only traffic study exposed artificial 6–20-month information gaps.
The official [domestic catalog](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/data-bank-28ds-t-100-domestic-segment-data)
and [international catalog](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/%E2%80%A2-data-bank-28is-t-100-and-t-100f)
also provide intermediate rolling archives. Their actual observed links were
saved, and 110 additional ZIP files were downloaded and preserved by content hash.
Together with the initial annual sources, the declared source set contains 132
archives, including every monthly archive endpoint from January 2019 through
December 2023 for DD and FD. Total compressed source bytes: 654,121,165.

The acquisition manifest is `data/traffic_vintage_sources.json`. Catalog-link
observations, original ZIP bytes, source URLs, retrieval times and filename release
dates remain inspectable. This source set is separate from the old annual-only
operating snapshot and its completed traffic scorecard.

## Information available at a forecast date

`app/operating_vintages.py` builds the 48 month-end histories from January 2020
through December 2023. At each issue date, it selects the latest released archive
**separately for each scope and observation month**, then aggregates the selected
source records. It does not filter a final revised panel by publication date: that
would erase earlier available versions. Later corrections and withdrawn
contributions change only histories where that release is available.

Both DD and FD must be released for a month to provide a complete target, including
a scope with no contributing segments. Missing quantities are not treated as zero.
Only scheduled-passenger UA/DL/AA certificate records are retained, using the
existing declared market crosswalk and physical/quality gates. Other-carrier and
unmapped quantities are not used as demand observations. Original parsing still
checks every archive record; archive-wide flags are not attributed to a carrier.
The source boundary still excludes foreign-to-foreign flying and unassigned
regional marketing operations.

Some overlapping international archives share the September 19, 2023 release date
and release number. A tie is accepted only if every retained certificate/entity/
country/market aggregate and normalized metric agrees. The lexicographically first
source hash is chosen deterministically; all equivalent hashes are retained in the
selected release. Conflicting same-release aggregates fail. This equivalence is
limited to the declared three certificates, not every carrier in the archives.

Observation versions use content IDs. Each origin references its exact versions,
scope releases, excluded rows, carrier counts and newest complete period. Final
2013–2023 evaluation targets are stored separately from issued histories. No
2024+ target quantity is admitted. The design and builder hashes are saved before
normalization; native JSON and Parquet readbacks must agree before publishing the
latest pointer. Five local source modules are preserved, but interpreter/dependency
binary preservation and an isolated executable replay worker remain pending.

## Reconstruction limits

These are **current downloads of historically named releases**. The original
historical bytes and exact publication timestamps have not been independently
recovered. Filename release dates provide a reconstruction gate; they do not prove
point-in-time forecast performance. The source set supplies monthly endpoints,
but it is not a claim that every revision ever issued has been recovered.

Overlapping raw records across archives are repeated source versions, not additional
independent economic observations. More source files do not multiply the validation
sample. The earlier annual-only traffic model and its failed best-baseline comparison
remain unchanged. The next estimation run must use each origin's version-specific
history and re-score every model on identical targets; it cannot reuse the old
coefficients or claim improved forecast accuracy from narrower release gaps alone.

## Commands and verification

```bash
python scripts/build_traffic_vintages.py --workers 4
python scripts/audit_traffic_vintages.py
python -m pytest -q tests/test_operating_vintages.py
```

The builder runs offline from preserved public source bytes. Four parser processes
may normalize independent archives; they do not invoke agents or remote models.
The independent audit imports no operating parser, vintage selector or traffic
estimator. It re-sums original segment records and independently selects releases
at every issue date, checking exact target keys, RPM/ASM, entities, hashes,
complete-scope dates, alias quantities, sample counts and information lags.
Quantity revisions between adjacent issue histories are recorded explicitly.

Fourteen tests currently cover historical revision isolation, withdrawal without
fallback, noncontributing-scope release gates, equivalent and conflicting ties,
invalid/locked origins, unavailable/future histories, normalization-source/cache
checks, exact immutable rebuild, JSON/Parquet agreement and corruption, locked
archive rejection, preserved builder corruption and rehashed quantity tampering
detected by the independent original-source audit.

The published snapshot is
`b2fa6c7660552e232776e10cd0000770dbef1afe6d117a128e02c55e397b449c`.
It contains 7,788 distinct observation versions and 48 origin histories. The newest
complete observations are **3–6 months** behind issue dates, independently verified
for the actual targets. The offline rebuild returns the identical snapshot and all
summary fields. Both native Parquet tables match JSON. The full suite passes
**337 tests** (one existing Starlette/httpx deprecation warning).

The independent audit passes **312,457 checks**, re-summing **6,292,393 segment
versions across 132 archives**. Those repeated source versions are not independent
observations. The ordinary CLI audit reproduces the same record:
`data/traffic_vintage_checks/067486ee870fd57c0d29543a34f14986455cd6827f659c5503728c79c5037598.json`.

## An unresolved discrepancy prevents accepted estimation

The audit records eight quantity revision events, all for AA January 2021, at the
February and April 2022 issue dates. They are material: domestic RPM moves from
3,831.060 million to 10,246.474 million, then to 10,186.390 million. Other markets
also show large temporary revisions. These source values remain preserved.

The final DD/FD certificate targets imply Q1 2021 RPM of **24,528.256 million**.
American's [official Q1 financial tables](https://s202.q4cdn.com/986123435/files/doc_news/2021/04/1/1Q21-Financials.pdf),
page 2, report consolidated RPM of **22,464 million**, including mainline and regional
flying. The source-derived total exceeds that broader reported boundary by
**2,064.256 million**. This is an unresolved measurement/source/reporting-boundary
discrepancy, not proof of a specific erroneous row. Geography, regional operations,
foreign-to-foreign coverage and distance conventions still require reconciliation.
The source audit proves faithful extraction, not economic validity.

The downloaded issuer PDF is preserved under SHA-256
`091830bdcd3b4449a36b74333825e46864e78540944fa775419895555daf4fbb`.
`python scripts/review_traffic_vintages.py` extracts its operating table and saves an
immutable present-day review, with the exact quarterly source targets and revision
events. Quarterly issuer quantities are never divided into replacement monthly
observations. The earlier annual-only scorecard shares affected final targets and
now carries a visible source-review warning; its immutable results are not altered.

The saved unresolved review is
`data/traffic_vintage_reviews/5e8823d8eb9b3da3d52dd88680005f63c89957d85a5b61f6554719d7578bd5a2.json`.
The browser warning and unchanged scorecard were visually verified in
`screenshots/monthly-traffic-vintage-review.png`; no browser warnings or errors
were recorded. The final full suite passes 337 tests in 95.03 seconds.

Before a new fit, trace the January domestic additions by entity/route/aircraft and
compare same-scope T1/T2 and issuer mainline evidence. Any exclusion/correction must
have documented source support and release-aware treatment; a finding made today
cannot silently remove an observation from a past information set. The full fitted
demand/pricing requirement remains open.

The follow-up [original-record trace](traffic_revision_trace.md) now confirms
that all 1,975 later AA DD January 2021 payloads repeat January 2020, excluding
only the date fields. A temporary FD release repeats all 659 January 2020 payloads;
April restores its earlier 491-record cohort exactly. The earlier Q1 domestic
records match T2 F RPM/ASM/departures across all ten aircraft types. Independent
line/pair/quarter checks pass 46,525; correction design and residual boundaries
remain open. This strengthens the original review without altering its immutable
record, source observations or traffic scores.
