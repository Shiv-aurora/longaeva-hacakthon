"""Evidence ledger and meta tests.

They read the committed outputs and tidy inputs. The rebuild and tamper tests also
need the raw filings under data/lab_raw and skip where those are absent. No network.
"""
import copy
import csv
import hashlib
import json
from pathlib import Path
import sys

import pytest

from app.lab import evidence as ev
from scripts.lab import build_meta_evidence

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "app" / "static" / "data"
RAW, DOC = "data/lab_raw/sec", "docs/lab_contract.md"


def load(name: str, folder: Path = DATA) -> dict:
    return json.loads((folder / name).read_text())


@pytest.fixture(scope="module")
def ledger():
    return load("evidence.json")


@pytest.fixture(scope="module")
def meta():
    return load("meta.json")


@pytest.fixture(scope="module")
def by_id(ledger):
    return {item["id"]: item for item in ledger["items"]}


# ------------------------------------------------------------------ committed files

def test_committed_files_are_well_formed_and_hold_no_local_paths(ledger, meta):
    ev.validate(ledger, meta)
    for name in ("evidence.json", "meta.json"):
        text = (DATA / name).read_text()
        assert str(ROOT.parent) not in text and "/Users/" not in text and "/private/" not in text and "file://" not in text
        assert '"fixture"' not in text


def test_every_id_meta_cites_is_in_the_ledger(meta, by_id):
    cited = ev.cited_ids(meta)
    assert set(cited) <= set(by_id)
    for block in [meta["guidance"], meta["report_date_source"], *meta["promises"]]:
        assert block["evidence_ids"] and set(block["evidence_ids"]) <= set(cited)
    reported = [promise["management_reported"] for promise in meta["promises"] if "management_reported" in promise]
    assert len(reported) == 1 and reported[0]["evidence_ids"][0] in cited


def test_files_keep_the_fixture_shape(ledger, meta):
    fixture, fixture_ledger = load("meta.json", DATA / "fixtures"), load("evidence.json", DATA / "fixtures")
    assert set(fixture) - {"fixture"} <= set(meta)
    assert set(fixture["guidance"]) == set(meta["guidance"])
    assert all(set(fixture["promises"][0]) <= set(promise) for promise in meta["promises"])
    for name, comparator in fixture["comparators"].items():
        assert set(comparator) <= set(meta["comparators"][name])
    assert all(set(fixture_ledger["items"][0]) <= set(item) for item in ledger["items"])
    assert set(fixture_ledger) - {"fixture"} <= set(ledger)


def test_meta_numbers_are_the_cited_entries(meta, by_id):
    guidance = meta["guidance"]
    eps, fuel = (by_id[name] for name in guidance["evidence_ids"])
    assert (eps["claim"], eps["period"], eps["value"]) == ("adj_eps_guidance", guidance["period"],
                                                           [guidance["eps_low"], guidance["eps_high"]])
    assert (fuel["claim"], fuel["period"], fuel["value"]) == ("fuel_assumed_usd_gal", guidance["period"],
                                                              guidance["fuel_assumed_usd_gal"])
    assert eps["doc_date"] == fuel["doc_date"] == guidance["made_on"] and eps["verified"] and fuel["verified"]
    for promise in meta["promises"]:
        assert promise["outcome"] is None
        for name in promise["evidence_ids"]:
            entry = by_id[name]
            value = entry["value"] if isinstance(entry["value"], list) else [entry["value"]] * 2
            assert value == [promise["recovery_low"], promise["recovery_high"]] and entry["verified"]
            assert (entry["claim"], entry["period"], entry["doc_date"]) == ("fuel_recovery_share", promise["period"],
                                                                            promise["made_on"])
    first = meta["promises"][0]  # The page reads the first promise as the one for the guided quarter.
    assert first["period"] == guidance["period"] and first["made_on"] == max(p["made_on"] for p in meta["promises"])
    reported = next(promise for promise in meta["promises"] if "management_reported" in promise)
    stated = by_id[reported["management_reported"]["evidence_ids"][0]]
    assert (stated["claim"], stated["period"]) == ("fuel_recovery_share_reported", reported["period"])
    assert stated["value"] == reported["management_reported"]["value"] and stated["verified"]
    assert reported["management_reported"]["approximate"] is ("approximately" in stated["quote"]) is True
    assert by_id[meta["report_date_source"]["evidence_ids"][0]]["value"] == meta["report_date"]


