# Stage 2 submission: UAL Counterfactual Lab

A forward simulation of United Airlines as a business, with the airline's
behavior measured from history and the method checked against what happened.

**Open:** run the app (commands in [README.md](README.md)) and go to
`http://127.0.0.1:8000`. The technical report is at `/report`.

## The idea in three lines

1. A monthly simulator of United (four markets, fleet, fuel, demand, capacity,
   cash) runs any scenario 10,000 times and returns ranges, not single numbers.
2. How United responds to a shock is not typed in. It is measured from 54 reported
   quarters, daily fuel prices and daily share prices, then plugged into the
   simulator beside two rivals: no response, and what management says.
3. The same method is replayed on past quarters against simple rivals, and one
   forecast for the quarter United reports on 20 October 2026 was frozen in
   advance with a hash.

## A three-minute tour

1. **Simulator.** The page opens on today's fuel price. Read the four tiles, then
   drag *Fuel price*. The three lines are the same simulated worlds under three
   behaviors.
2. **What breaks.** The grid shows the share of runs with negative free cash flow
   across fuel and demand; the line is where it crosses the threshold.
3. **Ask.** Type "What happens if jet fuel stays 25% higher for 6 months and
   United cuts capacity by 3%?" The agent shows its plan, runs the simulator,
   checks every figure and writes a short report.
4. **Behavior.** Six measured relationships. The large one, fuel cost to revenue,
   is the curve the simulator uses.
5. **Reality check.** Past quarters, the Delta check, and the frozen forecast with
   its daily path since United's guidance.

## What it found

- United pays about $0.14 plus 1.05 times the spot price of jet fuel, 18 days
  behind. By the end of a quarter this is 6 cents a gallon from the reported
  price; United's own stated assumption is 25 cents away.
- A year after a fuel cost change United has made up about 62% of it through
  revenue (90% range 49% to 91%). How fast is not pinned down.
- The stock reacts the same day (2.0% down for a 10% rise in fuel) and shows no
  delayed move. A trading rule built on a delay lost money out of sample.
- For Q3 2026 the method puts fuel at $3.93 a gallon against the $3.69 United
  assumed and adjusted EPS at $2.54 (P10 $2.18, P90 $2.82) against a guided
  $2.50 to $3.50. Frozen 10 October 2026.

## What it did not show

- Replayed on the 10 guided quarters from 2024, marking United's midpoint to fuel
  missed by $0.26 a share on average, the same as the midpoint itself. The method
  did not beat United's own number, so the Q3 forecast is a test, not a claim.
- The recovery curve that was fixed before the test years lost to simple rules.
  The smoother curve now in the simulator was chosen afterwards and is flagged.
- No capacity response to fuel and no tradable signal were found.
- Not built from the Stage 1 proposal: per-market fitted demand and pricing,
  competitor responses inside the simulator, crew and weather constraints, and
  testing with analysts.

## Rules the build kept

- No number is invented. Every figure traces to a file with a SHA-256 or to a
  computation on such files; anything that could not be computed is shown as not
  computed.
- No look-ahead in anything called a forecast or a replay; tests enforce it.
- The language model never authors a number. In the agent it writes placeholders
  that code fills from labeled facts; in the guidance reader it can only choose
  among sentences that code found, and a verifier checks the quote.
- Results that failed are published beside the ones that held.

## Compliance

Only public, sourced and properly licensed data is used: U.S. EIA fuel prices,
SEC filings, TSA checkpoint counts, FRED consumer prices, Yahoo Finance daily
prices (an unofficial feed) and a CC BY 4.0 Kaggle dataset of 2022 Expedia quotes
by Dillon Wong. No employer-proprietary data, private investor material or
fabricated results are included. This is research software, not investment advice.
