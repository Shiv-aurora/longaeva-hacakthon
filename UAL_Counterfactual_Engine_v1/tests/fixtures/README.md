# Financial source fixture

`sec_ual_2025_2026.json` is a reduced offline subset of the official UAL SEC
Company Facts response, not synthetic financial history. Its 125 fact objects
were checked for exact membership in the preserved full response:

- URL: https://data.sec.gov/api/xbrl/companyfacts/CIK0000100517.json
- Full-response SHA-256: `6621f43f61efe96e02f2aada43fbed7ea989332e4fad2c6c453f9abe139e6d32`
- Preserved bytes: `data/raw/<sha256>/source.bin`

The subset covers selected concepts and periods needed by the source/accounting
tests. Array indices and JSON pointers generated during fixture normalization
refer to the reduced fixture. They are not pointers into the full response.
Historical ingestion instead normalizes the full preserved response and retains
its original array indices and raw-source hash.

Tests that deliberately mutate dates, values or metadata do so in memory to
exercise rejection and availability rules. They are software checks, not
historical forecast evaluation.

## BTS fixtures

`bts_segment_subset.json` contains 23 exact source lines from six official
DD/FD archives, with full-source hashes, archive member names and original line
numbers. It includes both 28-field and legacy 32-field layouts, legitimate
aircraft-group differences, decimal freight, missing city identifiers and missing carrier-country metadata. `bts_wac_lookup.csv` preserves
the official WAC lookup (SHA-256
`b1efb1ef88ca3ad26f0bcabc3d215f48f1babe88c6cb76a64f99906b53143393`).

Tests construct small archives from these lines. Test archive hashes and line
indices identify those subsets, not the full official files. Deliberate test
mutations exercise revision selection and rejection; they are not observations.
# T2 aircraft statistics fixtures

`t2_rows.csv` contains nine exact rows from the official October 9, 2026 all-field
T2 export. `t2_rows_sources.json` records original ZIP/member/line pointers. The
fixtures include overlapping F/K/L/V/Z classes, an unmatched AA 747SP with missing
exposure, a signed negative quantity and unresolved anonymous reporting rows.
They are source parsing/accounting fixtures, not forecast-validation observations.
