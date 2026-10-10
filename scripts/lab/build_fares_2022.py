"""Build the 2022 daily fare case study: Expedia quote archive -> matched daily index -> page JSON.

Stages (each reads only what the one before wrote):

  register  hash the downloaded archive and write its manifest entry (source, licence, sha256, bytes)
  extract   stream itineraries.csv out of the zip (never unpacked to disk) into a 13-column Parquet file;
            about 6 minutes, and duckdb's temporary directory peaks near 22 GB while the pipe is read
  index     profile the quotes and write data/lab/fares_daily_2022.csv
  publish   fares_daily_2022.csv + fuel_daily.csv (+ tsa_daily.csv when present) -> app/static/data/daily_fares_2022.json

`publish` needs only tracked files, so the page JSON can be rebuilt without the
archive. The archive is untrusted third-party data: run this script with
`python -I`, from outside data/lab_raw/fares2022/. No network.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.lab import fares_2022 as fx

SOURCE = {
    "source": "kaggle:dilwong/flightprices",
    "url": "https://www.kaggle.com/datasets/dilwong/flightprices",
    "download_url": "https://storage.googleapis.com/kaggle-data-sets/2560255/4349289/bundle/archive.zip",
    "download_note": "Signed link issued by the Kaggle API (download_dataset) for this account; the signature is not "
                     "stored. Fetched with curl in one stream.",
    "dataset_version": 1,
    "dataset_last_updated": "2022-10-18T22:40:44.053Z",
    "title": "Flight Prices",
    "subtitle": "One-way flights found on Expedia between 2022-04-16 and 2022-10-05",
    "creator": "Dillon Wong",
    "licence": "Attribution 4.0 International (CC BY 4.0)",
    "licence_id": "CC-BY-4.0",
    "licence_url": "https://creativecommons.org/licenses/by/4.0/",
    "licence_source": "licence name as returned by the Kaggle API (get_dataset_info and get_dataset_metadata) on the "
                      "retrieval date",
    "attribution": "Dillon Wong, 'Flight Prices', Kaggle dataset dilwong/flightprices, version 1 (2022-10-18), "
                   "licensed CC BY 4.0. The uploader states the source as 'Data from Expedia'.",
    "terms_note": "The uploader's licence is CC BY 4.0. The quotes were collected from Expedia by the uploader; "
                  "Expedia's own terms for its site are a separate matter this project has not cleared. Research use; "
                  "only aggregated index values are redistributed here, never quote rows.",
    "members": [{"name": "itineraries.csv", "bytes": 31091834438}],
}


def relative(path: Path) -> str:
    path = Path(path).resolve()
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=1, allow_nan=False) + "\n")
    os.replace(partial, path)


def read_manifest(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"No manifest at {path}; run the register stage first")
    return json.loads(path.read_text())


def connect(memory: str, threads: int, temp: Path):
    import duckdb
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit={fx._lit(memory)}")
    con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET temp_directory={fx._lit(temp)}")
    return con


def register(args) -> dict:
    archive = args.archive
    with zipfile.ZipFile(archive) as bundle:
        members = [{"name": info.filename, "bytes": info.file_size} for info in bundle.infolist()]
    if members != SOURCE["members"]:
        raise SystemExit(f"Archive members differ from the Kaggle file list: {members}")
    entry = {**SOURCE, "retrieved_at": args.retrieved_at, "sha256": fx.file_sha256(archive),
             "bytes": archive.stat().st_size, "path": relative(archive),
             "header": fx.read_header(archive, fx.MEMBER)}
    manifest = json.loads(args.manifest.read_text()) if args.manifest.exists() else {}
    if manifest.get("entries", [{}])[0].get("sha256") != entry["sha256"]:
        manifest = {}   # a different archive: everything derived from the old one is dropped
    manifest = {"area": "fares_2022", "builder": "scripts/lab/build_fares_2022.py", "raw_dir": relative(archive.parent),
                "raw_note": "The archive is kept byte-exact and is never unpacked to disk: itineraries.csv (31 GB) is "
                            "streamed out of it with `unzip -p` into duckdb, which buffers the pipe in its temporary "
                            "directory under raw_dir (about 22 GB at the peak, removed when the stage ends). Files "
                            "under raw_dir are untracked.",
                "entries": [entry], **{k: v for k, v in manifest.items() if k in ("derived", "profile", "index", "outputs")}}
    write_json(args.manifest, manifest)
    return {"manifest": relative(args.manifest), "sha256": entry["sha256"], "bytes": entry["bytes"]}


def extract(args) -> dict:
    manifest = read_manifest(args.manifest)
    entry = manifest["entries"][0]
    if args.archive.stat().st_size != entry["bytes"]:
        raise SystemExit("Archive size differs from the manifest; register it again")
    header = fx.read_header(args.archive, fx.MEMBER)
    # The CSV is streamed: unzip writes the member to a pipe that becomes this process's stdin.
    unzip = subprocess.Popen(["/usr/bin/unzip", "-p", str(args.archive), fx.MEMBER], stdout=subprocess.PIPE)
    os.dup2(unzip.stdout.fileno(), 0)
    unzip.stdout.close()
    con = connect(args.memory, args.threads, args.slim.parent / "duckdb_tmp")
    started = datetime.now(timezone.utc)
    result = fx.extract_slim(con, "/dev/stdin", header, args.slim)
    code = unzip.wait()
    if code != 0:
        args.slim.unlink(missing_ok=True)
        raise SystemExit(f"unzip exited with {code}; the extract is incomplete and was removed")
    result.update({"path": relative(args.slim), "bytes": args.slim.stat().st_size, "sha256": fx.file_sha256(args.slim),
                   "from": entry["sha256"], "seconds": round((datetime.now(timezone.utc) - started).total_seconds()),
                   "note": "Every row of itineraries.csv, 13 of its 27 columns, cast with try_cast. Not byte-reproducible: "
                           "Parquet row-group layout depends on the duckdb version."})
    manifest["derived"] = [result]
    write_json(args.manifest, manifest)
    return result


def index(args) -> dict:
    manifest = read_manifest(args.manifest)
    if not manifest.get("derived") or fx.file_sha256(args.slim) != manifest["derived"][0]["sha256"]:
        raise SystemExit("The slim Parquet file is missing from the manifest or has changed; run the extract stage")
    con = connect(args.memory, args.threads, args.slim.parent / "duckdb_tmp")
    quotes = fx.quotes_sql(args.slim)
    manifest["profile"] = fx.profile(con, quotes)
    built = fx.build_index(con, quotes)
    written = fx.write_fares_csv(args.fares, built["rows"])
    manifest["index"] = {
        "spec": built["spec"], "primary": fx.PRIMARY,
        "variants": {name: text for name, (_, _, text) in fx.VARIANTS.items()},
        "elementary_prices": built["elementary_prices"],
        "search_days_with_an_elementary_price": len(built["search_days"]),
        "cell_counts": built["cell_counts"],
        "cell_counts_note": "Keyed carrier|lead_bucket. cells_seen: cells that count on at least one search day. "
                            "cells_kept: cells that meet min_coverage and enter the index."}
    manifest["outputs"] = [{"path": relative(args.fares), **written, "columns": list(fx.FARES_HEADER),
                            "note": "index = 100 on the first search day with a matched cell. n_cells: cells in that day's "
                                    "link. n_quotes: quote rows behind those cells that day. carrier ALL = every carrier "
                                    "with a kept cell; lead_bucket all = every bucket. Filter variant == "
                                    f"'{fx.PRIMARY}' for the primary index."}]
    write_json(args.manifest, manifest)
    return {"fares": relative(args.fares), **written, "rows_in_source": manifest["profile"]["rows"],
            "cells": {v: built["cell_counts"][v][f"{fx.ALL}|{fx.ALL_BUCKETS}"] for v in built["cell_counts"]}}


def publish(args) -> dict:
    generated_at = args.generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    document = fx.build_document(args.fares, args.fuel, args.tsa if args.tsa.exists() else None, args.manifest,
                                 generated_at, args.seed, args.n_boot)
    problems = fx.validate(document)
    if problems:
        raise SystemExit("daily_fares_2022.json does not conform: " + "; ".join(problems))
    write_json(args.out, document)
    return {"out": relative(args.out), "as_of": document["as_of"], "n_days": document["n_days"],
            "n_quotes": document["n_quotes"], "headline": document["headline"], "plain_summary": document["plain_summary"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stage", choices=("register", "extract", "index", "publish"))
    parser.add_argument("--archive", type=Path, default=fx.ARCHIVE)
    parser.add_argument("--slim", type=Path, default=fx.SLIM)
    parser.add_argument("--manifest", type=Path, default=fx.MANIFEST)
    parser.add_argument("--fares", type=Path, default=fx.FARES_CSV)
    parser.add_argument("--fuel", type=Path, default=fx.FUEL_CSV)
    parser.add_argument("--tsa", type=Path, default=fx.TSA_CSV)
    parser.add_argument("--out", type=Path, default=fx.OUT)
    parser.add_argument("--memory", default="8GB")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--seed", type=int, default=fx.DEFAULT_SEED)
    parser.add_argument("--n-boot", type=int, default=2000)
    parser.add_argument("--retrieved-at", help="UTC ISO time the archive was downloaded (register)")
    parser.add_argument("--generated-at", help="UTC ISO timestamp to record (default: now)")
    args = parser.parse_args()
    stage = {"register": register, "extract": extract, "index": index, "publish": publish}[args.stage]
    print(json.dumps(stage(args), indent=2))


if __name__ == "__main__":
    main()
