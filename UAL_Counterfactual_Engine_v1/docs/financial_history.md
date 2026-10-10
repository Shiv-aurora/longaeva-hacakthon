# Historical financial ingestion

The importer downloads the official SEC Company Facts snapshots for UAL (CIK
100517), DAL (27904) and AAL (6201). It preserves the original bytes and retrieval
metadata by SHA-256, and retains individual fact objects with filing dates,
accessions, exact JSON pointers, units, periods and source hashes. Curl uses the
host's normal HTTPS certificate trust; certificate verification is never disabled.

Source and API scope: https://www.sec.gov/search-filings/edgar-application-programming-interfaces

## Commands

```bash
python scripts/build_history.py --cutoff 2026-10-09
python scripts/build_history.py --sources data/history/<snapshot_id>/manifest.json --cutoff 2019-12-31
```

The first command fetches the three public snapshots, with one second between
requests. `--user-agent` can set an appropriate research identity/contact. The
second performs an offline reconstruction using the preserved raw sources. HTTP
requests to `/api/history/status` only read the cache; they never download data.

Raw files live in `data/raw/<sha256>/source.bin`, with `source.json` metadata.
Each `data/history/<snapshot_id>` contains all retained mapped versions in JSON
and Parquet, the derived quarterly panels, and a coverage/definition manifest.
The manifest hashes the semantic facts/panels and parser version. Existing raw
bytes and snapshot data are verified and never silently overwritten.

## Periods and availability

At the October 9 cutoff, each entity has 54 quarterly rows from 2013-Q1 through
2026-Q2. Revenue, operating income and fuel expense cover all 54 quarters for each
entity. Variable coverage is reported individually; a row does not imply every
field is available. Missing values remain missing.

Facts are selected by actual start/end dates, not XBRL `fy`, `fp` or `frame`, which
can describe the filing rather than the comparative observation. Only 10-K/10-Q
and amendments, standard entity-wide US-GAAP concepts and USD values are admitted.
The latest available filing date is selected; equivalent tag transitions use an
explicit priority. Same-release conflicting values are excluded and reported.
Reported quarters are preferred. Where only YTD flows exist, a quarter is current
YTD minus prior YTD, with both source legs preserved and cross-filing differences
flagged. Missing prior YTD is never treated as zero.

This is a **filing-date reconstruction from a current API snapshot**. It does not
recover release timestamps, removed extraction contexts, original historical API
snapshots, or earlier earnings announcements. Differencing across filings can mix
revisions. Direct quarter and derived YTD values should be reconciled further
against the original filings before a historical forecasting claim.

## Definitions and gaps

Operating expense is an explicit revenue-minus-operating-income identity. Nonfuel
expense subtracts reported fuel. FCF is OCF minus `PaymentsToAcquireProductiveAssets`;
this is a simple cash-use definition, not each carrier's adjusted FCF measure.
United's purchase amount is net of flight-equipment deposit returns. Peer purchase
and fuel definitions require airline-specific reconciliation before comparison.

Continuing-operations cash flow is kept as a separate variable, not silently
substituted for total cash flow. United has 42 total OCF observations plus 18
continuing-operations observations, with overlapping coverage. Several stock and
interest concepts also have gaps across taxonomy changes. Some standard concepts
are absent even when the original filing reports a corresponding custom concept.
AAL's historical reporting entity includes reorganization/merger transitions;
peer/regime comparisons require boundary review.

Company Facts omits custom airline/market dimensions. This importer does not yet
supply four-market ASM/RPM, fuel gallons, regional contract expense, loyalty or
advance sales. BTS traffic, Form 41, DB1B/DB1C, historical fuel/weather, segment
filings and operating-carrier mappings remain required by the proposal.

## Current simulator correction

The Q2 2026 reference now uses these independently checked UAL values:

- OCF: H1 $6,409M minus Q1 $4,799M = Q2 $1,610M.
- Cash asset purchases: H1 $3,015M minus Q1 $1,672M = Q2 $1,343M.
- Simple Q2 FCF: $267M, or $89M per derived reference month.

Previously the simulator used H1 FCF / 6, or $565.7M/month. That mixed operating
and cash periods and carried Q1 collection seasonality into every forecast month.
The correction aligns the periods; it does not establish forward monthly cash
seasonality, collection timing or a complete cash/debt model. The monthly run
record names both source legs of the subtraction.

## Evaluation partitions

Quarter rows are labeled 2013–2019 fitting, 2020–2023 selection and 2024+ locked
retrospective test. These are data assignments only. No model has been fit or
selected using this panel and no locked-test score has been computed. Software
checks of reported 2025/2026 figures are accounting/source tests, not forecast tests.
