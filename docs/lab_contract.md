# Lab contract: files, ownership and JSON shapes

This is the working agreement between the data, analysis, AI and front-end parts
of the UAL Counterfactual Lab build. If code and this file disagree, fix one of
them in the same change.

## What we are building

A forward simulation of United Airlines as a business: how capacity, traffic,
revenue, costs, margin and cash move when fuel, demand, aircraft deliveries or
management's own choices change, shown as ranges from random runs. The simulator
already exists in this repository (`app/monthly.py`). What it lacked is evidence
for how the airline behaves: its pricing response was a user-set assumption.

This build measures that behaviour from history and puts it inside the simulator,
then checks the simulator against what actually happened. Management statements
(for example United's July 2026 guidance that it will recover 80-90% of the fuel
increase in the third quarter) are inputs and comparison points, not the subject:
they appear as one preset scenario and one line in the validation, never as the
headline.

Three "clocks", each at the fastest frequency public data allows:

| Clock | Question | Frequency |
|---|---|---|
| Cost | How fast does spot jet fuel reach United's reported price per gallon? | daily fuel, quarterly reported price |
| Fare | What share of a fuel shock comes back through revenue, by quarter? | quarterly United, monthly industry CPI, daily 2022 case study if data allows |
| Market | How fast does the stock react, and does it over- or under-shoot? | daily |

## The simulation is the product

The hackathon asks for a forward simulation of a business, not an earnings
predictor. So the pieces fit together like this:

1. One measured response module (`app/lab/response.py`): how much of a fuel-cost
   shock United wins back through unit revenue, by quarter after the shock, with
   its uncertainty as joint draws. It is estimated by the fare clock and nothing
   else defines the response.
2. The business simulator (`app/monthly.py`: four markets, fleet groups, regional
   block, fuel, demand, capacity, cash) gains a "measured" pricing mode that draws
   that response per path. "Costs only" (no response) and "management's promise"
   are the same simulator with a different response plugged in.
3. The track record and the dated forward test are that same response applied to
   past and current quarters. They are how the simulation is validated; EPS is
   one scoreboard among several (fuel price paid, unit revenue, margin), not the
   product.

The site is an application, not an article. On a desktop screen it fits the
window without page scrolling: a slim top bar with three views, and panels that
show a title, a chart and numbers. Explanations open on demand in a side drawer;
long-form writing lives in `/report`. Rule of thumb: a visitor should never have
to read a paragraph to use it.

1. Simulator (default view). Scenario controls and presets on the left; outcome
   tiles and the main chart in the middle with three tabs (What if, What breaks,
   What must be true); the agent console docked on the right.
2. Behaviour. A grid of chart tiles for the measured relationships that drive the
   simulator: fuel price to fuel bill, fuel cost to revenue, capacity, travel
   demand, stock reaction, daily fares.
3. Reality check. The past-quarter replay against simple rivals, the Delta check
   and the dated forward test. Management's own statements appear here only, as
   one comparison row.

`POST /api/lab/grid` serves both "What breaks" and "What must be true":
```json
{"x": {"driver": "fuel_shock_pct", "values": [-20, 0, 20, 40]},
 "y": {"driver": "demand_shock_pct", "values": [-6, -3, 0, 3]},
 "metric": "fcf_12m_musd", "threshold": 0, "direction": "below",
 "mode": "measured", "base": {"horizon_months": 12}}
```
returns
```json
{"status": "ok", "metric": "fcf_12m_musd", "threshold": 0, "direction": "below", "mode": "measured",
 "cells": [{"x": 20, "y": -3, "prob": 0.0, "p50": 0.0}],
 "boundary": [{"x": 0.0, "y": 0.0}], "paths": 0, "elapsed_ms": 0}
```
`prob` is the share of runs on the stated side of the threshold at that tested
point; it is not the probability of the event in the world.

