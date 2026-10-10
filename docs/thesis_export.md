# One-page thesis export

The monthly screen now exports an analyst thesis record as a one-page PDF and a
companion JSON containing the complete thesis and independently replayed monthly
run. This implements the investor handoff on proposal page 4 for the current
sensitivity model. It does not complete the proposal's fitted simulator or
investment validation.

## What the handoff records

The analyst supplies a title, hypothesis, chosen reference, next checkpoint date,
observable measure/source URL, and evidence that would overturn the view. These
fields are required, have explicit length limits and are kept separate from model
evidence. The checkpoint must be after the saved information cutoff. The source URL
is recorded and linked; the exporter does not retrieve it or verify the analyst's
claims. Plain text is escaped before PDF rendering, and unsupported/control glyphs
are rejected rather than silently replaced.

The export derives the scenario, horizon, policy, path count, seed, four P10/P50/P90
cash/FCF distributions, chosen-reference disagreement, modeled schedule shortfall,
queued net fleet entries, conditional month-end cash-floor fraction, opening cash
date, dated source links and interpretation limits from the verified run. The full
JSON retains every setting, fleet deficit/month, reviewed ledger excerpt, input
hash and assumption, together with the source run and archived replay receipt.

The paired no-shock reference uses the saved pathwise shock impact. The frozen
reference uses the saved difference of paired shock impacts under the two policies.
It never subtracts marginal medians or interprets grid counts as probabilities.
Current monthly cores save quantiles of FCF summed path by path. Optional financing,
other investing and restricted transfers are outside FCF, so ending minus opening
cash is not used for funded runs. Independently replayed legacy unfinanced cores
retain the earlier cash-identity derivation. Funded PDFs show the fixed incremental
event totals; complete event dates, scope and analyst basis remain in the JSON.

The modeled schedule constraint is the largest **monthly median** shortfall, not
a quantile of each path's largest shortfall. A no-shortfall result is reported
without inventing a binding crew/hub constraint. The cash-floor fraction uses
month-end balances and excludes the opening balance; it is uncalibrated. Fleet
entries remain inferred net inventory movements. Without a purchase plan their
delays leave capex unchanged; explicitly mapped purchases move settlement and
funding while earlier deposits keep their dates. The JSON retains the complete
purchase allocation and residual capex assumptions.
Periods beyond 12 months are marked exploratory. Optional target-inversion valuation,
full discovery grids, price/consensus targets and missing fitted policy/crew/hub
constraints are not added to a monthly thesis as fabricated outputs.

## Verification and reproducibility

`POST /api/monthly/thesis` takes the displayed run record and analyst notes. It
replays the numerical record in an isolated process using the locally registered
archived monthly source and preserved inputs before deriving the thesis. Altered
results fail even if a caller recomputes their receipt hash. It accepts no uploaded
Python source or arbitrary command. The export uses the run's registered core and does not modify simulation outputs.

Each content-addressed `data/theses/<artifact_id>` contains:

- `thesis.pdf`, confirmed to contain exactly one page;
- `thesis.json`, containing the thesis and complete verified source run;
- `manifest.json`, with output digests and renderer/library versions;
- renderer `builder.py`, two embedded font files and their license.

Integral JSON floats/integers have a canonical representation, so equivalent
browser and Python records produce the same export identity. PDF creation metadata
is fixed for reproducible rendering. Atomic immutable writes support repeated
exports without partially published files. Download endpoints verify every file
and record digest before returning the PDF or JSON; arbitrary file formats are
not served.

CLI commands:

```sh
.venv/bin/python scripts/export_thesis.py --run data/runs/951752edf9e632c5b4bd9ed594d06c1ad451c809ac9d638c799d537273fc48aa.json --notes data/thesis_example_notes.json
# Old immutable export remains readable; its archived renderer is no longer the current replay renderer.
```

The example notes are explicitly illustrative. The browser default's 10,000-path
run produced the same artifact as the CLI:
`eb1ccbe64fefe5807472b9fa36267f0cb25d0bac35b209fa91e0c974eb59375d`.
Both downloaded files matched their manifest hashes. Replaying the saved thesis
recomputed the numerical derivation and reproduced the PDF bytes. The final PDF
was rendered to PNG and visually checked; an output copy is
`output/pdf/illustrative-thesis.pdf` with its companion JSON beside it.

The replay command requires the saved renderer hash and ReportLab version to match
this installed renderer. After renderer changes it reports an unsupported current-
renderer replay instead of claiming success; saved PDFs remain checksum-verifiable.
Renderer/font bytes and library version numbers are preserved, but interpreter and
library binaries are not bundled. Complete future-version executable preservation
remains R01 work.

## Browser behavior and checks

At `/monthly#thesis-export`, the notes start blank. Scenario changes disable export
and hide previous links until the displayed run matches the settings. Changes to
notes hide earlier links; changes during an export require a new export. Restoring
exact saved settings is supported. Each download is tied to the saved run ID.
Browser checks confirmed stale-input rejection, recovery to the saved settings,
successful export and both downloaded formats. Existing user tabs were not reloaded
or navigated; a separate background preview was used. Screenshot:
`screenshots/monthly-thesis-export.png`.

17 new tests cover all handoff fields, independent cash/constraint calculations,
paired-reference semantics, note/URL/date/glyph gates, escaped markup, deterministic
one-page layout at maximum lengths, browser-number canonicalization, immutable
rebuild, archived run/PDF replay, rehashed tamper rejection, atomic file preservation
and verified API downloads. The full suite passes 343 tests, with the existing
Starlette/httpx deprecation warning. Runtime dependencies now include ReportLab
4.4.9 and pypdf 6.10.0 in the lock file.

## Remaining delivery boundary

The export faithfully records the current model's assumptions and limitations.
Fitted behavior, historical forecast validation, calibrated uncertainty, detailed
cash/funding, supported crew/hub/rival scenarios, source-passage AI extraction,
React and analyst usability/finals work remain active proposal requirements. An
exported thesis is a reviewable analyst handoff, not investment-performance evidence
or an automatically validated investment view.

Renderer version 0.4.0 supports cash-event, purchase-funded and retention runs, keeps
FCF separate from financing, and describes exact-count purchase delays in the PDF.
Purchase prices, deposits, financing and residual capex remain analyst assumptions.
See `purchase_bridge.md` for the partial allocation boundary. Earlier
immutable exports remain checksum-verifiable, but `replay_thesis` requires the
matching current renderer; their older renderer bytes are preserved and execution
of arbitrary historical renderers is not implemented. Source monthly run replay
continues through its registered model archive.

Retention-enabled records also show the retained aircraft peak and fixed additional
maintenance/carrying total. Variable extra fuel remains a path distribution, not
a sum of marginal medians. The companion preserves every replacement/exit link,
eligibility assumption, limit, applied/unapplied request and effective queue.
