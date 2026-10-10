"""Forward call tests: the chain, the freeze and the scoring. Committed inputs only; nothing is written outside tmp; no network."""
from datetime import timedelta
import hashlib
import json

import numpy as np
import pytest

from app.lab import forward_call as fw
from app.lab import stats
from app.lab import track_record as tr
from scripts.lab import build_q3_call, score_q3_call

STAMP = "2026-10-09T00:00:00Z"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if not fw.TRACK_RECORD.is_file():
        pytest.skip("app/static/data/track_record.json has not been built")
    folder = tmp_path_factory.mktemp("q3")
    out, frozen = folder / "q3_call.json", folder / "q3_call_frozen.json"
    payload = build_q3_call.freeze(out, frozen, frozen_at=STAMP, generated_at=STAMP)
    return {"payload": payload, "out": out, "frozen": frozen}


@pytest.fixture(scope="module")
def payload(built):
    return built["payload"]


@pytest.fixture(scope="module")
def bare(tmp_path_factory):
    """The same forecast built with no meta.json: no consensus, no market price."""
    if not fw.TRACK_RECORD.is_file():
        pytest.skip("app/static/data/track_record.json has not been built")
    folder = tmp_path_factory.mktemp("bare")
    return build_q3_call.freeze(folder / "q3_call.json", folder / "frozen.json", frozen_at=STAMP, generated_at=STAMP,
                                cross_check=False, meta=folder / "no_meta.json")


@pytest.fixture(scope="module")
def block(payload):
    return payload["forecast"]


# ------------------------------------------------------------------ the freeze

def test_canonical_bytes_are_sorted_compact_and_hash_to_the_fingerprint():
    block = {"b": [1, 2.5], "a": {"z": None, "y": "x"}}
    assert fw.canonical(block) == b'{"a":{"y":"x","z":null},"b":[1,2.5]}'
    assert fw.canonical({"a": block["a"], "b": block["b"]}) == fw.canonical(block)
    assert fw.sha256(block) == hashlib.sha256(fw.canonical(block)).hexdigest()
    assert fw.sha256({**block, "b": [1, 2.6]}) != fw.sha256(block)


def test_frozen_file_holds_the_block_and_the_hash_covers_it(built, payload, block):
    assert built["frozen"].read_bytes() == fw.canonical(block)
    assert payload["sha256"] == hashlib.sha256(built["frozen"].read_bytes()).hexdigest() == fw.sha256(block)
    assert json.loads(built["out"].read_text()) == payload
    fw.validate(payload, built["frozen"])
    assert payload["frozen_at"] == block["frozen_at"] == STAMP and payload["outcome"] is None and payload["scores"] is None
    for key in ("chain", "eps_samples_quantiles", "odds", "scoring", "known_before_freeze", "data_cutoff", "comparators"):
        assert payload[key] == block[key]
    assert payload["timestamp_proof"] is None and "q3_call_frozen.json" in payload["timestamp_proof_note"]


def test_build_refuses_to_replace_a_frozen_forecast(built, monkeypatch, capsys):
    before = built["frozen"].read_bytes()
    with pytest.raises(FileExistsError, match="--refreeze"):
        build_q3_call.freeze(built["out"], built["frozen"], frozen_at="2026-10-11T00:00:00Z")
    assert built["frozen"].read_bytes() == before
    monkeypatch.setattr("sys.argv", ["build_q3_call.py", "--out", str(built["out"]), "--frozen", str(built["frozen"])])
    with pytest.raises(SystemExit) as stop:
        build_q3_call.main()
    assert stop.value.code == 1 and "was not changed" in capsys.readouterr().err
    assert built["frozen"].read_bytes() == before


def test_refreeze_replaces_it_and_the_same_inputs_give_the_same_block(built, tmp_path, block):
    out, frozen = tmp_path / "q3_call.json", tmp_path / "frozen.json"
    frozen.write_bytes(b"{}")
    again = build_q3_call.freeze(out, frozen, refreeze=True, frozen_at=STAMP, generated_at=STAMP, cross_check=False)
    assert frozen.read_bytes() == fw.canonical(again["forecast"])
    same = {k: v for k, v in again["forecast"].items() if k != "simulator_cross_check"}
    assert same == {k: v for k, v in block.items() if k != "simulator_cross_check"}


def test_validate_catches_a_changed_block_and_drifting_copies(built, payload):
    changed = json.loads(json.dumps(payload))
    changed["forecast"]["chain"][3]["p50"] += 0.01
    with pytest.raises(ValueError, match="sha256"):
        fw.validate(changed)
    drift = json.loads(json.dumps(payload))
    drift["odds"]["below_range"] = 0.0
    with pytest.raises(ValueError, match="differs from the forecast block"):
        fw.validate(drift)
    with pytest.raises(ValueError, match="fixture"):
        fw.validate({**payload, "fixture": True})


