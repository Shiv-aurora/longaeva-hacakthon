# BTS monthly operating history

`app/operating_history.py` imports the official BTS DB28 DD/FD segment archives.
The acquisition list in `data/bts_downloads.json` records the actual catalog links
and local files downloaded through the browser. There are 28 archives: annual
2013–2025 DD/FD files and the latest August 2025–July 2026 windows. The normal
HTTPS command-line request to bts.gov failed in this environment; the browser
download succeeded. No certificate validation was disabled.

Official catalogs and schema:

- [Domestic segment archives](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/data-bank-28ds-t-100-domestic-segment-data)
- [International segment archives](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/%E2%80%A2-data-bank-28is-t-100-and-t-100f)
- [BTS segment record description](https://www.bts.gov/sites/bts.dot.gov/files/docs/explore-topics-and-geography/topics/airlines-and-airports/230176/reference-file-db28-segment-data-product.pdf)
- [Official World Area Code lookup](https://www.transtats.bts.gov/Download_Lookup.asp?Y11x72=Y_jbeYQ_NeRN_PbQRf)

## Build and replay

```bash
python scripts/build_operating_history.py --downloads data/bts_downloads.json --wac-sha b1efb1ef88ca3ad26f0bcabc3d215f48f1babe88c6cb76a64f99906b53143393
python scripts/build_operating_history.py --sources data/operating_history/<snapshot_id>/manifest.json --cutoff 2026-10-09
```

The first command preserves downloaded ZIP bytes, original URLs, retrieval dates
and hashes. The second rebuilds from those preserved bytes, without downloads or
dependence on the user's Downloads folder. The lookup is fetched only when its
preserved hash is omitted. `/api/operating-history/status` only checks local data.

Each snapshot contains normalized archive aggregates, the selected monthly panel,
Parquet, coverage, schema/quality checks and an immutable manifest. Data and
manifest digests are checked before cached coverage is served. Rebuild reads back
and verifies the Parquet contents against the panel. Sources remain reconstructable
from `data/raw/<sha256>/source.bin`.

## Units, scope and quality

Only scheduled passenger service (class F, configuration 1/3) enters the panel.
ASM is seats times segment distance; RPM is passengers times segment distance.
Monthly load factor is total RPM / total ASM, never an average of segment load
factors. Departures and elapsed ramp/airborne minutes are retained. Extra sections
can make performed departures exceed scheduled departures; their ratio is not
automatically a completion probability. No quarterly traffic is divided into
fabricated monthly observations.

The importer handles both 28-field layouts and the older international 32-field
cabin layout, summing reported cabin seats and passengers only in that layout.
Airborne and ramp time positions differ between layouts. Aircraft group and type
are part of the segment key, so legitimate different-group records survive.
Repeated scheduled keys fail closed. Decimal freight/payload is admitted without
rounding passenger counts; these quantities are not included in the passenger
panel. Missing city identifiers and carrier-country codes are flagged while valid operating metrics survive; missing country codes remain null.
Missing required quantities, malformed records and out-of-period observations
fail the build. Unknown geography and RPM above ASM are reported, not repaired.

## Geographic mapping

World Area Codes map segment endpoints into the proposal's analytical markets.
Canada is domestic; Greenland is Atlantic. Puerto Rico and U.S. Virgin Islands
are Latin America. U.S. Pacific possessions are Pacific. Europe, Africa and
Middle East are Atlantic; Asia/Oceania are Pacific, with India explicitly Atlantic.
The India exception is supported by United's current BTS entity 10874 (Atlantic)
in the separately preserved DB20 snapshot, which also identifies 0A875 domestic,
10876 Latin and 10877 Pacific. The DB20 source hash is
`b7b42fb938e9307ff52a5bb1eb4d9556aba26da0af310db8ee8543c79db17e52`.

These assignments are explicit and still require historical/issuer reconciliation.
Ambiguous pairs across two non-domestic regions remain `cross_region`; unknown
codes remain `unmapped`. They are not silently allocated to a convenient market.
The full original ZIPs retain aircraft-level observations for subsequent fleet
productivity work; this panel currently aggregates aircraft types.

## Carrier boundaries

Rows describe **operating carriers**, not ticketing/marketing carriers or the
consolidated company. UA, DL and AA exclude feeder operators such as SkyWest.
Those operators remain in their own rows: all SkyWest flying is not United Express.
Legacy US Airways stays separate from AA pending a dated merger/boundary mapping.
The panel does not infer airline ownership from a carrier code or country code.
Historical Carrier Decode validation and regional attribution remain requirements.

DD plus FD omits foreign-to-foreign (FF) segments, including some intra-Pacific
flying by U.S. carriers. `both_archive_scopes_available` means both DD and FD
archives exist for a month; it does not establish complete issuer traffic coverage.
Do not reconcile this directly to consolidated UAL revenue or regional fuel.

## Dates and evaluation

Archive filenames retain period endpoints, release numbers and release dates.
At a cutoff, the latest eligible archive replaces the entire scope/month,
including formerly present records removed in a revision. Overlapping annual
and rolling files are not added together. Missing months are not zero-filled.

This is an **archive-release-date reconstruction from current downloads**, not
independently preserved historical bytes or exact release timestamps. Older source
vintages not collected are unavailable rather than imputed. The current WAC lookup
also needs historical country-boundary review. The old schema document describes
international release restrictions that do not match the periods actually present
in the latest linked public files; coverage follows inspected contents and their
filename dates, not a presumed universal publication lag.

Rows carry 2013–2019 fit, 2020–2023 selection and 2024+ locked-test labels. These are
partitions, not completed model evaluation. Demand/pricing fitting, delivered fuel
basis, cost and utilization fitting, historical baselines and forecast scores remain
pending. The simulation still uses its disclosed Q2 operating reference until
historical inputs and issuer boundaries are reconciled.

## Verified current snapshot

Snapshot `bc4ee21c76691e623cea52e775808bde23689acdf9c763da9c2e22fb2f1979fc`
uses the October 9, 2026 cutoff. All 28 archives were scanned: 6,730,446 raw
records and 4,995,357 scheduled passenger records **including archive overlap**.
Release selection yields 36,193 monthly carrier/country/market rows. UA, DL and
AA each have 163 observed months, January 2013–July 2026, with all four analytical
markets present. US Airways remains a separate 30-month series ending June 2015.
These counts do not establish consolidated-company completeness.

UA has 13 and DL 51 additional `cross_region` monthly rows. AA has none. Across
all carriers, 312 monthly rows are flagged outside the four-market mapping, three
have RPM above ASM, and two retain missing carrier-country metadata. These rows
remain inspectable; they are not zero-filled, reassigned or automatically repaired.

An independent positional sum over both latest ZIPs matched the preserved UA June
2026 totals: 28,171,221,560 seat-miles, 24,200,605,584 passenger-miles, 16,553,421
seats and 13,959,502 passengers across 4,642 scheduled segment records. All 23
fixture rows were checked against exact original source positions. The API serves
the verified snapshot, and Parquet readback matched the JSON panel during build.

Cutoff checks exclude future releases. With only the presently collected annual
and latest rolling archives, a 2019-12-31 cutoff reaches December 2018; a
2023-12-31 cutoff reaches December 2022; and 2026-07-16 reaches December 2025.
This is a **vintage acquisition gap**, not evidence that later observations were
unpublished. Collect intermediate rolling releases before historical evaluation;
never use the October snapshot as information available at an earlier cutoff.
