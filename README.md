# UAL Counterfactual Lab

A forward simulation of United Airlines as a business. Set a scenario (fuel price,
travel demand, capacity, late aircraft) and the simulator runs it 10,000 times and
shows the range of outcomes for operating income, margin, free cash flow and
earnings per share. How the airline responds inside the simulation is measured
from its own history, and the method is checked against quarters it never saw.

Stage 2 entry for the Longaeva AI Hackathon. Judges: the one-page brief is
[STAGE2_SUBMISSION.md](STAGE2_SUBMISSION.md); the technical report is at `/report`
in the running app, live at [https://ual-twin.vercel.app/report](https://ual-twin.vercel.app/report).
The legacy live demo is at [https://ual-twin.vercel.app](https://ual-twin.vercel.app).

## Run it

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000. Everything the pages show is already built and
committed, so no data download is needed.

The agent console works without any key: a rules planner runs the same tools. To
let a language model plan the work and word the answer, copy `.env.example` to
`.env.local` and set `GROQ_API_KEY`.

## What you see

| View | What it is for |
|---|---|
| Simulator | Move the drivers and read the outcomes. Three tabs: **What if** (one scenario), **What breaks** (where free cash flow turns negative across two drivers), **What must be true** (what has to hold for a target to be met). Ask the agent in plain words and it plans, runs the simulator and writes a short checked report. |
| Behavior | The relationships measured from history that drive the simulator. |
| Reality check | The method replayed on past quarters against simple rivals, Delta's latest quarter as a worked example, and one forecast for United's Q3 2026 fixed before the report. |

Every scenario runs three ways on the same 10,000 simulated worlds, so the
difference between them is the airline's response and nothing else:

- **No response**: fuel costs change and fares do not.
- **As management says**: United's own stated recovery path.
- **As history shows**: the recovery measured from United's reported quarters.

## What was measured

| Behavior | Result | On years it never saw |
|---|---|---|
| Spot fuel to United's fuel bill | Price paid = $0.14 + 1.05 x spot, about 18 days behind | Average miss 9 cents a gallon on 2024-2026; it ran low in all 10 quarters and its ranges were too narrow |
| Fuel cost to revenue | A year after a fuel cost change United has made up about 62% of it (90% range 49% to 91%; best single fit 75%) | The curve fixed in advance lost to simple rules (see below). The speed is not pinned down |
| Capacity | No response to fuel prices could be established | Did not help |
| Stock price | A 10% one-day rise in fuel goes with a 2.0% same-day fall (2024-2026) | No delayed move. A trading rule picked on 2020-2023 lost money on 2024-2026 |
| Travel demand | TSA travelers 2.85% below a year earlier in Q3 2026 | Context, not an input |
| Daily fares, 2022 | United's fare index peaked 34 days after fuel did | One episode; half the specifications include zero |
| Three airlines pooled | 85% after a year (49% to 104%) | Did not narrow the range, so it stays a cross-check |

## Reality check

- **Past quarters.** United gave a quarterly EPS range for 14 reported quarters.
  On the 10 from 2024 onward, marking its midpoint to fuel with the measured
  recovery missed reported EPS by $0.26 a share on average. United's own midpoint
  missed by $0.26, and assuming no recovery missed by $0.26. A three-way tie: the
  method did not beat United's own number.
- **Fuel price.** By the end of a quarter the fuel model is 6 cents a gallon from
  the price United later reports. United's stated assumption is 25 cents away
  (8 quarters).
- **Are the ranges the right width?** With the assumed demand noise, the
  simulator's 80% range held 25 of United's 33 unit-revenue outcomes in 2022-2026
  (76%) and 28 of 33 operating-margin outcomes (85%). It held every outcome in
  the calm years before 2020, so it is too wide for calm markets. Fuel
  volatility, measured over the 65 trading days before the cutoff (2.3 times as
  volatile as 2013-2019), sets most of the width.
- **Delta, Q3 2026.** Guided $2.00 to $2.50 at about $3.15 a gallon, paid $3.61,
  reported $1.72 on 9 October. No recovery gives $1.65; the measured share gives
  $1.88.
- **One forecast fixed in advance.** Frozen before United reports on 20 October
  2026: adjusted EPS of $2.54 (P10 $2.18, P90 $2.82) against United's range of
  $2.50 to $3.50, because fuel is put at $3.93 a gallon against the $3.69 United
  assumed. The forecast block, with its freeze time, is fixed in
  `data/lab/q3_call_frozen.json`; its SHA-256 is
  `053f94c7c2442a506c8948fd3d0c88f1b89888cfd3a5e1ffd9d6fccc3aedc068`.
  `scripts/lab/score_q3_call.py` adds the outcome and scores beside it afterwards.
  Because the replay did not beat the midpoint, this is a test of the method, not
  a claim that it is better than United's number.

## What did not work

Reported in the app and the report, not removed:

- The recovery curve fixed before the test years (five free quarterly lags) lost.
  Average miss in unit revenue four quarters ahead, cents per seat mile, 10
  quarters: no response 1.14, that curve 0.73, full immediate recovery 0.59,
  management's path 0.56. A smoother two-parameter curve scores 0.56, but it was
  chosen after the test years had been seen and is flagged as such everywhere.
- The speed of recovery depends heavily on one quarter (Q2 2026): half-life 1.0
  quarter with it, 2.8 without.
- On earnings, marking guidance to fuel tied the midpoint (above).
- The fuel model's ranges held the reported price in 3 of 10 test quarters.
- No capacity response and no tradable delay in the stock were found.
- A demand-noise value fitted on 2013-2019 covered only 61% of later outcomes
  and was set aside for the assumed one.
- The simulator has no seasonality: every quarter looks alike, while United's
  first quarter is much weaker than its second.
- A placeholder check in the agent can pass a figure placed beside the wrong words.

## Against the Stage 1 proposal

The proposal's hypothesis was that delayed, constrained airline responses should
explain outcomes better than a model that changes costs and holds behavior fixed.
What the evidence supports: a response beats holding behavior fixed on unit
revenue (0.56 to 0.73 against 1.14; with 10 quarters the range on that gap just
reaches zero). What it does not support: that a delayed response beats an
immediate one. With this few quarters the two cannot be told apart, and on
earnings no difference is visible.

| Proposed | Status |
|---|---|
| Monthly simulator of the business, four markets, fleet, cash | Delivered (`app/monthly.py`) |
| What if / What breaks / What must be true | Delivered |
| Behavior measured, not assumed | Delivered for fuel cost, fare recovery and stock reaction; capacity measured and found absent |
| Fit 2013-2019, select 2020-2023, locked test from 2024 | Delivered; anything chosen after the test years were seen is flagged as such |
| Evidence ledger; the language model extracts and never authors numbers | Delivered: 509 guidance records and a 72-item ledger, quotes checked against document text |
| Per-market fitted demand and pricing | Not delivered: one company-wide recovery curve |
| Competitor response inside the simulator | Not delivered: Delta and American appear only as cross-checks |
| Crew and weather constraints | Not delivered |
| Testing with analysts | Not done |

## How AI is used

- **Agent console.** A language model (Groq) plans which tools to call; the tools
  are the simulator and lookups over the built files. Tool results reach the model
  as labeled facts and it writes with placeholders that code fills in, so it
  cannot type a number. With no model available a rules planner does the same job.
- **Guidance reader.** Code finds candidate sentences in SEC filings, a picker
  (rules, or the model choosing an id) selects one, code parses the value, and a
  verifier keeps it only if the quote is an exact substring of the document.

## Data and licenses

| Source | Used for | Terms |
|---|---|---|
| U.S. EIA, Gulf Coast jet fuel, daily | Fuel prices | Public domain |
| SEC EDGAR: United, Delta and American filings | Reported quarters, guidance, quotes | Public |
| TSA checkpoint counts, daily | Travel demand | Public domain |
| FRED / BLS consumer prices | Airfare index, inflation | Public |
| Yahoo Finance daily prices | Share prices | Unofficial feed, research use |
| `dilwong/flightprices` (Kaggle), Expedia quotes April to October 2022 | Daily fares in 2022 | CC BY 4.0, by Dillon Wong |

Raw downloads stay out of the repository; `data/lab/manifests/` records each
file's URL, retrieval time and SHA-256, and `data/lab/` holds the small tidy tables
built from them.

## Repository map

| Path | Contents |
|---|---|
| `app/monthly.py` | The business simulator |
| `app/lab/` | Measurements, scenario runner, replay, forecast, agent, guidance reader |
| `scripts/lab/` | Builders for every data file |
| `app/static/lab/` | The dashboard and the report page |
| `app/static/data/` | Built outputs the pages read |
| `data/lab/` | Tidy inputs with provenance |
| `docs/lab_contract.md` | File shapes and rules |
| `docs/monthly_model_card.md` | Simulator model card |
| `docs/engine/` | Notes on the data pipelines and studies behind the simulator |

## Tests

```bash
python -m pytest -q
```

Tests run offline. A fresh clone skips the few that need local research stores.

## Credits

Navigation components adapted from [Rare UI](https://rareui.com) (Gooey Nav and
Hook Sidebar, © 2026 Swami Malode). Charts drawn with Observable Plot and D3. Set
in Geist. License files are in `app/static/lab/`.
