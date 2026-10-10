"""Build the reviewed anchors into an immutable DuckDB/Parquet evidence snapshot."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.evidence import build_warehouse, load_snapshot

if __name__ == "__main__":
    print(json.dumps(build_warehouse(load_snapshot()), indent=2))
