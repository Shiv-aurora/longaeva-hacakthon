# Reporting-boundary panel

The panel joins preserved scheduled-passenger BTS traffic to Form 41 by **exact
certificate carrier, reporting entity and quarter**. Joining financial region D to
the proposal's domestic geography would lose or misattribute traffic. The matched
panel keeps the observed geographic crosswalk beside each financial entity.

For example, United's 2026-Q2 domestic financial entity `0A875` contains
44,291,004,702 observed DD/FD ASMs: 43,177,200,792 in the proposal's domestic
geography, 877,263,270 in Latin America and 236,540,640 in the Pacific. United's
Atlantic financial entity also has small domestic, Latin and cross-region traffic
components. These are reported classifications, not reassigned accounting amounts.
No financial revenue/cost allocation to those geographic components is estimated.

## Preserved structure and gates

`panel.json` contains two separate tables:

- `entity_quarters`: union of observed traffic and P12/P52 entity keys, with
  weighted load factors, geography crosswalks, original financial/aircraft rows,
  monthly traffic source legs and missing/ambiguous reporting flags.
- `certificate_fuel_quarters`: P12A monthly fuel combined only within the same
  certificate carrier and DOT ID. System fuel is never copied into each financial
  entity. A field's quarterly value requires all three months and three nonnull
  observations; reported zero remains distinct from missing. Cost per gallon is
  quarterly total cost divided by total gallons and withheld for source flags,
  missing identity, missing/nonpositive quantities or negative cost.

Traffic uses the upstream panel's selected scope-month releases. A revision replaces
the old scope-month even when an entity disappears; old archive rows are not added
back. Financial quarter values are not divided into synthetic monthly observations.
Absent traffic entity-months remain unknown rather than inferred zero. The panel
retains other carriers and their unmatched records; it does not assign feeder
operators to UA/DL/AA or combine AA and US before their actual reporting changes.
System/international aggregates, aircraft configurations and equipment records stay
separate. P52 aircraft fields are retained, not blindly rolled into a fleet total.

`matched_reporting_identity` means traffic, P12 and P52 share an unambiguous
carrier/entity key and consistent DOT identity and financial region. It does **not**
mean their economic scopes match. Scheduled-passenger DD/FD traffic excludes
foreign-to-foreign and other service classes; P52 includes fuel for revenue and
nonrevenue flights. Transport-related revenue, regional contracts and holding-company
consolidation remain unreconciled. Consequently **all cross-schedule fit eligibility
is withheld** in this diagnostic panel. Same-schedule analysis can proceed separately;
cross-schedule coefficients require an explicit target-specific scope reconciliation.

[BTS explains the international release restrictions](https://www.bts.gov/topics/airlines-and-airports/restricted-data),
including the three-year restriction for US carrier foreign-to-foreign observations.
Missing FF coverage must not be interpreted as zero international operations.
[Its T2 description](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/data-bank-21-form-41-schedule-t-2-t-100)
identifies carrier/entity/date/aircraft type as the quarterly traffic grain and is a
reference for the remaining service/aircraft reconciliation.

## Verified snapshot

Snapshot `35aad1ea2a2dcc4a4c4755c80115529391b9a7598e3ca5dd757d9547288508f0`
contains 14,112 entity-quarter rows and 2,895 certificate fuel-quarter rows across
the retained carriers. Coverage of the proposal carriers is:

| Certificate | Matched entity-quarters | Matched quarters | Complete fuel quarters |
|---|---:|---:|---:|
| UA | 216 | 54 | 54 |
| DL | 216 | 54 | 54 |
| AA | 216 | 54 | 54 |
| US (separate legacy series) | 30 | 10 | 10 |

The three major carriers have four matched entities per quarter from 2013-Q1 to
2026-Q2. July 2026 traffic contributes four additional partial-quarter rows per
carrier without corresponding quarter financials. It is not a completed Q3 result.
Across all carriers, unmatched and non-passenger records remain visible. Coverage
counts are source/alignment evidence, not fitting or forecast scores.

Both nested Parquet tables were read back and checked against their JSON contents.
Nine tests cover revision replacement, exact entity matching, geographic crosswalks,
weighted load factors, missing months, conflicting identities, fuel weights/nulls,
duplicate grains, upstream corruption and immutable rebuilds. Actual preserved
Form 41 fixture records survive the join and Parquet round trip. The complete suite
has 106 passing tests.

`scripts/audit_reporting_panel.py` independently scans the original raw ZIP lines
without using the importer parser or entity-join function. For 2013-Q1, 2019-Q4,
2023-Q4 and 2026-Q2 it reproduced ASM, RPM, airborne minutes and record counts for
51 entity-quarters, using 115,060 raw segment records in eight archives. Raw line
witnesses, archive hashes and audit-code hash are recorded in
`data/reporting_panel_checks/<snapshot_id>.json`. Checking 2024+ input accounting
does not unlock those observations for model selection or forecast scoring.

## Offline commands and limits

```bash
python scripts/build_reporting_panel.py
python scripts/build_reporting_panel.py --operating data/operating_history/<snapshot_id> --form41 data/form41_history/<snapshot_id>
python scripts/audit_reporting_panel.py
```

The builder verifies upstream manifest/panel hashes before joining. It saves its own
source bytes and hash and publishes `latest.json` only after both Parquet tables
pass readback. Explicit upstream directories reproduce the same snapshot without
network access. Saving this builder does not archive the entire Python environment
or every imported dependency; complete executable-version replay remains a broader
delivery task.

Current downloaded Form 41 records establish acquisition availability on October 9,
2026, not historical filing vintages. The manifest records the latest acquisition
date across both pipelines and the WAC reference. The 2013–19 / 2020–23 / 2024+
partitions are preserved labels; no fitted model or historical forecast evaluation
is performed by this builder. This is a script/data deliverable and makes no change
to the simulator's Q2 reference, policy coefficients or UI.
