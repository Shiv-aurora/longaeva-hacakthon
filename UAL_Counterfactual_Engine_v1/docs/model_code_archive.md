# Executable model preservation

Monthly simulation, failure-search and target-inversion API/CLI exports now carry
a `replay_code` receipt. Before a run, the wrapper copies the model's source,
package initializer and a fixed replay worker into a content-addressed local
bundle under `data/model_code/<bundle_id>`. The descriptor hashes every file and
preserves the original core-file concatenation hash, so existing run IDs remain
unchanged. Registration metadata under `data/model_code/.registered` records
bundles explicitly captured from our own checkout.

The wrapper captures source at import and checks it before and after execution.
Edits while the server is running require a server restart. A run will fail rather
than attach newly edited source to an already loaded model. Direct calls to core
simulation functions remain useful for research/tests; use the server or run
scripts for exports with an archive receipt.

## Commands

From the application directory with the project virtual environment:

```sh
.venv/bin/python scripts/archive_model_code.py --verify-runs
.venv/bin/python scripts/run_monthly.py --replay data/runs/<run_id>.json
.venv/bin/python scripts/run_failure_search.py --replay data/failure_searches/<run_id>.json
.venv/bin/python scripts/run_target_inversion.py --replay data/target_inversions/<run_id>.json
```

Replay verifies the locally registered bundle, fixed file allowlist, per-file
hashes, original code hash and environment before executing its fixed worker in
an isolated Python process (`-I -B`). It supplies the saved evidence and fuel
directly; it does not load today's data or import today's model modules. All
deterministic output is recomputed and compared, including input snapshots,
manifest, assumptions, evidence metadata, summaries, fleet, matrices, valuation
and witnesses. Only the monthly summary's `runtime_seconds`, the search result's
top-level `runtime_seconds`, and the archive receipt are excluded from numerical
comparison. Integral JSON numbers such as `1` and `1.0` remain equivalent after
browser export.

Run JSON cannot provide executable source, a path, a module name or a command.
It can reference only a verified local registration. An arbitrary copied bundle
without its trusted registration is rejected. Preserve **both** the run JSON and
the entire `data/model_code` directory, including `.registered`, in trusted backups
or project transfers. Registrations attest to our local capture operation; they
are not digital signatures or a security boundary against someone who can alter
the checkout/archive filesystem. Review unfamiliar source before trusting it.

## Preserved versions and limits

The current `monthly-fleet-0.3.2`, `joint-failure-search-0.1.0` and
`operating-target-inversion-0.1.0` source bytes were archived before changes to the
core model. Legacy exports without receipts can select a matching local bundle;
if one is missing, migration is possible only while our own current checkout
still has the exact original code hash. Older monthly versions 0.2.0, 0.3.0 and
0.3.1 have no retained original source bytes and cannot currently be replayed.
Their run records are retained unchanged and the inventory reports them as
`original_source_unavailable`.

The current archive inventory verified five preserved records (one monthly, two
failure searches and two target inversions). A fresh browser download of the
10,000-path monthly reference run `951752edf9e6…` also replayed from its bundle
with all deterministic fields matching. Reports are retained in
`data/model_code_checks/`. The full software suite passes 134 tests, including
replay after a copied working tree and loaded model functions change, unknown
bundles, source/environment tampering and modified formerly unchecked outputs.

The descriptor checks exact Python, NumPy and DuckDB versions, OS and machine
architecture. It records these versions but does not bundle an interpreter,
dependency wheels, BLAS libraries or a container. Those runtime binaries still
need to be retained/provisioned separately; matching version strings alone do
not promise identical behavior on every installation. The replay comparison
rejects a numerical difference. Process isolation prevents accidental import of
the current checkout; it is not a sandbox for untrusted code.

The fixed source list covers these three current workflows. It must be updated
when a model gains dependencies, along with its worker and tests. Fitted-artifact
builders have their own source/input preservation but are not executed by this
run replay mechanism. Executable replay is reproducibility evidence, not
historical forecast validation, causal identification or investment performance.