`POST /api/lab/scenario` (draft; `app/lab/scenario.py` owns the final shape):
```json
{"fuel_shock_pct": 25, "fuel_shock_months": 6, "capacity_change_pct": 0, "demand_shock_pct": 0,
 "delivery_delay": {"aircraft": 0, "months": 0}, "horizon_months": 12, "seed": 1}
```
returns
```json
{"status": "ok", "run_id": "...", "elapsed_ms": 0,
 "modes": {"costs_only": {"quarters": [{"period": "2026Q4",
              "operating_income_musd": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
              "operating_margin": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
              "fcf_musd": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
              "eps_change": {"p10": 0.0, "p50": 0.0, "p90": 0.0}}],
            "prob": {"fcf_negative_12m": 0.0}},
           "management": {}, "measured": {}},
 "bridge_per_share": [{"step": "fuel cost", "value": 0.0}, {"step": "fare recovery", "value": 0.0},
                      {"step": "capacity", "value": 0.0}, {"step": "profit sharing", "value": 0.0},
                      {"step": "net", "value": 0.0}],
 "response_used": {"curve_p50": [0.0], "source": {"path": "app/static/data/fare_clock.json", "sha256": "..."}},
 "assumptions": ["..."]}
```
`{"status": "not_computed"}` when the measured response has not been built yet.

`fare_clock.json` therefore also carries `"curve_draws": [[r0, r1, r2, r3, r4], ...]`
(joint samples of the recovery share by quarter after the shock) so the simulator
and the call use the same uncertainty.

## Rules that apply to every part

- No fabricated numbers. Every number in an output traces to a raw file with a
  SHA-256, or to a computation on such files. If a source fails, say so; never
  substitute.
- No look-ahead. Anything called a forecast, call or backtest uses only data
  published before its as-of date. Splits: fit 2013–2019, select 2020–2023,
  locked test 2024–2026 (touched once).
- Report failures. A model that loses to a simple rival is reported as losing.
  Every variant tried is listed in the output.
- Ranges, not points: P10/P50/P90 and probabilities.
- The language model never authors a number, coefficient or forecast.
- Tests are offline (no network), small, and run with numpy, duckdb, httpx and
  the standard library only.

## Where things live

| Path | Tracked | Purpose |
|---|---|---|
| `data/lab_raw/<area>/…` | no | Raw downloads, byte-exact |
| `data/lab/manifests/<area>.json` | yes | One entry per raw file: `url`, `retrieved_at` (UTC ISO), `sha256`, `bytes`, `path` |
| `data/lab/*.csv`, `data/lab/*.json` | yes | Small tidy inputs built from raw files |
| `app/lab/*.py` | yes | Library code (loaders, statistics, clocks, call, reader, chat) |
| `scripts/lab/*.py` | yes | Command-line builders; follow the idiom of existing `scripts/*.py` |
| `app/static/data/*.json` | yes | Final outputs the pages load. Must never contain `"fixture": true` |
| `app/static/data/fixtures/*.json` | yes | Shape-only placeholders for front-end work. Always `"fixture": true` |
| `app/static/lab/**` | yes | New site: pages, styles, scripts, vendored fonts and chart libraries |
| `tests/test_lab_<area>.py`, `tests/fixtures/lab_<area>/` | yes | Tests and small fixtures |

Tidy inputs (wave 1):

- `data/lab/fuel_daily.csv`: `date,usd_per_gallon` (EIA Gulf Coast kerosene-type jet, daily, missing days absent).
- `data/lab/prices_daily.csv`: `date,ticker,close,adj_close` for UAL, DAL, AAL, LUV, SPY, JETS, HO=F.
- `data/lab/cpi_monthly.csv`: `date,series_id,value` for CUSR0000SETG01, CUUR0000SETG01, CUSR0000SA0.
- `data/lab/ual_filings_index.csv`: one row per selected document (not every download): `accession,filed_on,period_end,form,items,exhibit,filename,url,sha256,path,kind`. `kind` is `earnings_release`, `investor_update` or `other`. `exhibit` is `8-K` when the guidance sits in the filing's own text (common from 2019).
- `data/lab/ual_quarterly.csv`: one row per quarter from the earnings-release statistics tables: `period_end,filed_on,accession,asm_m,rpm_m,total_revenue_musd,passenger_revenue_musd,trasm_cents,prasm_cents,casm_cents,casm_ex_cents,fuel_gallons_m,fuel_price_usd_gal,fuel_expense_musd,pretax_income_musd,adj_pretax_income_musd,net_income_musd,gaap_eps_diluted,adj_eps_diluted,diluted_shares_m,profit_sharing_musd,source_note`. Blank where the release does not report the item.
- `data/lab/ual_guidance.json`: list of verified guidance facts (see `evidence` shape below).
- `data/lab/dal_check.json`: Delta's July 2026 guidance, Q2 and Q3 2026 actuals, with quotes.
- `data/lab/literature.json`: published estimates with verbatim quotes and URLs.
- `data/lab/tsa_daily.csv`: `date,travelers` (TSA checkpoint counts, daily from 2019). Its output is `app/static/data/demand_signal.json` (`daily`, `quarterly`, `monthly_control`, `versus_united`, `limits`, `headline`).

