"""Archive current source and inventory/optionally replay existing saved runs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.model_archive import ARCHIVE_ROOT, archive_source, capture_source, digest, replay_saved, verify_bundle


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-runs", action="store_true", help="Replay available versions; report unavailable older source honestly")
    args = parser.parse_args()
    bundles = [archive_source(capture_source(kind)) for kind in ("monthly", "failure_search", "target_inversion")]
    records = []
    for kind, directory in (("monthly", "runs"), ("failure_search", "failure_searches"), ("target_inversion", "target_inversions")):
        for path in sorted((ROOT / "data" / directory).glob("*.json")):
            saved = json.loads(path.read_text())
            sha = saved["manifest"]["code_sha256"]
            matches = [p.stem for p in (ARCHIVE_ROOT / ".registered").glob("*.json")
                if (d := json.loads(p.read_text())).get("kind") == kind and d.get("code_sha256") == sha]
            record = {"file": path.relative_to(ROOT).as_posix(), "run_id": saved["manifest"]["run_id"],
                "kind": kind, "code_sha256": sha,
                "status": "registered_source_available" if matches else "original_source_unavailable"}
            if matches:
                verify_bundle(sorted(matches)[0], kind)
                record["bundle_id"] = sorted(matches)[0]
                if args.verify_runs:
                    replay_saved(path, kind)
                    record["status"] = "full_deterministic_replay_verified"
            records.append(record)
    report = {"schema": "model-code-inventory-0.1.0", "current_bundles": bundles, "saved_runs": records,
              "verified": args.verify_runs, "scope": "Executable model replay; no forecast or investment validation"}
    report_id = digest(report)
    target = ROOT / "data" / "model_code_checks" / (report_id + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if json.loads(target.read_text()) != report: raise ValueError("Immutable archive inventory differs")
    else:
        with target.open("x") as stream: json.dump(report, stream, indent=2); stream.write("\n")
    print(json.dumps({"inventory": str(target), "saved_runs": records}, indent=2))


if __name__ == "__main__":
    main()
