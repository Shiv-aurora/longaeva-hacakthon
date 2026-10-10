# Reviewed traffic correction and source ablation

This is research toward proposal D02, M03, V01–V04. It does not close those
requirements. Original source artifacts and the annual-only scorecard remain intact.

## Original-source correction

The pre-scan declaration is `data/traffic_correction_spec.json`. It limits the
correction to AA January 2021 scheduled-passenger DD/FD records whose entire literal
non-date payload multiset equals January 2020, including multiplicity. No numerical
threshold, partial record match, or model residual chooses a correction. The scanner
reviews 24 eligible archives and confirms four entire reused cohorts.

The correction uses the declared earlier January 2021 DD/FD sources, released by
January 4, 2022 and available before every affected forecast issue. It keeps the
original selected release and both-scope publication gates, rejected/reference/
replacement source lines, and actual effective contributor hashes. Other carriers,
months and source records remain unchanged. Every reviewed row carries:

> Present-day reviewed reconstruction using hindsight source-quality knowledge;
> not a historical point-in-time forecast.

Review knowledge is dated October 9, 2026. Earlier availability of replacement
records does not make this later quality decision historical knowledge.

The current immutable snapshot is
`f8a8749ba591396d58a642dc4fe5955ac0c8c0ae95a5fb8a52b97c80ee9663eb`.
JSON and three Parquet tables verify; the offline rebuild reproduces the same ID.
The independent original-record audit passes 244,351 checks and is preserved at
`data/reviewed_traffic_checks/ca965bcb9b85431a53c5b0e0ab5dc065392e0115f03dc1f2a36a26021f81c43b.json`.
It checks complete raw cohorts, correction lines, all 48 histories, targets,
unchanged peers, quantities, effective contributors and dates. There are 23 affected
issue histories, 50 history rows with changed RPM/ASM, and two evaluation rows with
changed quantities. Metadata corrections can affect additional rows without
changing their quantities.

Reviewed AA Q1 2021 certificate totals are 18,101.795811 million RPM and
31,190.701567 million ASM. These are below the issuer's broader consolidated totals
of 22,464 million RPM and 37,764 million ASM. That resolves the particular impossible
RPM ordering; it does not establish complete consolidated reconciliation. Regional
marketing, foreign-to-foreign service and remaining geographic/T2 scope differences
remain open. No quarter discrepancy is allocated into invented monthly observations.

```bash
.venv/bin/python scripts/build_reviewed_traffic.py --workers 4
.venv/bin/python scripts/audit_reviewed_traffic.py
```

## Four-case fitting design

`data/monthly_traffic_vintage_spec.json` saves the existing model family, grids,
ridges, initial 2013–19 sample, selection 2020–23 and locked 2024+ boundary before
fitting. It compares:

1. Native rolling histories and native final targets.
2. The identical native rolling forecasts and reviewed final targets.
3. Reviewed rolling histories and reviewed final targets.
4. Original annual-only histories and reviewed final targets.

The first two cases isolate target correction without changing predictions. The
second and third isolate history correction. The third and fourth compare source
availability as well as quality; they are descriptive source ablations, not causal
effects. Every rolling fit selects the exact origin's observation versions, rather
than filtering a final revised panel. Initial seasonal means and MASE scales freeze
at the first complete 12 × 84 initial grid, June 30, 2020, shared by every case.

There are three source histories, 42 expanding fits each, three pooling candidates
and three baselines. All four cases retain 1,800 requested forecasts per model.
The initial 0.1.0 fitting run failed on numerically unsupported recursive AR
predictions at October 2020. Its design, coefficients and failure keys are preserved
in `data/monthly_traffic_vintage_failures/d5dd367b0b77c2559459889e2b865a69d81c65cf8006773d41e4bb81bbb5e800.json`.

The 0.1.1 protocol amendment was saved before refitting, after seeing that failure.
It preserves the absolute-log bound of 40 and all grids/ridges, retains unsupported
predictions as null with exact reasons, and scores every model/case on the global
intersection of supported keys. There are 37 AR failures in each rolling case and
zero in the annual case: 1,763 common forecasts, with all 48 cells represented in
both disruption and recovery regimes. This subset does not establish whole-domain
robustness. The local declaration is not external preregistration.

The immutable study is
`31226d81e2e4cc0163b965034b6e9e6127ca61cc04ab0a9873a56adbebf8d164`.
It preserves fit samples/digests, coefficient and grid-likelihood witnesses,
posteriors, distributions, source-target provenance, failures and scores. Its
separate pointer does not replace the original annual-only study.

```bash
.venv/bin/python scripts/fit_monthly_traffic_vintages.py
.venv/bin/python scripts/audit_monthly_traffic_vintages.py
```

Native intervals are conditional on fitted parameters. Parameter uncertainty,
calibration, distributional baselines, block uncertainty and locked temporal tests
remain open. Served certificate traffic is not unconstrained demand, a pricing
policy, consolidated traffic or investment validation. No fitted component is
integrated into the financial sensitivity solely because its source audit passes.

## Independently checked numerical results

The separate statistical audit passes 735,982 checks over all 126 expanding fits.
It verifies exact input/design/code bindings, per-issue revision samples, frozen
initial means/scales, augmented-design least-squares coefficients, every variance
grid likelihood and posterior using sequential scalar updates, recursive forecasts
and null/failure reasons, native quantiles, target ablations, common-key selection,
all 48 point-score cells and disruption/recovery scorecards. Its preserved record is
`data/monthly_traffic_vintage_checks/8860c6585dc6ade4c2d1868c936e3765d4db494710965eb70f896549d755fa54.json`.
Independent auditor/helper source bytes are preserved by their SHA256 hashes.
Raw ZIP source audits are separately bound; this statistical run does not reread
every archive or add new independent economic observations.

| History / evaluation target | Best state MASE | Best baseline MASE | State native 80% coverage | AR failures |
|---|---:|---:|---:|---:|
| Rolling native / native | 6.834918 | 6.097960 | 77.48% | 37 |
| Rolling native / reviewed | 6.835518 | 6.099292 | 77.47% | 37 |
| Rolling reviewed / reviewed | 6.832708 | 6.069367 | 77.47% | 37 |
| Annual native / reviewed | 9.833676 | 6.910281 | 68.27% | 0 |

Lower MASE is better. The strongest baseline is regularized AR in the rolling cases and the fixed initial
seasonal mean in the annual case;
state rho 0.5 is selected for each rolling case and rho 0 for annual history.
All scores use the same 1,763 forecasts. Improved source availability changes the
comparison materially; the narrow AA correction does not overturn the state model's
loss to the strongest baseline. The full failures, overlapping horizon counts and
conditional interval diagnostics remain available in the study/audit downloads.
Coverage is descriptive, not a calibrated guarantee or a full-domain result.

`data/monthly_traffic_vintage_review.json` pins the numerical review. The read-only
API verifies immutable study/design/source bundles, upstream input artifacts,
statistical/original-source reports and preserved auditor sources before publishing
scores or downloads. Corruption returns HTTP 503. Endpoints are
`/api/traffic-vintages/status`, `/api/traffic-vintages/record` and
`/api/traffic-vintages/audit`. The dashboard presents this separate source comparison
above the unchanged original annual-only study. It does not deploy a fitted policy.

Rehashed forecast/target/score changes, missing common samples, duplicate score
cells, locked targets and numerical support are tested with independent equations.
Original-source correction and code/checksum/API download gates have separate tests.
Study Parquet exports and exact offline estimation replay under preserved runtime
binaries remain open reproducibility work; source preservation and one audited
execution do not close proposal R01.