## Output JSON shapes

Values below show shape and units only. Every file carries `generated_at` (UTC ISO),
`as_of` (last data date used) and `inputs` (list of `{path, sha256}`). Chart
headlines are generated from the numbers by the builder (`headline` field); the
front end never hard-codes a finding.

`meta.json`
```json
{"company": "UAL", "report_date": "2026-10-20",
 "guidance": {"made_on": "2026-07-15", "period": "2026Q3", "eps_low": 2.5, "eps_high": 3.5,
              "fuel_assumed_usd_gal": 3.69, "evidence_ids": ["..."]},
 "promises": [{"made_on": "2026-07-15", "period": "2026Q3", "recovery_low": 0.8, "recovery_high": 0.9,
               "evidence_ids": ["..."], "outcome": null}],
 "comparators": {"consensus_eps": {"value": 2.84, "as_of": "...", "source_url": "..."},
                 "market_odds_beat": {"value": 0.52, "as_of": "...", "volume_usd": 690, "source_url": "..."}}}
```

`cost_clock.json`
```json
{"headline": "...", "daily_fuel": [["2026-07-14", 3.1]], "reported": [{"period_end": "2026-06-30", "usd_gal": 4.19}],
 "fit": {"spec": "...", "lag_days": 0, "basis_usd_gal": 0.0, "slope": 0.0, "ci": [0.0, 0.0], "r2": 0.0, "n": 0},
 "by_split": {"fit": {"mae": 0.0, "n": 0}, "select": {"mae": 0.0, "n": 0}, "test": {"mae": 0.0, "n": 0}},
 "management_check": {"claimed_usd_m": 575, "claimed_eps": 1.12, "reproduced_usd_m": 0.0, "reproduced_eps": 0.0,
                      "inputs": {"gallons_m": 0.0, "price_change_usd_gal": 0.0, "tax_rate": 0.0, "diluted_shares_m": 0.0}},
 "variants_tried": ["..."]}
```

`fare_clock.json`
```json
{"headline": "...", "recovery_definition": "...",
 "curve": [{"quarters_after": 0, "p10": 0.0, "p50": 0.0, "p90": 0.0}],
 "rivals": {"costs_only": [0, 0, 0, 0], "management": [{"period": "2026Q3", "low": 0.8, "high": 0.9}],
            "published": [{"label": "...", "value": 0.0, "url": "..."}]},
 "united_points": [{"period_end": "...", "fuel_shock": 0.0, "revenue_response": 0.0, "split": "fit"}],
 "industry_monthly": [{"lag_months": 0, "coef": 0.0, "lo": 0.0, "hi": 0.0}],
 "asymmetry": {"up": 0.0, "down": 0.0, "p_value": 0.0},
 "by_split": {"fit": {}, "select": {}, "test": {}}, "n": 0, "variants_tried": ["..."]}
```

`market_clock.json`
```json
{"headline": "...", "same_day": [{"ticker": "UAL", "beta": 0.0, "t_nw": 0.0, "n": 0}],
 "lagged": [{"horizon_days": 5, "coef": 0.0, "t_nw": 0.0}],
 "event_study": {"n_events": 0, "threshold": "...", "path": [{"day": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0}]},
 "by_split": {"fit": {}, "select": {}, "test": {}}, "variants_tried": ["..."]}
```

