"""Audit a study with independent scalar statistics and production-path witnesses.

No convergence-module functions are imported. Registered replay checks every result;
this audit independently checks the numerical-error calculation and stopping rule.
Two declared seeds at every count are also aggregated from production paths using
Python sorting/interpolation, separately from the diagnostic quantile routine.
"""
from dataclasses import replace
from decimal import Decimal, localcontext
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.model_archive import capture_source, deterministic, digest, replay_saved
from app.monthly import MonthlyScenario, _rollout, _world, baseline, month_sequence, shock_weights
from app.fleet import capacity_paths
from app.data import fuel_volatility

POLICIES = ("frozen", "assumed_constrained")
METRICS = ("cumulative_fcf_musd", "cumulative_fcf_impact_musd", "cumulative_operating_income_impact_musd",
           "ending_cash_musd", "minimum_cash_musd", "cumulative_revenue_musd",
           "response_contribution_to_fcf_musd", "cumulative_operating_margin_pct")
QUANTILES = {"p10": .1, "p50": .5, "p90": .9}
Z = Decimal("1.959963984540054")


def interval(k, n):
    with localcontext() as context:
        context.prec = 45
        p, n = Decimal(k) / n, Decimal(n)
        denominator = 1 + Z * Z / n
        center = (p + Z * Z / (2 * n)) / denominator
        radius = Z * (p * (1 - p) / n + Z * Z / (4 * n * n)).sqrt() / denominator
        return float(max(0, center - radius)), float(min(1, center + radius))


def percentile(sorted_values, q):
    index = (len(sorted_values) - 1) * q
    i = math.floor(index)
    fraction = index - i
    return sorted_values[i] if fraction == 0 else sorted_values[i] + fraction * (sorted_values[i + 1] - sorted_values[i])