# ------------------------------------------------------------------ the chain

def test_chain_has_four_links_with_management_beside_each(block):
    fuel, share, trasm, eps = block["chain"]
    assert [link["link"] for link in block["chain"]] == ["fuel_price_paid_usd_gal", "recovery_share", "trasm_yoy", "adj_eps_diluted"]
    for link in block["chain"]:
        assert link["p10"] <= link["p50"] <= link["p90"] and "management" in link
    assert fuel["management"] == 3.69 and share["management"] == [0.8, 0.9] and eps["management"] == [2.5, 3.5]
    assert trasm["management"] == "above 0.121" and trasm["management_detail"]["evidence_id"] == "ual-2026q2-ex992-trasm-2026q3"
    assert "never defined" in share["management_note"]
    assert fuel["fuel_prices_complete"] is True and fuel["last_fuel_price_used"] == "2026-09-30"
    assert fuel["past_misses"]["n"] >= 10 and fuel["p90"] - fuel["p10"] > 0.05
    assert block["guidance"]["updates_since"]["used"] is False and block["guidance"]["updates_since"]["filings_found"] == []


def test_eps_link_is_the_replay_function_with_the_chosen_variant(block):
    world = tr.World()
    published = json.loads(fw.TRACK_RECORD.read_text())
    variant = published["selection"]["chosen"]
    assert block["method"]["variant"] == variant and block["method"]["fuel_effect_from"] == "closed_form"
    cutoff = tr._day(block["data_cutoff"])
    row = tr.forecast(world, fw.TARGET, cutoff, cutoff + timedelta(days=1))
    fuel, share, _, eps = block["chain"]
    ours = row["primary"]["ranges"][variant]
    assert eps["p50"] == pytest.approx(ours["p50"], abs=1e-6) and eps["p10"] == pytest.approx(ours["p10"], abs=1e-6)
    assert fuel["p50"] == pytest.approx(row["fuel"]["usd_gal"], abs=1e-6)
    assert sorted(block["eps_samples"]) == pytest.approx(sorted(ours["members"]), abs=1e-6)
    assert len(block["eps_samples"]) == eps["earlier_misses_n"] == len(published["quarters"])
    # Midpoint, marked to the clock's fuel, less the part not recovered.
    surprise = (fuel["p50"] - fuel["management"]) * eps["gallons_m"] * (1 - eps["tax_rate"]) * (1 - eps["profit_sharing_rate"]) / eps["diluted_shares_m"]
    assert eps["fuel_surprise_per_share"] == pytest.approx(surprise, abs=1e-5)
    assert eps["p50"] == pytest.approx(eps["guidance_mid"] - (1 - share["p50"]) * surprise, abs=1e-5)
    calls = block["scoring"]["baseline_calls"]
    assert calls["guidance_mid"] == 3.0 and calls["consensus"] == block["comparators"]["consensus_eps"]["value"]
    assert calls["costs_only"] == pytest.approx(3.0 - surprise, abs=1e-5)
    assert calls["management"] == pytest.approx(3.0 - (1 - 0.85) * surprise, abs=1e-5)
    assert block["without_profit_sharing"]["costs_only"] < calls["costs_only"]


def test_odds_quantiles_and_scoring_rule(block):
    odds, samples = block["odds"], np.array(block["eps_samples"])
    assert odds["below_range"] + odds["inside_range"] + odds["above_range"] == pytest.approx(1.0)
    assert odds["below_range"] == pytest.approx(np.mean(samples < 2.5), abs=1e-6) and odds["above_range"] == pytest.approx(np.mean(samples > 3.5), abs=1e-6)
    consensus = block["comparators"]["consensus_eps"]["value"]
    assert odds["consensus_used"] == consensus and odds["n_samples"] == len(samples)
    if consensus is None:
        assert odds["above_consensus"] is None and odds["above_consensus_reason"]
    else:
        assert odds["above_consensus"] == pytest.approx(np.mean(samples > consensus), abs=1e-6) and odds["above_consensus_reason"] is None
    levels = [q[0] for q in block["eps_samples_quantiles"]]
    values = [q[1] for q in block["eps_samples_quantiles"]]
    assert levels == list(fw.LEVELS) and values == sorted(values)
    assert dict(block["eps_samples_quantiles"])[0.5] == pytest.approx(np.median(samples), abs=1e-6)
    assert block["scoring"]["rule"] == "CRPS" and block["scoring"]["baselines"] == ["guidance_mid", "consensus", "costs_only", "management"]


