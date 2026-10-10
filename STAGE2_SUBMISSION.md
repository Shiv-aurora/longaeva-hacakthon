# Stage 2 submission | UAL Counterfactual Engine

**Entry point:** run the application and open `http://127.0.0.1:8000/monthly`.

## Research question

How do fuel, travel-demand, and fleet shocks propagate through United Airlines when capacity is constrained and management's pricing response is delayed? How do those shocks change operating income, free-cash-flow sensitivity, and the operating conditions under which an investor's thesis fails?

This is an evidence-anchored **operating sensitivity prototype**, not a validated stock-return model or a replica of United's proprietary revenue management system.

## Working application

The Python/FastAPI application implements monthly transitions for **domestic, Atlantic, Pacific, and Latin America** markets, with mainline narrowbody and widebody groups and a regional contract block. It supports fuel and demand shocks, capacity cuts, fleet-entry delays, temporary outages, regional contract-rate changes, explicit analyst purchase/cash events, and limited retirement-extension assumptions. The default engine uses 10,000 paired stochastic paths and displays assumptions alongside results. The monthly model card is `docs/monthly_model_card.md`.

Three investor workflows are implemented:

1. **What if?** Apply a shock and inspect paired operating and financial trajectories under frozen versus assumed constrained pricing.
2. **What breaks?** Search a bounded two-driver grid for cases below analyst-specified absolute FCF or minimum-cash thresholds. Tested-grid frequency is **not** event probability.
3. **What must be true?** Invert explicit operating targets over tested driver ranges. Optional operating-asset valuation uses user-supplied assumptions and is **not** a uniquely implied equity price.

The monthly interface also exports a one-page analyst thesis PDF and a JSON record tied to a verified simulated run. This is a reproducible scenario handoff, not a verified trading signal.

## Public evidence and research

- Daily EIA Gulf Coast jet-fuel pilot: 190 prints (January 5 through October 6, 2026), with weekend gaps preserved.
- UAL Q2 2026 source-backed operating baseline and 82 reviewed, dated source/assumption anchors.
- SEC quarterly imports: 54 quarter rows each for UAL, DAL and AAL, covering 2013 Q1 through 2026 Q2, with explicit missing-value and publication-date limitations.
- BTS scheduled-traffic imports: 163 monthly rows each for UA, DL and AA, January 2013 through July 2026. Coverage is **not** equivalent to consolidated marketed service or complete international activity.
- Form 41 P12/P52 cost and operations snapshots and P12A monthly fuel history, preserving source grains and mismatched reporting boundaries.
- Separate, audited utilization, aircraft-capacity, monthly-traffic and fare-source research pipelines.

The preserved research comparisons are deliberately mixed. A quarterly T2 aircraft-capacity model performs better than a direct alternative on an eligible 2020-2023 selection set, but United's narrowbody 12-month comparison has only one usable observation. A reviewed monthly traffic state model **loses** to regularized autoregression (MASE 6.833 versus 6.069); its nominal 80% conditional intervals cover 77.47% in the audited selection study. These research components are **not** presented as validated behavior inside the financial simulator.

See `docs/proposal_status.md`, `docs/reporting_panel.md`, `docs/quarterly_capacity.md`, `docs/reviewed_traffic.md` and `docs/fare_geography.md` for exact denominators, source audits and held-back boundaries.

## Verification and limits

The repository records a **385-test** passing local Python suite in `docs/local_development.md`. That is a software/source-accounting verification record, not demonstrated historical forecast performance. The observed simulation distributions are conditional on the selected model and analyst assumptions. Convergence tests measure Monte Carlo numerical stability, not probabilistic calibration.

**Open:** historical daily comparable UAL ticket quotes; fitted management/competition policies; full fleet/service-scope reconciliation; full-company cash accounting; parameter/model-form uncertainty; complete 2024+ held-out full-engine scorecard; evidence of investable alpha.

Original public archives and generated Parquet/model-run/code-bundle stores are **not tracked** in Git. A clean source checkout cannot regenerate every displayed research artifact without reacquiring original public files or transferring those reviewed caches. See `data/README.md` and individual research-method documents. Missing inputs must be reported, never silently synthesized.

## Run and inspect

Before final upload, run `python -m pytest -q tests/test_stage2_judge_flow.py` and the full suite locally. GitHub documentation records an earlier full 385-test pass; the added judge-flow changes require a fresh local run.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python scripts/build_monthly_data.py
python -m pytest -q
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/monthly`. If a data-dependent research scorecard is unavailable in a clean checkout, consult its `docs/` rebuild and verification instructions. The primary simulator relies on committed curated inputs and does not claim access to unavailable private research archives.

**Fast demo path:** open `/monthly` and select **RUN JUDGE SCENARIO**. The preset runs the actual monthly engine with an illustrative 20% fuel shock, a nine-month delay to 30 eligible fleet entries, and a two-month assumed pricing lag. Review the resulting operating-income/FCF summary, paired policies, fleet feasibility and uncertainty. Then explore **What breaks?**, **What must be true?**, and the one-page thesis export. For a timed walkthrough see `docs/stage2_judge_demo.md`.

**Deployment:** Run locally by default. The current FastAPI server and file-upload paths are not authenticated; do not expose it publicly without access controls and input review.

## Compliance

Only public, sourced, properly licensed evidence may enter the competition submission. Do not bundle employer-proprietary data, private investor materials, raw confidential sources or fabricated test results. See Longaeva's participant certification for binding restrictions.

For release review, see `docs/proposal_delivery.md` and the original research specifications.