`track_record.json`
```json
{"headline": "...",
 "quarters": [{"period": "2024Q3", "guided_on": "...", "guidance_low": 0.0, "guidance_high": 0.0,
               "fuel_assumed": 0.0, "fuel_realized": 0.0, "actual_eps": 0.0, "split": "test",
               "calls": {"guidance_mid": 0.0, "costs_only": 0.0, "management": 0.0,
                         "fitted": {"p10": 0.0, "p50": 0.0, "p90": 0.0}}}],
 "scores": [{"model": "fitted", "mae": 0.0, "crps": 0.0, "inside_p10_p90": 0.0, "n": 0}],
 "fuel_price_test": {"n": 0, "mae_guided": 0.0, "mae_marked": 0.0},
 "delta_check": {"period": "2026Q3", "guidance_low": 2.0, "guidance_high": 2.5, "actual_eps": 0.0,
                 "calls": {}, "evidence_ids": ["..."]}}
```

`q3_call.json`
```json
{"frozen_at": "...", "sha256": "...", "timestamp_proof": "q3_call.json.ots", "data_cutoff": "...",
 "known_before_freeze": ["Delta reported Q3 on 2026-10-09"],
 "chain": [{"link": "fuel_price_paid_usd_gal", "p10": 0.0, "p50": 0.0, "p90": 0.0, "management": 3.69},
           {"link": "recovery_share", "p10": 0.0, "p50": 0.0, "p90": 0.0, "management": [0.8, 0.9]},
           {"link": "trasm_yoy", "p10": 0.0, "p50": 0.0, "p90": 0.0, "management": "above 0.121"},
           {"link": "adj_eps_diluted", "p10": 0.0, "p50": 0.0, "p90": 0.0, "management": [2.5, 3.5]}],
 "eps_samples_quantiles": [[0.05, 0.0], [0.5, 0.0], [0.95, 0.0]],
 "odds": {"below_range": 0.0, "inside_range": 0.0, "above_range": 0.0, "above_consensus": 0.0},
 "scoring": {"rule": "CRPS", "baselines": ["guidance_mid", "consensus", "costs_only", "management"]},
 "outcome": null}
```

`signal.json`
```json
{"headline": "...",
 "daily": [{"date": "...", "fuel": 0.0, "eps_p10": 0.0, "eps_p50": 0.0, "eps_p90": 0.0,
            "stock_excess_since_guidance": 0.0, "gap": 0.0}],
 "backtest": {"definition": "...", "costs_bps_per_side": 5,
              "by_split": {"fit": {}, "select": {}, "test": {}}, "variants_tried": ["..."]}}
```

`evidence.json` (and `data/lab/ual_guidance.json`): list of
```json
{"id": "ual-2026q2-ex992-fuel-assumption", "claim": "fuel_assumed_usd_gal", "period": "2026Q3",
 "value": 3.69, "unit": "usd_per_gallon", "quote": "exact text from the document",
 "doc_url": "...", "doc_sha256": "...", "char_start": 0, "char_end": 0,
 "picked_by": "rules | groq:<model>", "verified": true}
```
A fact is `verified` only if the quote is an exact substring of the normalised
document text at `char_start:char_end` and the value parses from the quote.

## Front end

`app/static/lab/` loads `/static/data/<name>.json`. With `?fixtures=1` it loads
`/static/data/fixtures/<name>.json` instead and shows a visible "placeholder data"
banner. A missing or unbuilt file renders an explicit "not computed yet" state,
never a silent blank and never fixture values.

## API

`app/lab/api.py` exposes a FastAPI `APIRouter` with prefix `/api/lab`.
`app/server.py` includes it; nothing else in `app/lab/` imports `app.server`.

