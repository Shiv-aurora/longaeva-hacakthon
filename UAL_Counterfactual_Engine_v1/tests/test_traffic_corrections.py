from copy import deepcopy
from pathlib import Path

import pytest

from app.traffic_corrections import detect_reuse, reconstruct_reviewed, reviewed_panel, scan_cohort
from app.operating_history import METRICS
from app.operating_vintages import reconstruct


def cohort(sha, period="2021-01", payload=("route", "seats", "passengers"), count=1):
    return {"source_sha256":sha, "source_member":"original.asc", "scope":"DD", "period":period,
        "rows":[{"source_line":i+1, "payload":list(payload)} for i in range(count)]}


def source(sha, scope="DD", released="2022-02-01", first="2021-01", last="2021-12"):
    from datetime import date
    stamp = date.fromisoformat(released).strftime("%d%b%Y").upper()
    return {"sha256":sha, "scope":scope, "published_on":released,
        "source_url":f"https://www.bts.gov/DB28SEG.{scope}.WAC.{first.replace('-','')}.{last.replace('-','')}.REL01.{stamp}.zip"}


def detection():
    ref, good, bad = cohort("r", "2020-01"), cohort("g", payload=("route", "lower seats", "lower passengers")), cohort("b")
    sources = [source("r", released="2021-03-01"),source("g",released="2022-01-04"),source("b")]
    return ref, good, bad, sources


def test_repair_requires_whole_literal_cohort_including_multiplicity():
    ref, good, bad, sources = detection()
    patches, review = detect_reuse([bad], {"DD":ref}, {"DD":good}, sources)
    assert list(patches) == ["b"] and patches["b"]["replacement_source_sha256"] == "g"
    changed = deepcopy(bad); changed["rows"][0]["payload"][-1] = "different"
    assert not detect_reuse([changed], {"DD":ref}, {"DD":good}, sources)[0]
    twice = cohort("b",count=2)
    assert not detect_reuse([twice], {"DD":ref}, {"DD":good}, sources)[0]


def test_empty_partial_or_prior_year_replacement_is_not_accepted():
    ref, good, bad, sources = detection()
    empty = deepcopy(bad);empty["rows"] = []
    assert not detect_reuse([empty], {"DD":ref}, {"DD":good}, sources)[0]
    with pytest.raises(ValueError, match="also repeats"):
        detect_reuse([bad], {"DD":ref}, {"DD":cohort("g")}, sources)
    sources[1]["published_on"] = "2022-03-01"
    with pytest.raises(ValueError, match="earlier released"):
        detect_reuse([bad], {"DD":ref}, {"DD":good}, sources)


def normalized(sha, carrier, rpm, scope, release, market="domestic"):
    return {"source_sha256":sha,"scope":scope,"published_on":release,"period":"2021-01",
        "carrier":carrier,"carrier_entity":"entity","carrier_wac":10,"market":market,
        **dict.fromkeys(METRICS,0),"rpm":rpm,"asm":1000,"segment_records":1}


def fixture():
    sources = [source("a",released="2022-01-04"),source("b"),source("c",scope="FD",released="2022-01-04")]
    normalized_rows = [normalized("a","AA",100,"DD","2022-01-04"),normalized("a","UA",200,"DD","2022-01-04"),
        normalized("b","AA",900,"DD","2022-02-01"),normalized("b","UA",220,"DD","2022-02-01"),
        normalized("c","AA",20,"FD","2022-01-04")]
    archives = [{"rows":[r for r in normalized_rows if r["source_sha256"] == s["sha256"]]} for s in sources]
    patches = {"b":{"scope":"DD","period":"2021-01","replacement_source_sha256":"a","replacement_published_on":"2022-01-04"}}
    return archives, sources, patches


def test_only_aa_cohort_replaced_and_peer_latest_records_preserved():
    archives, sources, patches = fixture()
    panel = reviewed_panel(archives,sources,"2022-02-28",patches,"2026-10-09")
    aa = next(r for r in panel["rows"] if r["carrier"] == "AA")
    ua = next(r for r in panel["rows"] if r["carrier"] == "UA")
    assert aa["rpm"] == 120 and aa["source_sha256s"] == ["a","c"]
    assert ua["rpm"] == 220 and ua["source_sha256s"] == ["b"]
    assert panel["corrections_applied"][0]["selected_release"]["source_sha256"] == "b"
    before = reviewed_panel(archives,sources,"2022-01-31",patches,"2026-10-09")
    assert before["corrections_applied"] == []
    assert len(panel["selected_releases"]) == 24 # both scopes, metadata-only months included


def test_future_replacement_and_wrong_scope_fail():
    archives, sources, patches = fixture()
    patches["b"]["replacement_published_on"] = "2022-03-01"
    with pytest.raises(ValueError, match="not available"):
        reviewed_panel(archives,sources,"2022-02-28",patches,"2026-10-09")
    patches["b"]["replacement_published_on"] = "2022-01-04";patches["b"]["scope"] = "FD"
    with pytest.raises(ValueError, match="not available"):
        reviewed_panel(archives,sources,"2022-02-28",patches,"2026-10-09")


def test_missing_other_scope_remains_unknown_and_replacement_not_imputed():
    archives, sources, patches = fixture()
    panel = reviewed_panel(archives[:2],sources[:2],"2022-02-28",patches,"2026-10-09")
    assert not any(r["both_archive_scopes_available"] for r in panel["rows"])
    with pytest.raises(ValueError, match="absent"):
        reviewed_panel([{"rows":[]},archives[1],archives[2]],sources,"2022-02-28",patches,"2026-10-09")


def test_reconstruction_preserves_native_history_and_labels_hindsight():
    archives, sources, patches = fixture()
    native = reconstruct(archives,sources,["2022-01-31","2022-02-28"],"2022-02-28")
    saved = deepcopy(native)
    result = reconstruct_reviewed(archives,sources,native,patches,"2026-10-09","2022-02-28")
    assert native == saved
    assert all("hindsight" in r["knowledge_label"] and r["review_known_on"] == "2026-10-09" for r in result["observation_versions"].values())
    aa = next(r for r in result["evaluation_targets"]["observations"] if r["carrier"] == "AA")
    assert aa["rpm_million"] == 120/1e6
    assert aa["scope_releases"][0]["source_sha256"] == "b"
    assert aa["source_sha256s"] == ["a","c"]
    assert aa["published_on"] == "2022-02-01" # original complete-scope gate retained


def test_scanner_rejects_locked_scope_before_reading_source():
    with pytest.raises(ValueError, match="locked quantities"):
        scan_cohort((source("does-not-exist"),"2024-01",str(Path('/does-not-exist'))))
