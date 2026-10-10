"""Measure one exact verified-study API case; never aggregate across workloads."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.convergence import ConvergenceSpec
from app.model_archive import capture_source, deterministic, digest, environment, read_verified_study
from app.monthly import MonthlyScenario


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100 or not 1 <= args.port <= 65535:
        raise ValueError("Use 5–100 measured requests and a valid local port")
    source = json.loads(args.run.read_text())
    controls = ConvergenceSpec(**json.loads(args.spec.read_text()))
    controls.validate(MonthlyScenario(**source["scenario"]))
    code = capture_source("convergence").code_sha256
    cached = read_verified_study(source["manifest"]["run_id"], asdict(controls), code)
    if cached is None:
        raise ValueError("Completely replay and register this exact study before benchmarking")
    expected = digest(deterministic(cached))
    url = f"http://127.0.0.1:{args.port}/api/monthly/convergence"
    payload = json.dumps({"run_record": source, "convergence": asdict(controls)}, allow_nan=False).encode()
    durations = []
    response_bytes = []
    for index in range(args.samples + 1):
        request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        started = time.perf_counter()
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            elapsed = time.perf_counter() - started
            if response.headers.get("X-UAL-Study-Cache") != "verified":
                raise ValueError("Benchmark received a freshly computed study instead of verified reuse")
        result = json.loads(raw)
        if digest(deterministic(result)) != expected or result["replay_code"] != cached["replay_code"]:
            raise ValueError("Benchmark response differs from the verified study")
        if index:  # One explicitly excluded warm-up request.
            durations.append(elapsed)
            response_bytes.append(len(raw))
    report = {"schema": "study-cache-benchmark-0.1.0", "recorded_at": datetime.now(timezone.utc).isoformat(),
              "url": url, "reference_run_id": source["manifest"]["run_id"],
              "study_id": cached["manifest"]["run_id"], "study_result_sha256": expected,
              "controls": asdict(controls), "code_sha256": code, "environment": environment(),
              "benchmark_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "request_bytes": len(payload), "response_bytes": response_bytes,
              "excluded_warmup_requests": 1, "measured_requests": args.samples,
              "transport_elapsed_seconds": durations,
              "percentile_method": "NumPy linear interpolation over measured requests",
              "latency_seconds": {name: float(np.quantile(durations, value))
                  for name, value in (("p50", .5), ("p90", .9), ("p95", .95))},
              "timing_boundary": "Before HTTP POST through complete response-byte read; excludes client JSON decoding and validation",
              "scope": "Sequential warm local API requests for this exact scenario/spec, including complete monthly-reference replay and all cache/code checks. No concurrent load, cold-start, unseen-scenario, infrastructure-cost or analyst-usability claim."}
    target = ROOT / "data" / "performance" / (digest(report) + ".json")
    target.parent.mkdir(exist_ok=True)
    with target.open("x") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"benchmark_file": str(target), **report}, indent=2))


if __name__ == "__main__":
    main()