| Route | What it does |
|---|---|
| `GET /status` | Whether the language model is configured, which data files are built, whether the simulator is connected |
| `GET /today` | The scenario request that matches the latest fuel print, and what it rests on (`{"status": "ok", "request": {...}, "basis": [...]}`) |
| `POST /scenario` | One simulator run in three behaviour modes. No language model involved |
| `POST /grid` | The simulator over two drivers against a threshold ("What breaks", "What must be true") |
| `POST /chat` | One agent turn as a single JSON object: plan, tool calls, checked report |
| `POST /chat/stream` | The same turn as Server-Sent Events |
| `GET /insights` | Three or four ready findings for the console's empty state, built by code from the data files |

Pages: `/lab` is the application, `/report` the technical report, `/research`
and `/monthly` the earlier workstation.

A scenario response carries `status`, `run_id`, `cached`, `elapsed_ms`, `request`,
`paths`, `as_of`, `fuel`, `modes` (`costs_only`, `management`, `measured`, each with
`quarters`, `next_12m`, `prob`, `bridge`), `bridge_per_share`, `response_used`,
`capacity_response`, `per_share`, `monte_carlo`, `inputs`, `model` and
`assumptions`. An invalid request returns `{"status": "invalid", "errors": [...]}`.

## Agent shapes

Request to `/chat` and `/chat/stream`: `{message, history, model?}`. `model` is an id
from `/status` `models`, or `"rules"` to force the rules planner; anything else is a 422.

Response: `{status: ok|fallback|not_supported, answer, note, planned_by, written_by, model,
report, sources, follow_ups, facts, charts, evidence_ids, tool_calls, steps, scenarios,
grids, usage}`.

- `report`: `{title, summary, table, sections, template, facts, charts, assumptions,
  evidence_ids, verified, unsupported, planned_by, written_by}`. The title is written by
  code. The summary is one or two sentences. `sections` holds at most one paragraph.
- `report.table` is `null` for a lookup, otherwise `{columns, rows: [{label, cells:
  [{fact, text, range, label}]}], note}`. One run gives a row per behavior ("No response",
  "As management says", "As history shows"); two runs give a row per case and a
  "Difference" row.
- `report.facts`: `[{id, label, text, value, unit, source}]`. The model writes
  placeholders such as `{f7}`; code fills them. A typed figure fails the check.
- `sources`: `[{id, name, kind, detail, href}]`, `kind` one of `simulator | data | filing |
  web`. Every fact's `source` is one of these ids.
- `follow_ups`: up to three questions chosen by code.

Stream `step` event: `{id, kind: plan|tool|write|check, status: running|done|failed|retried,
label, meta, details: [{label, meta}], name, args, summary, elapsed_ms}`. A step that takes
time is sent as `running` and again under the same id when it resolves. `final` carries the
response above.

`GET /status` adds `agent: "model"|"rules"`, `models: [{id, label, note, available}]`
(always ending with the rules option) and `commands: [{name, description, template}]`.

`GET /insights`: `{status, items: [{id, text, chart: {file, kind} | null, question, source}]}`
with `chart.kind` one of `daily_estimate | recovery_curve | scores | scenario_fan`.

The agent never states the forecast's freeze time or a build time; it says "before United
reports on 20 October 2026".

## Notes on shapes as built

- Summary text: every output has `headline` (one sentence). `plain_summary` is a
  list of sentences in `fare_clock`, `market_clock` and `peer_recovery`, and a
  single string in the others; readers must accept both.
- Guidance records (`data/lab/ual_guidance.json`): a full year is labelled like
  `2026FY`; a range is `value: [low, high]` with `low` and `high` beside it; each
  record also has `qualifier`, `baseline_year`, `context_before`/`context_after`
  and revision fields.
- `fare_clock.json`: `curve` and `curve_draws` are the smooth two-parameter curve
  the simulator uses (chosen after the locked test was seen, and flagged as such);
  `curve_unrestricted` is the pre-specified five-lag curve that lost that test;
  `smooth.expanding` holds the curve as estimated at each past quarter.
- Outputs added after the first draft of this file: `capacity_response.json`,
  `peer_recovery.json` (United, American and Delta pooled; a cross-check, not an
  input to the simulator), `demand_signal.json`, `daily_fares_2022.json`.
