# Form 41 cost, operations and reported fuel history

`app/form41_history.py` imports three official TranStats full-table exports:

- [P-1.2 statement of operations](https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMI&QO_fu146_anzr=Nv4+Pn44vr4+Sv0n0pvny): quarterly revenues, expenses, operating income, depreciation, interest and taxes.
- [P-5.2 aircraft operating expenses](https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMK&QO_fu146_anzr=Nv4+Pn44vr4+Sv0n0pvny): quarterly aircraft-level costs, aircraft days, airborne hours and fuel issued.
- [P-12(a) fuel cost and consumption](https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMH&QO_fu146_anzr=Nv4+Pn44vr4+Sv0n0pvny): observed monthly scheduled/nonscheduled fuel quantities and costs by reporting region.

The browser selected **All Years, All Periods, All Fields** for each export.
`data/form41_downloads.json` records those selections, the observed schedule URLs,
download paths and captured field definitions. The ZIP bytes and visible official
field definitions are separately preserved in the immutable raw source store.
The URLs identify the official export form; the generated ZIP names are transient
download names, not permanent public asset URLs.

## Rebuild and availability

```bash
python scripts/build_form41_history.py --downloads data/form41_downloads.json
python scripts/build_form41_history.py --sources data/form41_history/<snapshot_id>/manifest.json
```

The second command uses preserved raw bytes and definitions without network access
or dependence on the Downloads folder. It reproduces the same snapshot ID, JSON
panels and Parquet contents. Each schedule has a separate JSON panel and Parquet
file; `SELECT row.period, row.unique_carrier, row.values.OP_REVENUES ...` accesses
the nested row structure. The latest pointer updates only after all three schedules
pass validation and Parquet readback. `/api/form41-history/status` verifies the
manifest and JSON panels before serving local coverage; Parquet is checked during
build/rebuild rather than on each coverage request.

These are **current-download reconstructions**, acquired October 9, 2026. No
row-level filing/release date or historical revision vintage is supplied by these
exports. Acquisition dates are preserved, not backdated to observation periods or
filing deadlines. A July 16, 2026 cutoff rejects this snapshot. The records can
support explicitly labeled retrospective reconstruction work; they do not prove
historical as-of information availability. The proposal's fit/selection/locked-test
partitions are labels on observations, not completed model fitting or evaluation.

## Units, grain and accounting boundaries

All original metric names survive in `values`, with source and normalized units
in the manifest. P12 and P52 financial figures are reported in thousands of USD
and converted to millions of USD. P52 airborne hours and aircraft days are in
thousands and converted to hours/days; fuel issued is in thousands of gallons and
converted to millions of gallons. Their reported two-decimal precision can imply
10-hour/day resolution; a reported zero is not replaced by an inferred quantity.
P12A costs are **dollars** and quantities **gallons**, converted to millions of USD
and gallons. Applying the quarterly schedules' thousand-unit multiplier to P12A
would be incorrect. Blank amounts remain null; reported zero remains zero. Signed
income/expense entries and negative quantities remain visible, with quality flags
where appropriate. No sign reversal or imputation is applied.

P12 grain includes DOT airline ID, unique carrier, financial entity, region and
quarter. P52 adds aircraft type/configuration/group. P12A retains monthly certificate
carrier records and each reported subtotal in separate fields. Every normalized row
contains its source hash, ZIP member and CSV line. Duplicate grains, unexpected
headers, malformed/nonfinite values, invalid aircraft codes, month/quarter mismatch
and future periods fail before publication. Profit and fuel identity differences
are flagged and preserved rather than automatically repaired.

Regions D/A/P/L, international aggregate I and system aggregate S stay separate.
An international or system total must not be added to its component regions.
Aircraft totals, equipment records and detailed aircraft types are not silently
combined. These BTS reporting regions have not been reconciled to the proposal's
US-plus-Canada analytical geography. DOT certificate carriers are not automatically
SEC consolidated holding-company boundaries. US Airways stays separate from AA;
feeder operators remain individual carriers and are not attributed wholesale to a
major airline. The official [BTS catalog](https://www.transtats.bts.gov/Tables.asp?QO_VQ=EGI&QO_anzr=Nv4+Pn44vr4+Sv0n0pvny+er21465+%28S14z+HE+Sv0n0pvny+Qn6n%29)
describes the carrier identifier and merger-reporting boundaries. Dated mappings,
scope reconciliation and fare/cost fitting remain required.

P52 aircraft fuel issued includes revenue and nonrevenue flights; P12A distinguishes
scheduled and nonscheduled consumption. Those definitions must be reconciled before
estimating fuel intensity or delivered-price basis. Cost categories and subtotals
are not added together indiscriminately. This importer does not estimate cost
flexibility, fit policy, or replace the monthly simulator's Q2 reference.

## Verified snapshot

Snapshot `db5f423f3cb010fabc3c949fc3e0ba914804c00f52274466df733f95bb2d9fc4`
preserves these raw export hashes:

| Schedule | Raw SHA-256 | Scanned rows | Retained 2013+ rows |
|---|---|---:|---:|
| P12 | `ead609f7ae744f8a179413a6e69c23d6bf54f313abc9f3c3bd4019f99eb82dfd` | 15,683 | 5,511 |
| P52 | `b9706c06c9cf2e034623ef544bfe0a465cbe5c5746301f7b32580bfeda8260d6` | 68,364 | 28,575 |
| P12A | `2f6b34ba12fb7a17d65a6a6eb51385097d1f6b2a6eab5774dc2460d5f14f2fb8` | 24,867 | 8,642 |

UA, DL and AA each have 54 quarters (2013-Q1–2026-Q2) in P12/P52 and 162
months (2013-01–2026-06) in P12A. US remains a separate 10-quarter/30-month
series ending in 2015-Q2/June. All carriers in the exports survive extraction;
coverage highlights the three proposal carriers without claiming consolidation.

Quality flags include 64 P12 operating identities differing by more than $30,
two P12A nonscheduled-cost identity differences, two records with negative reported
fuel quantities and one missing unique carrier identity. That unassigned January
2016 record retains raw carrier code `0JQ`, 3,000 gallons and $5,100, with null DOT
identity. It is not reassigned or dropped. P52 additionally retains 11 records with
negative reported fuel issued, eight with negative airborne hours and five with
negative aircraft days. These are field-flag counts and may overlap within records.
They require adjustment/reporting review before utilization or fuel-intensity fits;
positive values and parse success do not establish economic completeness either.

All 34 fixture records were independently checked at exact positions in the original
ZIP members. Hand checks include UA June 2026 total fuel of 376.132265 million
gallons and $1,338.279238 million cost, and a P52 623/Atlantic record with 0.01324
million gallons, 20 airborne hours and $0.04518 million fuel expense. Tests cover
units, signed values/nulls, reporting grains, identities, acquisition cutoff,
corruption, immutable JSON/Parquet rebuild and API failure reporting. The complete
suite has 97 passing tests. These checks establish import/software behavior, not
forecast validity. The browser displays verified cached coverage and explicitly
states that simulator integration remains pending.
The final snapshot's offline rebuild and cached API/browser ID agree exactly;
`screenshots/monthly-form41-history.png` records the verified coverage display.