def audit(saved, production_witnesses=True, progress=None):
    base = MonthlyScenario(**saved["manifest"]["base_scenario"])
    controls = saved["manifest"]["convergence"]
    levels, seeds = saved["levels"], saved["seeds"]
    checks = 0

    def check(condition, name):
        nonlocal checks
        if not condition:
            raise ValueError("Independent convergence audit failed: " + name)
        checks += 1

    def close(a, b, name):
        check(math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8), name)

    expected_horizons = sorted({h for h in (1, 3, 6, 12, base.horizon_months) if h <= base.horizon_months})
    check(len(seeds) == controls["replicates"] and len(set(seeds)) == len(seeds) and seeds[0] == base.seed, "distinct replica seeds and original seed")
    check([level["paths"] for level in levels] == controls["path_counts"][:len(levels)], "ordered evaluated path counts")
    previous, streak, stopped = None, 0, False
    for level in levels:
        n = level["paths"]
        replicas = level["replicas"]
        check(len(replicas) == len(seeds), "replica count")
        for replica, seed in zip(replicas, seeds):
            check(replica["seed"] == seed and replica["paths"] == n, "replica seed/count")
            for mode in POLICIES:
                check(sorted(map(int, replica["policies"][mode])) == expected_horizons, "supported horizons")
                for h in expected_horizons:
                    values = replica["policies"][mode][str(h)]
                    check(set(values) == {*METRICS, "cash_floor_breach", "path_outcomes_sha256"}, "complete outcome fields")
                    for metric in METRICS:
                        check(set(values[metric]) == set(QUANTILES), "quantile fields")
                        check(values[metric]["p10"] <= values[metric]["p50"] <= values[metric]["p90"], "ordered quantiles")
                    fraction = values["cash_floor_breach"]
                    k = fraction["count"]
                    check(isinstance(k, int) and not isinstance(k, bool) and 0 <= k <= n, "breach count")
                    close(fraction["fraction"], k / n, "breach fraction")
                    low, high = interval(k, n)
                    close(fraction["wilson95_low"], low, "Decimal Wilson lower")
                    close(fraction["wilson95_high"], high, "Decimal Wilson upper")
        check(len(level["diagnostics"]) == len(POLICIES) * len(expected_horizons) * 25, "all quantile/fraction checks")
        keys = {(row["policy"], row["horizon_months"], row["metric"], row["statistic"]) for row in level["diagnostics"]}
        check(len(keys) == len(level["diagnostics"]), "unique diagnostic keys")
        outcomes = []
        for row in level["diagnostics"]:
            mode, h, metric, statistic = row["policy"], str(row["horizon_months"]), row["metric"], row["statistic"]
            check(mode in POLICIES and int(h) in expected_horizons and metric in (*METRICS, "cash_floor_breach"), "diagnostic key boundary")
            if metric == "cash_floor_breach":
                check(statistic == "fraction", "fraction statistic")
                fractions = [r["policies"][mode][h][metric] for r in replicas]
                values = [x["fraction"] for x in fractions]
                sample_bound = max(max(x["fraction"] - interval(x["count"], n)[0], interval(x["count"], n)[1] - x["fraction"]) for x in fractions) if base.stochastic else 0.
                width = max(sample_bound, float(Z) * statistics.stdev(values))
                tolerance = controls["breach_tolerance_fraction"]
            else:
                check(statistic in QUANTILES, "quantile statistic")
                values = [r["policies"][mode][h][metric][statistic] for r in replicas]
                width = float(Z) * statistics.stdev(values)
                absolute = controls["margin_tolerance_pp"] if metric.endswith("_pct") else controls["absolute_tolerance_musd"]
                tolerance = max(absolute, controls["relative_tolerance"] * abs(statistics.mean(values)))
            close(row["selected_seed_estimate"], values[0], "selected seed estimate")
            close(row["replicate_mean"], statistics.mean(values), "replica mean")
            close(row["mc_standard_error_single_run"], statistics.stdev(values), "single-run sample SD without sqrt(repeats)")
            close(row["approximate_mc_half_width95"], width, "pointwise numerical half-width")
            close(row["replicate_min"], min(values), "replica minimum")
            close(row["replicate_max"], max(values), "replica maximum")
            close(row["tolerance"], tolerance, "absolute/relative tolerance")
            if previous is None:
                check(row["paired_rms_change"] is None, "first count lacks change comparison")
                passing = False
            else:
                if metric == "cash_floor_breach":
                    old = [r["policies"][mode][h][metric]["fraction"] for r in previous]
                else:
                    old = [r["policies"][mode][h][metric][statistic] for r in previous]
                change = math.sqrt(statistics.mean((a - b) ** 2 for a, b in zip(values, old)))
                close(row["paired_rms_change"], change, "paired-prefix RMS change")
                passing = max(width, change) <= tolerance
            check(row["passes"] is passing, "individual gate")
            outcomes.append(passing)
        passing = all(outcomes)
        streak = streak + 1 if passing else 0
        check(level["passes"] is passing and level["failed_checks"] == outcomes.count(False), "level result and failures")
        check(level["consecutive_passing_levels"] == streak, "consecutive-count stopping")
        check(not stopped, "no extra evaluated level after stopping")
        stopped = n >= base.paths and streak >= controls["consecutive_levels"]
        previous = replicas
    summary = saved["summary"]
    check(summary["status"] == ("tolerance_met" if stopped else "path_limit_reached"), "honest stopping status")
    check(stopped or len(levels) == len(controls["path_counts"]), "limit actually exhausted")
    check(summary["stopping_paths"] == levels[-1]["paths"], "last evaluated count")
    incremental = saved["model_version"] == "monthly-convergence-0.2.0"
    check(incremental or saved["model_version"] == "monthly-convergence-0.1.0", "known numerical protocol")
    draws = len(seeds) * (levels[-1]["paths"] if incremental else sum(x["paths"] for x in levels))
    check(summary["evaluated_path_draws"] == draws, "total numerical work")
    if incremental:
        check(summary["prefix_paths_scored"] == len(seeds) * sum(x["paths"] for x in levels), "rescored prefix work")
        check(summary["temporary_summary_bytes"] == len(seeds) * levels[-1]["paths"] * len(POLICIES) * len(expected_horizons) * len(METRICS) * 8, "temporary outcome-vector bytes")
        check(summary["peak_rollout_batch_paths"] == min(controls["batch_paths"], levels[-1]["paths"]), "maximum batch size")
    source = next(x for x in levels if x["paths"] == base.paths)
    check(summary["displayed_level_passes"] is source["passes"] and summary["displayed_level_failed_checks"] == source["failed_checks"], "displayed-run result distinct from stopping result")
    if production_witnesses:
        # Separate aggregation witnesses: original seed plus the next declared seed
        # at every count, every monitored horizon and both policies.
        inputs = saved["replay_inputs"]
        b = baseline(inputs["evidence_snapshot"], base.information_cutoff, base.cash_plan, base.purchase_plan)
        fuel = inputs["fuel_observations"]
        spot, sigma = fuel[-1]["usd_per_gallon"], fuel_volatility(fuel) * math.sqrt(21)
        fleet = capacity_paths(b, base, b["fleet_plan"], month_sequence(base.information_cutoff, base.horizon_months), shock_weights(base))
        for level in levels:
            for replica in level["replicas"][:2]:
                audit_path_witness(base, b, fleet, spot, sigma, replica, expected_horizons, check, close)
                if progress is not None:
                    progress({"phase": "path_witness_complete", "level_paths": level["paths"], "seed": replica["seed"]})
    return {"checks_passed": checks, "production_witness_seeds_per_level": 2 if production_witnesses else 0,
            "study_id": saved["manifest"]["run_id"], "source_run_id": saved["manifest"]["reference_run_id"],
            "stopping_status": summary["status"], "displayed_level_failed_checks": source["failed_checks"],
            "scope": "Independent scalar sampling statistics/stopping audit and selected production-path aggregation; not independent forecast validation"}