def test_verified_means_checked_against_document_text(ledger):
    items = ledger["items"]
    assert ledger["counts"] == {"items": len(items), "verified": sum(i["verified"] for i in items),
                                **{basis: sum(i["basis"] == basis for i in items) for basis in ev.BASES}}
    for item in items:
        assert item["verified"] is (item["basis"] == "document_text")
        if item["verified"]:
            assert item["normalisation"] == "lab-reader-text-v1" and item["note"] is None
            assert ev.check_quote({**item, "char_start": 0, "char_end": len(item["quote"])}, item["quote"]) == [] \
                or item["source"] == ev.GUIDANCE  # Guidance values are parsed by app.lab.verify, with table headers.
        else:
            assert item["note"] and item["doc_sha256"] is None and item["char_start"] is None
    paraphrase = next(i for i in items if i["basis"] == "paraphrase")
    assert paraphrase["quote"] is None and paraphrase["value"] is None and "paraphrase" in paraphrase["note"]
    assert {i["picked_by"] for i in items} == {"rules", "literature", "web_lookup"}


def test_inputs_carry_the_hash_of_each_source_read(ledger, meta):
    assert ledger["inputs"] == meta["inputs"]
    paths = [entry["path"] for entry in ledger["inputs"]]
    assert {ev.GUIDANCE, ev.LITERATURE, ev.DAL_CHECK, ev.QUARTERLY, ev.QUARTERLY_SOURCES, ev.INDEX} <= set(paths)
    for entry in ledger["inputs"]:
        path = ROOT / entry["path"]
        assert not Path(entry["path"]).is_absolute() and (path.exists() or entry["path"].startswith("data/lab_raw/"))
        if path.exists():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"], entry["path"]
    assert {item["doc_sha256"] for item in ledger["items"] if item["verified"]} <= {e["sha256"] for e in ledger["inputs"]}