def test_nothing_in_the_forecast_is_dated_after_its_cutoff(payload, block):
    assert block["data_cutoff"] < block["target"]["report_expected_on"]
    assert block["uses"] and all(use["dated"] <= block["data_cutoff"] for use in block["uses"])
    assert any("Delta reported" in line for line in block["known_before_freeze"])
    assert any("No guidance update" in line for line in block["known_before_freeze"])
    assert payload["as_of"] == payload["data_cutoff"] == block["data_cutoff"]
    assert all(not item["path"].startswith("/") and len(item["sha256"]) == 64 for item in payload["inputs"])
    assert "/Users/" not in json.dumps(payload)
    assert isinstance(payload["plain_summary"], list) and payload["plain_summary"][0] == payload["headline"] and payload["limits"]


def test_comparators_come_from_meta_with_value_date_and_url_or_are_null(payload, block, bare):
    for item in block["comparators"].values():
        if item["value"] is None:
            assert item["reason"] and item["as_of"] is None and item["source_url"] is None
        else:
            assert item["as_of"] <= block["data_cutoff"] and item["source_url"].startswith("https://") and item["taken_from"] == "app/static/data/meta.json"
    if fw.META.is_file():
        meta = json.loads(fw.META.read_text())
        for name in ("consensus_eps", "market_odds_beat"):
            ours, theirs = block["comparators"][name], meta["comparators"][name]
            assert [ours[k] for k in ("value", "as_of", "source_url")] == [theirs[k] for k in ("value", "as_of", "source_url")]
        assert block["target"]["report_expected_on"] == meta["report_date"]
        assert any(item["path"] == "app/static/data/meta.json" for item in payload["inputs"])
    # Without meta.json nothing is invented: both are null with a reason and the chain is the same.
    plain = bare["forecast"]
    assert plain["comparators"]["consensus_eps"] == {"value": None, "as_of": None, "source_url": None, "reason": plain["comparators"]["consensus_eps"]["reason"]}
    assert plain["comparators"]["market_odds_beat"]["value"] is None and plain["comparators"]["market_odds_beat"]["reason"]
    assert plain["odds"]["above_consensus"] is None and plain["odds"]["above_consensus_reason"]
    assert plain["scoring"]["baseline_calls"]["consensus"] is None and plain["target"]["report_expected_on"] == fw.REPORT_EXPECTED_ON
    assert plain["chain"] == block["chain"] and plain["eps_samples"] == block["eps_samples"]
    assert all(item["path"] != "app/static/data/meta.json" for item in bare["inputs"])


