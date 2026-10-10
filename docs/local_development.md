# Local development and history

The Git root is this `UAL_Counterfactual_Engine_v1` application directory, on
branch `main`. GitHub exposes `app/`, `scripts/`, `tests/`, `docs/` and the
application README directly at the repository root.
Initial commits record the existing implementation in separate component groups:
data acquisition, operating mechanics, investor workflows, reproducibility,
research models, independent audits and UI/API delivery. They use current commit
timestamps and specific subject/body messages, not an invented development history.

Public source inputs and model declarations are tracked. Original multi-GB
downloads, generated research/run stores, local environments and credentials are
excluded. Source-table blank cells and CSV line endings are preserved verbatim;
proposal PDFs are binary files. See `data/README.md` for transfer/rebuild boundaries.

## Review commands

```bash
git status --short --branch
git log --oneline --reverse
git log -1 --format=full
git rev-list --count HEAD
git show --stat HEAD
```

Stage related source, tests and methods together. Commit messages should explain
the resulting behavior and any measurement boundary that matters to a reviewer.
Keep actual observations separate from fixtures, and do not stage generated
source archives merely to make the working tree appear reproducible.

## Verification for this initial history

The complete Python suite passed **385 tests** in 117.59 seconds, with one existing
Starlette/httpx deprecation warning. Eight added implementation comments explain
missing observations, marginal percentile curves, assumed pricing, atomic cash
settlement, retention capacity, paired grid worlds, operating-asset valuation and
convergence streaks. Comparing parsed Python syntax trees before and after all
eight edits confirmed unchanged runtime statements and expressions.

The live fare and geographic ingestion modules retain their declared source
hashes. Their processes, data and preserved model-code bundles were left intact.
The comment changes do not establish fitted policies, forecast calibration,
investment value or completion of the proposal.

Run checks from the application directory:

```bash
.venv/bin/python -m pytest -q
```

The initial history was created locally, then published to
`https://github.com/Shiv-aurora/longaeva-hacakthon.git`. A subsequent layout commit
promotes the application directory to the repository root while preserving those
existing commits. The original proposal PDFs and distribution ZIP remain in the
parent workspace; they are not part of the current application-root snapshot.

## Focused submission workstation

The monthly workstation now opens at `/`, with the original daily fuel pilot at
`/fuel-pilot`. Five tabs separate scenario analysis, failure search, target
inversion, evidence and thesis export. Four illustrative presets keep purchase
and retention assumptions visible without placing every advanced control on the
first screen.

Verification for this change: **112 targeted tests passed** (four API/route tests
and 108 monthly, purchase, retention, search, inversion and thesis tests). Browser
checks exercised the combined and no-retention presets, both 3×3 inverse grids,
inspection of a compatible target point, and a downloaded one-page thesis PDF.
The combined result matched the numerical case in `demo_case_verification.md`.
The primary controls, chart and three metrics fit a 1366×769 desktop viewport;
the 389×843 mobile layout scrolls without horizontal overflow. No browser console
errors or warnings were observed. These checks establish implementation behavior,
not forecast calibration or validation of assumed airline responses.
