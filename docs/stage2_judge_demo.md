# Stage 2 judge demonstration (2 to 3 minutes)

## 0:00-0:25 | The question
UAL Counterfactual Engine is an evidence-anchored operating sensitivity model of United Airlines. Its research question is how fuel, demand and fleet shocks transmit through capacity, operating profit and cash when management responses are delayed or constrained. It is not a United pricing-policy replication or a stock-return forecast.

Open `/monthly` directly. The separate `/` screen is the earlier daily fuel pilot.

## 0:25-1:10 | The working scenario
Press **RUN JUDGE SCENARIO**. This applies a deliberately illustrative 20% fuel shock and delays 30 eligible planned aircraft entries by nine months, with a two-month assumed pricing lag. The existing API simulates the shock and no-shock worlds with shared random drivers. The results summary reports:
- Paired cumulative operating-income impact, median
- Paired cumulative FCF impact, median and conditional P10 to P90 spread
- Modeled policy-response contribution relative to frozen pricing

Point to the capacity feasibility table: the intervention respects the declared fleet-entry queue and assumptions. **Do not state that these modeled aircraft delays actually happened** or that their management response rates were observed.

## 1:10-1:45 | Investor questions
In **What breaks?**, choose two drivers and an analyst-defined absolute FCF or minimum-cash threshold. The grid tests each combination with shared worlds and exposes actual witness scenarios. A fraction of grid points is **not** a probability of real events.

In **What must be true?**, supply an operating target such as FCF or margin. The inversion lists compatible tested combinations and matching paths, not one uniquely implied forecast. The optional valuation is based on independently entered FCFF assumptions and does not infer the actual UAL share price.

## 1:45-2:15 | Evidence and validation
The operating baseline uses dated source-backed UAL disclosures; the fuel pilot uses observed EIA prices. The research pipeline separately imports SEC, BTS and Form 41 history. It records raw source boundaries and missing data rather than inventing observations.

The repository reports extensive source/software checks and some fitted subcomponent comparisons. Critically, its reviewed monthly traffic state model **does not beat regularized autoregression** on the specified selection set. The fitted research components do not yet replace the simulator's assumed pricing and capacity response rules.

## 2:15-2:40 | What this establishes
The prototype demonstrates an inspectable, repeatable counterfactual workflow with explicit financial identities, fleet constraints and a one-page analyst handoff. It does **not** establish fitted United management behavior, calibrated future intervals or a backtested investment signal. The next scientific milestone is to match fare, traffic and cost source boundaries, fit observed responses and test the whole engine on a truly held-out period.

## Final smoke checks (run on your machine before upload)

```bash
git pull --ff-only
python -m pytest -q tests/test_stage2_judge_flow.py
python -m pytest -q
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/monthly` and press **RUN JUDGE SCENARIO**. Confirm that the result panel, capacity table and What breaks? / What must be true? controls all work.

## Packaging

```bash
bash scripts/package_stage2.sh
```

This ZIP contains committed files only, not the full locally archived research sources. Include the technical report PDF; if a verified video is already recorded, include it as an extra. Do not delay the Box upload for a public deployment. Verify that the Box upload completed before 11:59 PM ET on October 9, 2026.
