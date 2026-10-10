# Local development and history

The Git root is the parent `longeva-hackathon` directory, on branch `main`.
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
cd UAL_Counterfactual_Engine_v1
.venv/bin/python -m pytest -q
```

This history is local. No remote was configured or push performed during its
creation. A later remote publication is a separate action.
