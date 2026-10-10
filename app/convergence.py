"""Repeated-seed numerical stability of the declared monthly sensitivity.

This measures sampling error conditional on one model and its fixed assumptions.
It does not fit, calibrate or combine the two policy models into a probability law.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import math
from pathlib import Path
import platform
import shutil
import tempfile
from time import perf_counter

import numpy as np

from .data import fuel_volatility, load_fuel
from .evidence import digest, load_snapshot
from .fleet import capacity_paths
from .monthly import MonthlyScenario, VERSION as MONTHLY_VERSION, _rollout, _world, baseline, month_sequence, shock_weights, simulate_monthly

VERSION = "monthly-convergence-0.2.0"
CORE_FILES = ("convergence.py", "monthly.py", "fleet.py", "evidence.py", "data.py", "cash_bridge.py")
POLICIES = ("frozen", "assumed_constrained")
QUANTILES = ("p10", "p50", "p90")
MONEY_METRICS = ("cumulative_fcf_musd", "cumulative_fcf_impact_musd",
                 "cumulative_operating_income_impact_musd", "ending_cash_musd",
                 "minimum_cash_musd", "cumulative_revenue_musd",
                 "response_contribution_to_fcf_musd")
METRICS = (*MONEY_METRICS, "cumulative_operating_margin_pct")
Z95 = 1.959963984540054
MAX_PATH_MONTHS = 300_000_000  # generated once, not the sum of rescored prefixes
MAX_SUMMARY_BYTES = 12_000_000_000


def checkpoints(base: MonthlyScenario) -> list[int]:
    return sorted({h for h in (1, 3, 6, 12, base.horizon_months) if h <= base.horizon_months})


@dataclass(frozen=True)
class ConvergenceSpec:
    path_counts: tuple[int, ...] = (1000, 5000, 10000, 20000, 50000)
    replicates: int = 12
    absolute_tolerance_musd: float = 25
    relative_tolerance: float = .01
    margin_tolerance_pp: float = .05
    breach_tolerance_fraction: float = .005
    consecutive_levels: int = 2
    batch_paths: int = 5000

    def validate(self, base: MonthlyScenario) -> None:
        base.validate()
        if not isinstance(self.path_counts, (list, tuple)) or not 3 <= len(self.path_counts) <= 10:
            raise ValueError("Choose three to ten increasing path counts")
        if any(isinstance(n, bool) or not isinstance(n, int) or not 20 <= n <= 1000000 for n in self.path_counts):
            raise ValueError("Each path count must be an integer between 20 and 1000000")
        if any(a >= b for a, b in zip(self.path_counts, self.path_counts[1:])):
            raise ValueError("Path counts must be strictly increasing")
        if base.paths not in self.path_counts:
            raise ValueError("Path counts must include the displayed scenario's path count")
        for name, low, high in (("replicates", 8, 24), ("consecutive_levels", 2, 3), ("batch_paths", 20, 10000)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        for name, low, high in (("absolute_tolerance_musd", .0001, 10000),
                                ("relative_tolerance", 0, .2), ("margin_tolerance_pp", .0001, 10),
                                ("breach_tolerance_fraction", .000001, .2)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{name} must be finite and between {low} and {high}")
        if self.path_counts[-1] * self.replicates * base.horizon_months > MAX_PATH_MONTHS:
            raise ValueError("Convergence study exceeds the 300-million generated path-month work limit; reduce counts, repeats or horizon")
        if summary_storage_bytes(base, self.path_counts[-1], self.replicates) > MAX_SUMMARY_BYTES:
            raise ValueError("Convergence study exceeds the 12GB temporary summary-storage limit; reduce counts, repeats or horizon")


def summary_storage_bytes(base: MonthlyScenario, paths: int, replicates: int) -> int:
    return len(POLICIES) * len(checkpoints(base)) * len(METRICS) * paths * replicates * 8


def replica_seeds(root_seed: int, replicates: int) -> list[int]:
    """Keep the displayed run as replica zero; derive distinct other seeds.

    SeedSequence mixes the replica ID, a retry counter, root and fixed domain.
    Reduction respects MonthlyScenario's integer-seed contract; any collisions
    with another recorded seed are retried rather than treated as independent.
    """
    seeds = [root_seed]
    for replica in range(1, replicates):
        retry = 0
        while True:
            sequence = np.random.SeedSequence([replica, retry, root_seed, 271828])
            seed = int(sequence.generate_state(1, dtype=np.uint64)[0]) % 1_000_000_001
            if seed not in seeds:
                seeds.append(seed)
                break
            retry += 1
    return seeds


def wilson_interval(successes: int, n: int) -> tuple[float, float]:
    """Pointwise approximate 95% binomial sampling interval, including 0/n."""
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0 or isinstance(successes, bool) or not isinstance(successes, int) or not 0 <= successes <= n:
        raise ValueError("Wilson interval requires an integer count between zero and n")
    p = successes / n
    denominator = 1 + Z95 ** 2 / n
    center = (p + Z95 ** 2 / (2 * n)) / denominator
    radius = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n ** 2)) / denominator
    return max(0., center - radius), min(1., center + radius)


def path_outcomes(base: MonthlyScenario, b: dict, spot: float, sigma: float, fleet: dict,
                  rng: np.random.Generator | None = None) -> dict:
    """Use the production engine; accumulate each path before quantiling it."""
    worlds = _world(base, spot, sigma, rng=rng)
    rollouts = {mode: {intervention: _rollout(b, base, *worlds, spot, intervention, mode, fleet)
                       for intervention in (False, True)} for mode in POLICIES}
    impacts = {mode: rollouts[mode][True]["free_cash_flow_musd"] - rollouts[mode][False]["free_cash_flow_musd"] for mode in POLICIES}
    response = (impacts["assumed_constrained"] - impacts["frozen"]).cumsum(axis=1)
    horizons = checkpoints(base)
    result = {}
    for mode in POLICIES:
        control, scenario = rollouts[mode][False], rollouts[mode][True]
        revenue = scenario["revenue_musd"].cumsum(axis=1)
        oi = scenario["operating_income_musd"].cumsum(axis=1)
        vectors = {"cumulative_fcf_musd": scenario["free_cash_flow_musd"].cumsum(axis=1),
                   "cumulative_fcf_impact_musd": impacts[mode].cumsum(axis=1),
                   "cumulative_operating_income_impact_musd": (scenario["operating_income_musd"] - control["operating_income_musd"]).cumsum(axis=1),
                   "ending_cash_musd": scenario["cash_musd"],
                   "minimum_cash_musd": np.minimum.accumulate(scenario["cash_musd"], axis=1),
                   "cumulative_revenue_musd": revenue,
                   "response_contribution_to_fcf_musd": response if mode == "assumed_constrained" else np.zeros_like(response),
                   "cumulative_operating_margin_pct": np.divide(100 * oi, revenue, out=np.zeros_like(oi), where=revenue > 0)}
        result[mode] = {str(h): {metric: np.array(values[:, h - 1], copy=True) for metric, values in vectors.items()} for h in horizons}
    return result


def summarize_replica(outcomes: dict, cash_floor: float, seed: int, paths: int) -> dict:
    """In-memory reference for tests and small diagnostic callers."""
    return _summarize_vectors(lambda mode, horizon, metric: outcomes[mode][horizon][metric],
        list(outcomes[POLICIES[0]]), cash_floor, seed, paths)


def _summarize_vectors(read, horizons: list[str], cash_floor: float, seed: int, paths: int) -> dict:
    result = {"seed": seed, "paths": paths, "policies": {}}
    for mode in POLICIES:
        result["policies"][mode] = {}
        for horizon in horizons:
            metrics, path_hash, count = {}, hashlib.sha256(), 0
            for metric in METRICS:
                values = read(mode, horizon, metric)
                if values.shape != (paths,) or not np.isfinite(values).all():
                    raise ValueError("Temporary path summary is incomplete or nonfinite")
                metrics[metric] = dict(zip(QUANTILES, map(float, np.quantile(values, [.1, .5, .9], method="linear"))))
                path_hash.update(np.asarray(values, dtype="<f8").tobytes())
                if metric == "minimum_cash_musd":
                    count = int(np.count_nonzero(values < cash_floor))
                del values
            low, high = wilson_interval(count, paths)
            metrics["cash_floor_breach"] = {"count": count, "fraction": count / paths, "wilson95_low": low, "wilson95_high": high}
            # Bytes include the complete paired-path derived outcomes, not just quantiles.
            metrics["path_outcomes_sha256"] = path_hash.hexdigest()
            result["policies"][mode][horizon] = metrics
    return result


def append_batch(folder: Path, outcomes: dict) -> None:
    for mode in POLICIES:
        for horizon, vectors in outcomes[mode].items():
            for metric in METRICS:
                with (folder / f"{mode}-{horizon}-{metric}.f64").open("ab") as stream:
                    stream.write(np.asarray(vectors[metric], dtype="<f8").tobytes())


def summarize_disk_replica(folder: Path, base: MonthlyScenario, seed: int, paths: int) -> dict:
    def read(mode, horizon, metric):
        path = folder / f"{mode}-{horizon}-{metric}.f64"
        if path.stat().st_size != paths * 8:
            raise ValueError("Temporary path-summary byte count differs")
        return np.fromfile(path, dtype="<f8", count=paths)
    # Read one vector at a time. No whole monthly trajectories or entire ensemble
    # are kept in RAM, and no approximate/streaming quantile sketch is used.
    return _summarize_vectors(read, list(map(str, checkpoints(base))), base.cash_floor_musd, seed, paths)


def level_diagnostics(replicas: list[dict], previous: list[dict] | None, spec: ConvergenceSpec, stochastic: bool) -> list[dict]:
    rows = []
    for mode in POLICIES:
        for horizon in replicas[0]["policies"][mode]:
            for metric in METRICS:
                for quantile in QUANTILES:
                    values = np.array([r["policies"][mode][horizon][metric][quantile] for r in replicas])
                    mean = float(values.mean())
                    se = float(values.std(ddof=1))  # single-run error; never divide by sqrt(replicates)
                    absolute = spec.margin_tolerance_pp if metric.endswith("_pct") else spec.absolute_tolerance_musd
                    tolerance = max(absolute, spec.relative_tolerance * abs(mean))
                    change = None if previous is None else float(np.sqrt(np.mean((values - np.array([r["policies"][mode][horizon][metric][quantile] for r in previous])) ** 2)))
                    rows.append({"policy": mode, "horizon_months": int(horizon), "metric": metric, "statistic": quantile,
                                 "selected_seed_estimate": float(values[0]), "replicate_mean": mean,
                                 "mc_standard_error_single_run": se, "approximate_mc_half_width95": Z95 * se,
                                 "replicate_min": float(values.min()), "replicate_max": float(values.max()),
                                 "paired_rms_change": change, "tolerance": tolerance,
                                 "passes": bool(change is not None and max(Z95 * se, change) <= tolerance)})
            samples = [r["policies"][mode][horizon]["cash_floor_breach"] for r in replicas]
            fractions = np.array([x["fraction"] for x in samples])
            se = float(fractions.std(ddof=1))
            # Zero/all-success replicates must not falsely report zero error.
            width = max(max(x["fraction"] - x["wilson95_low"], x["wilson95_high"] - x["fraction"]) for x in samples) if stochastic else 0.
            width = max(width, Z95 * se)
            change = None if previous is None else float(np.sqrt(np.mean((fractions - np.array([r["policies"][mode][horizon]["cash_floor_breach"]["fraction"] for r in previous])) ** 2)))
            rows.append({"policy": mode, "horizon_months": int(horizon), "metric": "cash_floor_breach", "statistic": "fraction",
                         "selected_seed_estimate": float(fractions[0]), "replicate_mean": float(fractions.mean()),
                         "mc_standard_error_single_run": se, "approximate_mc_half_width95": width,
                         "replicate_min": float(fractions.min()), "replicate_max": float(fractions.max()),
                         "paired_rms_change": change, "tolerance": spec.breach_tolerance_fraction,
                         "passes": bool(change is not None and max(width, change) <= spec.breach_tolerance_fraction)})
    return rows


def study_convergence(base: MonthlyScenario, spec: ConvergenceSpec = ConvergenceSpec(), snapshot: dict | None = None,
                      fuel: list[dict] | None = None, progress=None) -> dict:
    started = perf_counter()
    spec.validate(base)
    snapshot = load_snapshot() if snapshot is None else snapshot
    fuel = load_fuel() if fuel is None else fuel
    fuel = [r for r in fuel if r["date"] <= base.information_cutoff]
    reference = simulate_monthly(base, snapshot=snapshot, fuel=fuel)
    b = reference["baseline_inputs"]
    spot, sigma = fuel[-1]["usd_per_gallon"], fuel_volatility(fuel) * math.sqrt(21)
    fleet = capacity_paths(b, base, b["fleet_plan"], reference["months"], shock_weights(base))
    seeds = replica_seeds(base.seed, spec.replicates)
    streams = [np.random.default_rng(seed) for seed in seeds]
    levels, previous, consecutive = [], None, 0
    stable = False
    planned_bytes = summary_storage_bytes(base, spec.path_counts[-1], spec.replicates)
    generated = 0
    with tempfile.TemporaryDirectory(prefix="ual-path-summaries-") as temporary:
        if shutil.disk_usage(temporary).free < planned_bytes + 1_000_000_000:
            raise ValueError("Insufficient temporary disk space for the requested path-summary budget plus a 1GB reserve")
        folders = [Path(temporary) / str(i) for i in range(spec.replicates)]
        for folder in folders:
            folder.mkdir()
        prior_count = 0
        for paths in spec.path_counts:
            replicas = []
            for i, (seed, folder, rng) in enumerate(zip(seeds, folders, streams)):
                for offset in range(prior_count, paths, spec.batch_paths):
                    count = min(spec.batch_paths, paths - offset)
                    outcomes = path_outcomes(replace(base, paths=count, seed=seed), b, spot, sigma, fleet, rng=rng)
                    append_batch(folder, outcomes)
                    del outcomes
                    generated += count
                    if progress is not None:
                        progress({"phase": "sampling", "level_paths": paths, "replica": i + 1,
                                  "replicates": spec.replicates, "completed_replica_paths": offset + count,
                                  "generated_paths": generated})
                replicas.append(summarize_disk_replica(folder, base, seed, paths))
            diagnostics = level_diagnostics(replicas, previous, spec, base.stochastic)
            passing = all(row["passes"] for row in diagnostics)
            # One passing budget is insufficient: a failed later budget resets
            # the stability streak under the same predeclared tolerances.
            consecutive = consecutive + 1 if passing else 0
            levels.append({"paths": paths, "replicas": replicas, "diagnostics": diagnostics,
                           "passes": passing, "failed_checks": sum(not row["passes"] for row in diagnostics),
                           "consecutive_passing_levels": consecutive})
            if progress is not None:
                progress({"phase": "level_complete", "level_paths": paths, "generated_paths": generated,
                          "failed_checks": levels[-1]["failed_checks"], "consecutive_passing_levels": consecutive})
            previous, prior_count = replicas, paths
            if paths >= base.paths and consecutive >= spec.consecutive_levels:
                stable = True
                break
    source_level = next(level for level in levels if level["paths"] == base.paths)
    source_checks = {}
    mode, horizon = base.response_mode, str(base.horizon_months)
    for metric in ("cumulative_fcf_musd", "cumulative_fcf_impact_musd", "cumulative_operating_income_impact_musd", "response_contribution_to_fcf_musd", "ending_cash_musd"):
        source_checks[metric] = all(round(source_level["replicas"][0]["policies"][mode][horizon][metric][q], 4) == reference["summary"][metric][q] for q in QUANTILES)
    source_checks["cash_floor_breach_path_fraction"] = source_level["replicas"][0]["policies"][mode][horizon]["cash_floor_breach"]["fraction"] == reference["summary"]["cash_floor_breach_path_fraction"]
    if not all(source_checks.values()):
        raise ValueError("Diagnostic paths differ from the displayed production run")
    manifest = {"model_version": VERSION, "monthly_model_version": MONTHLY_VERSION,
                "base_scenario": asdict(base), "convergence": {**asdict(spec), "path_counts": list(spec.path_counts)},
                "reference_run_id": reference["manifest"]["run_id"],
                "evidence_snapshot_id": digest(snapshot), "fuel_snapshot_id": digest(fuel),
                "fuel_last_observation": fuel[-1]["date"], "information_cutoff": base.information_cutoff,
                "python_version": platform.python_version(), "numpy_version": np.__version__,
                "code_sha256": hashlib.sha256(b"".join(Path(__file__).with_name(n).read_bytes() for n in CORE_FILES)).hexdigest()}
    manifest["run_id"] = digest(manifest)
    return {"model_version": VERSION, "manifest": manifest, "seeds": seeds, "levels": levels,
            "summary": {"status": "tolerance_met" if stable else "path_limit_reached",
                        "stopping_paths": levels[-1]["paths"], "displayed_paths": base.paths,
                        "displayed_level_passes": source_level["passes"],
                        "displayed_level_failed_checks": source_level["failed_checks"],
                        "evaluated_path_draws": generated,
                        "prefix_paths_scored": spec.replicates * sum(level["paths"] for level in levels),
                        "temporary_summary_bytes": summary_storage_bytes(base, levels[-1]["paths"], spec.replicates),
                        "peak_rollout_batch_paths": min(spec.batch_paths, levels[-1]["paths"]),
                        "reference_checks": source_checks, "runtime_seconds": round(perf_counter() - started, 3)},
            "replay_inputs": {"evidence_snapshot": snapshot, "fuel_observations": fuel},
            "methods": [
                "Each replica has its own recorded seed. Replica zero is the displayed run's seed; others use SeedSequence([replica, retry, root_seed, 271828]) reduced to the monthly seed range, with duplicate seeds rejected. Production PCG64 and world-shock law are unchanged.",
                "Increasing path counts append new paths to each replica's advancing PCG64 stream. Nested prefixes are rescored without regenerating earlier paths. Control, intervention and both pricing policies share world paths. Replica scatter is across distinct seeds, not across dependent prefixes or months.",
                "Monthly rollouts are computed in the recorded batch size. Only exact checkpoint outcome vectors are appended to private temporary binary files; quantiles read one complete vector at a time. No approximate quantile sketch is used. Temporary files are removed on completion or failure. Batching changes resources, not the world law or path outcomes.",
                "Quantiles use NumPy linear interpolation on pathwise accumulated flows, horizon-weighted margin and month-end/minimum cash. Horizons 1/3/6/12 and the selected endpoint are checked when inside the run.",
                "Single-run Monte Carlo standard error is sample SD of replica quantile estimates, without dividing by sqrt(replicates). The 1.96-times-SD half-width is an approximate pointwise diagnostic, not a calibrated or simultaneous forecast interval.",
                "Conditional cash-floor fractions also use the largest pointwise 95% Wilson distance from a replica estimate, so zero/all breaches retain a nonzero sampling bound when stochastic. With stochastic disabled there is no Monte Carlo error.",
                "A level passes only when every monitored pointwise half-width and paired-prefix RMS change meets its recorded absolute/relative tolerance. Stop after the requested consecutive passing levels, at or above displayed paths, or exhaust the path limit. These user-set stopping tolerances are not empirical forecast acceptance criteria.",
                "Replica means are averages of quantile estimates, not quantiles of a pooled or structural ensemble. No paired-impact quantile is formed by subtracting marginal quantiles.",
                "Fixed coefficients, data vintage, structural disagreement, parameter uncertainty and historical calibration are outside this numerical error check. Passing it does not establish forecast accuracy, investment value or a calibrated event probability."]}