def test_ids_cited_by_other_built_files_are_in_the_ledger(by_id):
    """The page shows a source only for an id the ledger holds, so a built file must not cite one it lacks."""
    for path in sorted(DATA.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
        except ValueError:
            continue  # Another builder is part-way through writing it.
        missing = [name for name in ev.cited_ids(payload) if name not in by_id]
        assert not missing, f"{path.name} cites ids that are not in evidence.json: {missing}"


# ------------------------------------------------------------------ validation

def test_validate_rejects_broken_files(ledger, meta):
    def broken(change):
        files = copy.deepcopy((ledger, meta))
        change(*files)
        with pytest.raises(ValueError):
            ev.validate(*files)

    broken(lambda l, m: m["guidance"]["evidence_ids"].append("ual-2026q2-ex992-not-there"))
    broken(lambda l, m: l["items"].append(copy.deepcopy(l["items"][0])))
    broken(lambda l, m: l["items"][0].update(char_start=None))
    broken(lambda l, m: l["items"][-1].update(verified=True))
    broken(lambda l, m: l["items"][-1].update(note=None))
    broken(lambda l, m: m["guidance"].update(eps_high=9.9))
    broken(lambda l, m: m["promises"][0].update(outcome=0.85))
    broken(lambda l, m: m["comparators"]["consensus_eps"].update(value=None))
    broken(lambda l, m: m["comparators"]["market_odds_beat"].pop("source_url"))
    broken(lambda l, m: m["inputs"][0].update(path=str(ROOT / ev.GUIDANCE)))
    broken(lambda l, m: l.update(fixture=True))
    unconfirmed = copy.deepcopy(meta)
    unconfirmed["comparators"]["consensus_eps"] = {"value": None, "reason": "no page confirmed it"}
    ev.validate(ledger, unconfirmed)


# ------------------------------------------------------------------ the check

TEXT = ("Outlook\nFuel\t\t$\t4.19\t\t$\t2.34\nTax\t\t(363)\nA benefit of 5-cents per gallon.\n"
        "It recovered approximately half, then 80% to 90% of $50 million.")


def record(quote, value, unit, shift=0):
    start = TEXT.index(quote) + shift
    return {"quote": quote, "value": value, "unit": unit, "char_start": start, "char_end": start + len(quote)}


def test_check_quote_applies_the_contract_rule():
    assert ev.check_quote(record("Fuel\t\t$\t4.19\t\t$\t2.34", 4.19, "usd_per_gallon"), TEXT) == []
    assert ev.check_quote(record("Tax\t\t(363)", -363, "usd_million"), TEXT) == []
    assert ev.check_quote(record("A benefit of 5-cents per gallon.", 0.05, "usd_per_gallon"), TEXT) == []
    last = "It recovered approximately half, then 80% to 90% of $50 million."
    assert ev.check_quote(record(last, 0.5, "share"), TEXT) == []
    assert ev.check_quote(record(last, [0.8, 0.9], "share"), TEXT) == []
    assert ev.check_quote(record("Outlook", None, "text"), TEXT) == []
    assert ev.check_quote(record("Fuel\t\t$\t4.19\t\t$\t2.34", 4.19, "usd_per_gallon", shift=1), TEXT)
    assert ev.check_quote(record("Fuel\t\t$\t4.19\t\t$\t2.34", 4.91, "usd_per_gallon"), TEXT)
    assert ev.check_quote(record("Tax\t\t(363)", 363, "usd_million"), TEXT)
    assert ev.check_quote(record(last, [0.8, 0.95], "share"), TEXT)
    assert ev.check_quote(record("A benefit of 5-cents per gallon.", 0.5, "share"), TEXT)  # No "half", no percentage.
    assert ev.check_quote(record("Outlook", 1, "text"), TEXT)
    assert ev.check_quote({**record("Outlook", None, "text"), "quote": "Outlook "}, TEXT)
    assert ev.printed_numbers("of $50 million, or 12.5 percent", "share") == [0.125]
    assert ev.printed_numbers("1,146 and (17.8) and 13¢", "usd_million") == [1146.0, -17.8, 0.13]


def test_cited_ids_reads_lists_maps_and_single_ids():
    node = {"a": {"evidence_ids": ["x", "y"]}, "b": [{"evidence_ids": {"low": "y", "high": "z"}}, {"evidence_id": "w"}]}
    assert ev.cited_ids(node) == ["x", "y", "z", "w"]


def test_web_lookups_are_dated_and_tied_to_the_quarter_they_were_made_for(meta):
    assert meta["guidance"]["period"] == ev.WEB["period"] and meta["as_of"] == ev.WEB["retrieved_on"]
    for block in [meta["report_date_source"], *meta["comparators"].values()]:
        assert block["retrieved_on"] == ev.WEB["retrieved_on"] and block["source_url"].startswith("https://")
    with pytest.raises(ValueError, match="Rerun"):
        ev.web_entries("2026Q4")


# ------------------------------------------------------------------ against the raw filings

def test_build_script_reproduces_the_committed_files(require_local_cache, ledger, meta, tmp_path, monkeypatch, capsys):
    require_local_cache(RAW, DOC, what="raw SEC filings")
    monkeypatch.setattr(sys, "argv", ["build_meta_evidence.py", "--out-dir", str(tmp_path),
                                      "--generated-at", ledger["generated_at"]])
    build_meta_evidence.main()
    assert json.loads(capsys.readouterr().out)["counts"] == ledger["counts"]
    assert load("evidence.json", tmp_path) == ledger and load("meta.json", tmp_path) == meta


def test_a_record_the_verifier_rejects_stops_the_build(require_local_cache):
    require_local_cache(RAW, DOC, what="raw SEC filings")
    index = {row["url"]: row for row in csv.DictReader((ROOT / ev.INDEX).open())}
    good = next(r for r in json.loads((ROOT / ev.GUIDANCE).read_text()) if r["id"] == "ual-2026q2-ex992-adj-eps-2026q3")
    assert ev.guidance_entries(ev._Sources(ROOT), [good], index)[0]["verified"] is True
    for change in ({"value": [2.5, 4.0], "high": 4.0}, {"char_start": good["char_start"] + 1},
                   {"quote": good["quote"].replace("$2.50", "$2.60")}, {"period": "2026Q4"}, {"picked_by": "me"}):
        with pytest.raises(ValueError, match="fails app.lab.verify"):
            ev.guidance_entries(ev._Sources(ROOT), [{**good, **change}], index)
    with pytest.raises(ValueError, match="SHA-256"):
        ev.guidance_entries(ev._Sources(ROOT), [{**good, "doc_sha256": "0" * 64}], index)
    with pytest.raises(FileNotFoundError):
        ev.guidance_entries(ev._Sources(ROOT / "tests"), [good], index)
