# Aircraft capacity and exposure reconciliation

`app/t2_history.py` imports the official **T2 quarterly aircraft-type statistics**.
The [T2 field definitions](https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FIH)
provide seat-miles, revenue hours, reported total hours, assigned aircraft-days,
fuel and service class. The [BTS product description](https://www.bts.gov/browse-statistical-products-and-data/bts-publications/data-bank-21-form-41-schedule-t-2-t-100)
identifies aircraft type and carrier entity as reporting dimensions. The inspected
TranStats summary catalog also says T2 includes reported international/military
operations that can be absent from its public segment tables.

These observations supply the capacity measure missing from the P52 utilization
study. They also reveal why the two sources cannot be substituted automatically.
This is a source/target preparation milestone. No capacity model or simulator
integration has been completed by this importer.

## Source and reporting scope

The all-years, all-quarter, all-field download contains 269,110 rows. Keep all
110,667 rows from 2013 onward, across certificate carriers and service classes;
retain unidentified records as unresolved original source rows. Do not invent a
carrier or merge unidentified rows whose incomplete keys appear identical.
Identified duplicate reporting grains fail. Preserve raw ZIP bytes, field
definitions, exact source member/line, nulls, signed quantities and reported months.

T2 quantities are full miles, hours, days and gallons, with multiplier one. P52
aircraft quantities have their separate thousand-unit conversion. The normalized
T2 rows keep all fields and the source separates revenue airborne hours from
reported total hours and ramp hours. Deprecated nonrevenue-hours and route-days
are often null; they are not filled or used to manufacture an identity.

Service classes overlap: K=F+G, V=L+N+P+R, Z=K+V. Capacity targets select **Z only**,
never add it to its component classes. Use detailed financial regions D/A/P/L and
passenger/combined configurations 1/3. System/international aggregates stay outside
this sum. Financial regions are not relabeled as the proposal's geographic markets.
These are exact operating certificates, not holding-company or marketed regional
networks. US Airways remains a separate legacy series.

`data/t2_aircraft_classification.json` adds official type 822, Boeing 747SP, as
widebody to the prior analyst mapping, for 37 types. T2 reports some such American
observations without P52 exposure. The original P52 mapping/fit remains unchanged.
Unknown types invalidate the certificate-quarter target scope rather than silently
reduce its aircraft coverage.

## Quarterly targets and eligibility

For each carrier and NB/WB group, sum represented T2 Z rows first. Derive:

- ASM / assigned aircraft-days.
- Reported total hours / assigned aircraft-days.
- Revenue hours / assigned aircraft-days.
- ASM / reported total hours, and ASM / revenue hours.

Use ratios of sums, not means of row ratios. Require known identity, three reported
months, nonnegative required quantities and a positive denominator. A missing or
negative required quantity withholds that target across the group quarter. A zero
individual exposure is aggregated before division. A missing assigned-days field
does not invalidate ASM per revenue hour when its own inputs are valid. Hour/day
ratios above 24 are withheld. RPM exceeding ASM is flagged and withholds targets.

The accounting decomposition is
`ASM/day = reported hours/day × ASM/reported hour`. It does not identify a causal
response, available spare aircraft, crew feasibility or a physical capacity ceiling.
Assigned days divided by calendar days yields average **assigned exposure**, not
owned fleet inventory. Quarter averages are not divided into monthly observations.

| Certificate | Quarters | NB/WB group quarters | Eligible ASM/day | Eligible ASM/revenue-hour |
|---|---:|---:|---:|---:|
| UA | 54 | 108 | 88 | 108 |
| DL | 54 | 108 | 102 | 108 |
| AA | 54 | 108 | 101 | 108 |
| US, retained separately | 10 | Not modeled here | — | — |

Major-carrier coverage is 2013-Q1–2026-Q2. Of 324 group quarters, 291 have eligible
assigned-days capacity targets. That includes 148 fitting-period, 88 selection-period
and 55 locked-period input observations. These are source counts, not forecast
scores; missing/invalid exposure affects important disruption quarters.

## Two-way P52 comparison

Compare exact carrier/DOT-ID/entity/region/quarter/aircraft-group/type/configuration
keys. T2 has 6,276 selected Z rows; 49 have no matching P52 record. Among matched
rows, total hours have 17 differences beyond five hours, assigned days have 16
beyond five days, and fuel has 19 beyond five gallons. There are also null fields.
The five-unit threshold reflects P52's two-decimal thousand-unit reporting precision;
it is a quantization check, not a statistical tolerance or an instruction to repair
one source with the other.

The reverse comparison retains **2,721 P52 records without T2 Z counterparts**:
2,635 have zero reported aircraft quantities, 70 have positive exposure/activity,
and 16 have unknown/invalid quantities. Eighteen of the unmatched rows have positive
assigned days. Idle/transitioning aircraft and reporting differences must therefore
remain visible; omitted T2 rows cannot automatically become zero assigned exposure.
The panel records two-way exposure agreement for each group quarter and leaves
cross-schedule model transfer ineligible. Even agreement does not establish an
owned-fleet, consolidated financial or geographic-market reconciliation.

T2 ratio targets describe **the aircraft rows represented in T2**. They do not become
whole-mainline fleet estimates merely because they share a carrier/group label with
P52. The original P52 component cannot inherit T2 capacity coefficients without
an explicit deployment scope, exposure treatment and validation.

## Preserved result and verification

Snapshot `7f6abe2ff74d7aecea05156039ef79adac3603b89209d648c14323bb65e5b5aa`
contains normalized JSON plus four verified Parquet tables: original normalized
rows, group-quarter targets, forward P52 comparison and reverse P52 omissions.
Structured keys preserve typed identities; per-group status counts use lists to
avoid DuckDB inserting null fields into variable dictionary keys.

Raw ZIP SHA-256:
`c77a96e28f55ddc1f3d6c8f42c44e6b81497a69fc50c55b24c2d1eb77e5f1115`.
Definition SHA-256:
`045778183cac47711be91e1df437175d5e5c4eba75a1bbf07af71fdea3b276a3`.
The manifest preserves classification, upstream P52 snapshot, source acquisition,
builder/dependency bytes and hashes. `latest.json` publishes only after every
Parquet readback matches JSON. This preserves code/input provenance, not an entire
executable environment.

`scripts/audit_t2_history.py` independently reads original T2/P52 CSVs with Decimal
arithmetic, without calling the importer/target parser. It checks all 324 group
quarters, all 6,276 selected T2 lines, every eligible target, and both directions
of the P52 comparison. Original line sets and target witnesses are saved under
`data/t2_checks/`. Checking locked-period **input accounting** does not unlock
those observations for selection or forecast evaluation.

Nine tests include real export fixtures, service/system double-count prevention,
weighted ratios, target-specific null/negative/zero exposure gates, physical and
identity checks, P52 differences/omissions, acquisition cutoffs, upstream corruption,
immutable builds and all four Parquet round trips. The full suite passes 143 tests.

## Commands and next model step

```sh
.venv/bin/python scripts/build_t2_history.py
.venv/bin/python scripts/audit_t2_history.py
.venv/bin/python scripts/build_t2_history.py --from-snapshot data/t2_history/7f6abe2ff74d7aecea05156039ef79adac3603b89209d648c14323bb65e5b5aa
```

The preserved-source rebuild needs neither a network connection nor the original
Downloads path and reproduces the same snapshot with unchanged builder bytes.
Snapshot acquisition on October 9, 2026 does not establish historical publication
vintages; a July 16 cutoff rejects it.

`data/t2_capacity_spec.json` declares the next capacity comparison before model
implementation/scoring: frozen 2013–19 fitting, 2020–23 selection, recursive
3/6/12-month quarterly horizons, direct capacity versus utilization×productivity
ridge models, common-target seasonal baselines and explicit source exclusions.
2024+ targets stay locked for subsequent forecast evaluation. The covered-aircraft
scope and reverse exposure gaps remain requirements for simulator deployment.
