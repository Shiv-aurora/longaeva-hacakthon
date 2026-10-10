# Local data and Git boundaries

Git records the small public baseline inputs, reviewed anchor declarations,
model specifications, examples and acquisition manifests in this directory.
Raw downloads, generated Parquet/history stores, model-code archives, numerical
audits and scenario exports remain local and are excluded by the root `.gitignore`.
Ignoring them does not delete or alter any data. Local computations continue to
use their preserved bytes and existing source hashes.

A Git clone alone does not contain the complete acquired data or archived runs.
Rebuild commands and source methods are documented in the project README and
`docs/`; original inputs must be acquired or transferred separately. Preserve
the local `data/raw`, research snapshots and registered code bundles when moving
an existing experiment. Commit history is not a replacement for an immutable
data manifest, historical publication evidence or reproducible runtime archive.

This repository was initialized after the implementation had been built. Its
initial commits record existing components in reviewable groups using current
commit timestamps; they do not reconstruct the original development timeline.