def test_unit_revenue_link_reproduces_the_fare_clock_equation(block):
    trasm = block["chain"][2]
    fare = json.loads(tr.Paths().fare_clock.read_text())
    fit = fare["smooth"]["through_latest"]
    inputs = trasm["inputs"]
    assert trasm["form"] == "h1" and trasm["not_used_by_eps_link"] is True
    assert inputs["long_run_share"] == fit["L"] and inputs["capacity_growth_pct"] == 1.0
    points = {p["period_end"]: p for p in fare["united_points"]}
    lags = [inputs["fuel_cost_change_cents_per_asm"]] + [points[d]["fuel_shock"] for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30")]
    before = [points[d]["fuel_shock"] for d in ("2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30")]
    weights = [(1 - fit["rho"]) * fit["rho"] ** j for j in range(5)]
    term, term_before = (fit["L"] * float(np.dot(weights, x)) for x in (lags, before))
    change = (points["2026-06-30"]["revenue_response"] + fit["capacity_coef_cents_per_point"] * (1.0 - points["2026-06-30"]["capacity_growth_pct"])
              + term - term_before)
    assert trasm["change_cents_per_asm"] == pytest.approx(change, abs=1e-4)
    assert trasm["p50"] == pytest.approx(change / trasm["year_ago_trasm_cents"], abs=1e-5)


def test_simulator_cross_check_agrees_with_the_closed_form_given_the_same_share(block):
    check = block["simulator_cross_check"]
    assert check["computed"] and check["used_in_forecast"] is False and check["period"] == "2026Q4"
    modes = check["modes"]
    same = modes["replay_curve"]["operating_income_change_musd"]["p50"]
    share = modes["replay_curve"]["share_in_first_quarter_p50"]
    none = modes["costs_only"]["operating_income_change_musd"]["p50"]
    assert same == pytest.approx((1 - share) * none, rel=0.02)          # the simulator's own no-recovery hit, less the share
    assert none < modes["measured"]["operating_income_change_musd"]["p50"] < 0
    assert modes["measured"]["share_in_first_quarter_p50"] < share


# ------------------------------------------------------------------ scoring after the report

def test_scoring_fills_the_outcome_and_leaves_the_block_alone(bare):
    payload, block = bare, bare["forecast"]
    before = fw.canonical(block)
    scored = fw.score(payload, 2.75, "2026-10-20", fuel_price=3.95, trasm_yoy=0.14, source="made up for the test",
                      consensus={"value": 2.9, "as_of": "2026-10-19", "source_url": "https://example.com"}, scored_at=STAMP)
    assert fw.canonical(scored["forecast"]) == before and scored["sha256"] == payload["sha256"]
    assert payload["outcome"] is None                                         # the input is not changed in place
    outcome, scores = scored["outcome"], scored["scores"]
    assert outcome["adj_eps_diluted"] == 2.75 and outcome["position"] == "inside_range" and outcome["reported_on"] == "2026-10-20"
    assert scores["fitted"]["crps"] == pytest.approx(stats.crps_ensemble(np.array(block["eps_samples"]), 2.75))
    assert scores["fitted"]["abs_error"] == pytest.approx(abs(block["chain"][3]["p50"] - 2.75))
    assert scores["baselines"]["guidance_mid"]["crps"] == pytest.approx(0.25)
    assert scores["baselines"]["consensus"]["abs_error"] == pytest.approx(0.15)
    assert set(scores["ranking"]) == {"fitted", "guidance_mid", "consensus", "costs_only", "management"}
    assert scores["fuel_price_paid_usd_gal"]["abs_error"] == pytest.approx(abs(block["chain"][0]["p50"] - 3.95))
    assert scores["fuel_price_paid_usd_gal"]["management_abs_error"] == pytest.approx(0.26)
    assert scores["trasm_yoy"]["actual"] == 0.14 and isinstance(scores["trasm_yoy"]["inside_p10_p90"], bool)
    assert scores["above_consensus_from_frozen_samples"] == pytest.approx(np.mean(np.array(block["eps_samples"]) > 2.9))
    assert scores["consensus_scored"] == "the figure supplied after the freeze"
    plain = fw.score(payload, 2.75, "2026-10-20")
    assert plain["scores"]["baselines"]["consensus"]["crps"] is None and "consensus" not in plain["scores"]["ranking"]
    assert plain["scores"]["fuel_price_paid_usd_gal"] is None and plain["scores"]["consensus_scored"] is None


def test_a_consensus_frozen_in_the_block_is_the_one_scored(payload, block):
    frozen = block["scoring"]["baseline_calls"]["consensus"]
    if frozen is None:
        pytest.skip("no consensus figure is recorded in meta.json")
    scored = fw.score(payload, 2.75, "2026-10-20", consensus={"value": frozen + 1.0, "as_of": "2026-10-19", "source_url": "https://example.com"})
    assert scored["scores"]["baselines"]["consensus"]["call"] == frozen
    assert scored["scores"]["baselines"]["consensus"]["abs_error"] == pytest.approx(abs(frozen - 2.75))
    assert scored["scores"]["consensus_scored"] == "the figure frozen in the block"
    assert scored["scores"]["above_consensus_from_frozen_samples"] == pytest.approx(block["odds"]["above_consensus"], abs=1e-6)


def test_scoring_refuses_an_early_result_or_a_changed_block(payload):
    with pytest.raises(ValueError, match="cutoff"):
        fw.score(payload, 2.75, payload["data_cutoff"])
    changed = json.loads(json.dumps(payload))
    changed["forecast"]["eps_samples"][0] -= 1.0
    with pytest.raises(ValueError, match="no longer matches"):
        fw.score(changed, 2.75, "2026-10-20")


def test_score_script_writes_beside_the_block_and_checks_the_frozen_file(built, tmp_path):
    call, frozen = tmp_path / "q3_call.json", tmp_path / "frozen.json"
    call.write_bytes(built["out"].read_bytes())
    frozen.write_bytes(built["frozen"].read_bytes())
    scored = score_q3_call.run(call, frozen, actual_eps=3.1, reported_on="2026-10-20", fuel_price=3.9, trasm_yoy=None, source="test")
    written = json.loads(call.read_text())
    assert written == scored and written["outcome"]["adj_eps_diluted"] == 3.1 and written["forecast"] == built["payload"]["forecast"]
    assert frozen.read_bytes() == built["frozen"].read_bytes()
    frozen.write_bytes(b'{"tampered":true}')
    with pytest.raises(SystemExit):
        score_q3_call.run(call, frozen, actual_eps=3.1, reported_on="2026-10-20")


# ------------------------------------------------------------------ the published files

def test_published_call_matches_its_frozen_file_and_the_track_record():
    if not (fw.OUTPUT.is_file() and fw.FROZEN.is_file()):
        pytest.skip("the forward call has not been frozen yet")
    published = json.loads(fw.OUTPUT.read_text())
    fw.validate(published, fw.FROZEN)
    assert published["sha256"] == hashlib.sha256(fw.FROZEN.read_bytes()).hexdigest()
    assert published["forecast"]["method"]["variant"] == json.loads(fw.TRACK_RECORD.read_text())["selection"]["chosen"]
    assert published["forecast"]["target"]["period"] == "2026Q3" and not published.get("fixture")