def audit_path_witness(base, b, fleet, spot, sigma, replica, horizons, check, close):
    """Use a separately chosen 4093-path batch size and scalar quantile method."""
    n, seed = replica["paths"], replica["seed"]
    rng = np.random.default_rng(seed)
    with tempfile.TemporaryDirectory(prefix="ual-independent-quantiles-") as temporary:
        folder = Path(temporary)
        for start in range(0, n, 4093):
            scenario = replace(base, paths=min(4093, n - start), seed=seed)
            worlds = _world(scenario, spot, sigma, rng=rng)
            raw = {mode: {on: _rollout(b, scenario, *worlds, spot, on, mode, fleet) for on in (False, True)} for mode in POLICIES}
            delta = {mode: raw[mode][True]["free_cash_flow_musd"] - raw[mode][False]["free_cash_flow_musd"] for mode in POLICIES}
            for mode in POLICIES:
                shocked, control = raw[mode][True], raw[mode][False]
                revenue = shocked["revenue_musd"].cumsum(axis=1)
                income = shocked["operating_income_musd"].cumsum(axis=1)
                vectors = {
                    "cumulative_fcf_musd": shocked["free_cash_flow_musd"].cumsum(axis=1),
                    "cumulative_fcf_impact_musd": delta[mode].cumsum(axis=1),
                    "cumulative_operating_income_impact_musd": (shocked["operating_income_musd"] - control["operating_income_musd"]).cumsum(axis=1),
                    "ending_cash_musd": shocked["cash_musd"],
                    "minimum_cash_musd": np.minimum.accumulate(shocked["cash_musd"], axis=1),
                    "cumulative_revenue_musd": revenue,
                    "response_contribution_to_fcf_musd": (delta["assumed_constrained"] - delta["frozen"]).cumsum(axis=1) if mode == "assumed_constrained" else np.zeros_like(revenue),
                    "cumulative_operating_margin_pct": np.divide(100 * income, revenue, out=np.zeros_like(income), where=revenue > 0)}
                for h in horizons:
                    for metric in METRICS:
                        with (folder / f"{mode}-{h}-{metric}.raw").open("ab") as stream:
                            stream.write(np.asarray(vectors[metric][:, h - 1], dtype="<f8").tobytes())
            del raw, delta, vectors, worlds, shocked, control, revenue, income
        for mode in POLICIES:
            for h in horizons:
                values = replica["policies"][mode][str(h)]
                path_hash = hashlib.sha256()
                for metric in METRICS:
                    path = folder / f"{mode}-{h}-{metric}.raw"
                    check(path.stat().st_size == n * 8, "independent witness byte count")
                    vector = np.fromfile(path, dtype="<f8")
                    path_hash.update(vector.tobytes())
                    ordered = sorted(float(v) for v in vector)
                    for q, proportion in QUANTILES.items():
                        close(values[metric][q], percentile(ordered, proportion), "Python-sort production-path quantile")
                    if metric == "minimum_cash_musd":
                        count = int(np.count_nonzero(vector < base.cash_floor_musd))
                        check(count == values["cash_floor_breach"]["count"], "production minimum-cash breach count")
                    del vector, ordered
                check(path_hash.hexdigest() == values["path_outcomes_sha256"], "complete paired-outcome witness hash")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/audit_convergence.py STUDY.json")
    path = Path(sys.argv[1])
    saved = json.loads(path.read_text())
    if saved["manifest"]["code_sha256"] != capture_source("convergence").code_sha256:
        raise ValueError("Independent witnesses require the matching current production core")
    from app.model_archive import replay_and_register_study
    replayed = replay_and_register_study(path)
    if deterministic(saved) != deterministic(replayed):
        raise ValueError("Registered replay differs")
    report = {**audit(saved, progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True)), "audit_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "study_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    identity = digest(report)
    folder = ROOT / "data" / "convergence_checks"
    folder.mkdir(exist_ok=True)
    target = folder / (identity + ".json")
    content = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if target.exists():
        if target.read_text() != content:
            raise ValueError("Existing independent audit differs")
    else:
        with target.open("x") as stream:
            stream.write(content)
    print(json.dumps({"audit_file": str(target), **report}, indent=2))


if __name__ == "__main__":
    main()
