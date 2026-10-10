# UAL Counterfactual Lab

The legacy live demo is at [https://ual-twin.vercel.app](https://ual-twin.vercel.app); the technical report is at [https://ual-twin.vercel.app/report](https://ual-twin.vercel.app/report).

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

A simulation of United Airlines, with its behavior measured from history

What the simulator does, what was measured to build it, how it is checked against reality, and where it fails.

## Summary

This is a simulation of United Airlines under changes in fuel, demand and aircraft deliveries, with the airline's response measured from history, not assumed. Two measurements held up: the price United pays follows spot fuel about 18 days behind, and a year after a fuel cost change unit revenue has made up about three-fifths of it (90% range 49% to 91%). Four did not: the recovery curve specified in advance lost its held-back test, no capacity response could be told from zero, airport traveler counts barely track United's traffic, and the best trading rule lost money on later years. All are co-movements, not causal estimates. The simulator is checked by replaying past quarters, where it only ties United's own guidance midpoint, and by an estimate for United's next quarter, fixed before the result is known.

## The simulator

The simulator steps United's business forward month by month; the figure below shows its inputs and outputs. Each output is a range: the middle of many random runs, with a tenth of them beyond each bound.

Each scenario runs three times on the same random paths, changing only the behavior: no fare response, management's stated path, and the response measured from history. The application keeps the three questions of the Stage 1 proposal: what if, what breaks, and what must be true.

### What goes in

- Fuel price
- Travel demand
- Capacity flown
- Aircraft deliveries
- Random worlds, drawn once
- The business, run under three behaviors
  - No response
    - fares do not move
  - Management's stated path
    - the share it says it will recover
  - Measured from history
    - the recovery curve, with its uncertainty

### What comes out, as ranges

- Revenue
- Costs
- Operating margin
- Free cash flow
- Earnings per share

The same worlds go through all three, so a difference in the results is caused by behavior alone.

How a scenario runs.

### Data

Everything is public. Each raw download is stored with its hash, and every output lists its inputs. The years from 2024 were set aside as the test; the text says where a choice or a fit later used them.

Sources, coverage and checks.

| Source | What it gives | Coverage | How it was checked |
|---|---|---|---|
| United Airlines filings, SEC EDGAR | Quarterly operating and financial figures from earnings releases; guidance from investor updates | 54 quarters, Q1 2013 to Q2 2026; 159 documents read for guidance | Printed unit revenue equals revenue over seat miles to within a hundredth of a cent. Price times gallons equals fuel expense to within 0.3%. Every guidance figure is stored with its exact quote. |
| Delta Air Lines filings, SEC EDGAR | The same figures on Delta's adjusted basis; its July guidance and September results | Q4 2017 to Q3 2026 | 182 figures from the two latest releases, each tied to its quote in the document. |
| American Airlines: SEC financial data and Transportation Department traffic statistics | Revenue, fuel expense and capacity for the pooled estimate | Q1 2014 to Q2 2026 | Not reconciled to releases. Capacity is mainline only, and fuel before 2020 is rescaled to the later definition. |
| Energy Information Administration | Gulf Coast jet fuel, daily spot price | 4,190 trading days, January 4, 2010 to October 6, 2026 | Compared day by day with the copy published by the Federal Reserve Bank of St. Louis: no difference on 4,190 days. |
| Transportation Security Administration | Travelers screened at US airports, daily | 2,838 days, January 1, 2019 to October 8, 2026 | No day is missing. |
| Bureau of Labor Statistics, via the St. Louis Fed | Consumer price index for airline fares, monthly | Through August 2026; 84 months used to fit | 31 recent months compared with the Bureau's own service: no difference. October 2025 was never published and is left out, not filled. |
| Daily share prices, Yahoo Finance | United, Delta, American and Southwest; an airline fund; an S&P 500 fund; heating-oil futures | 3,714 sessions, January 3, 2012 to October 9, 2026 | Not cross-checked. The feed is unofficial and there is no second source. |
| Expedia fare quotes, 2022, collected by Dillon Wong | Quoted one-way fares by search date and flight date | 82,138,753 quotes on 171 search days, April 16, 2022 to October 5, 2022 | 2 search days are absent and 16 thin ones are flagged; nothing is filled in. 8,095,627 quotes enter the index. |

## What we measured

### Fuel price to fuel bill

United does not hedge fuel, so its bill follows the market: the price it reports each quarter tracks Gulf Coast spot about 18 days behind. Its range is too narrow: on 10 held-back quarters the typical miss was 9 cents, the reported price was above the prediction every time, and only 30% of results fell inside.

The fit, and two checks on the latest quarter

Spot price, daily
Price United reported paying, quarter average
$0.00
$1.00
$2.00
$3.00
$4.00
$5.00
2014
2016
2018
2020
2022
2024
2026
Spot
Paid
Spot jet fuel each day, and the price United reported paying each quarter.
Sources: Energy Information Administration; United's earnings releases.

### Fuel cost to revenue

A recovery curve is the share of a fuel cost change that unit revenue has made up, quarter by quarter.

The curve specified in advance lost its test: on 10 held-back quarters it missed by more than management's stated share used as a fixed rule, and more than full recovery at once, though the gaps are too small to tell from chance.

The curve now used has two parameters, the long-run share and the speed of reaching it. It was chosen after the test years were seen, so it has no clean test. Its share after one year, 62% (90% range 49% to 91%), is the best-determined number here; the speed is not determined.

Method, the speed, and industry fares

Measured from United's history, middle
Its 10–90% range
What management has said
No response (costs only)
0%
20%
40%
60%
80%
100%
0
+1
+2
+3
+4
Quarters after the fuel shock
Management, Q3 2026: 80–90%
Management, Q4 2026: 100%
United 62%
No response
Average miss on the held-back quarters, in cents per available seat mile. Lower is better.

| Rule | Four quarters ahead | One quarter ahead |
|---|---|---|
| No response | 1.14 | 0.79 |
| Full recovery in the same quarter | 0.59 | 0.76 |
| Management's statement, as a fixed rule | 0.56 | 0.69 |
| Curve specified in advance | 0.73 | 0.84 |
| Two-parameter curve, chosen after the test | 0.56 | 0.50 |

Share of a fuel cost change made up through revenue, by quarter after the change. The line is the middle of the resampled draws and the band holds the middle four-fifths of them.
Sources: United's earnings releases; Energy Information Administration.

### Capacity

No capacity response to fuel could be established. A year after a ten percent rise in fuel, United's capacity growth differs by +0.12 points, with a range (−0.11 to +0.35) that includes zero; a second specification disagrees, and the fitted response made held-back errors larger, so the simulator leaves capacity where the user sets it.

The two specifications

Change in capacity growth, middle estimate
Its 10–90% range
−0.20
0.00
+0.20
+0.40
0
+1
+2
+3
+4
Quarters after a 10% rise in jet fuel
Change in United's capacity growth after a rise in jet fuel.
Sources: United's earnings releases; Energy Information Administration.

### Travel demand

Travelers screened at US airports ran 2.8% below a year earlier in the third quarter of 2026. The count is a weak guide to United: over 9 clean quarters its growth has a correlation of 0.06 with growth in United's traffic, so the simulator shows it as context only.

Travelers a day, 7-day average

2.00m
2.50m
2.50m
Against a year earlier

−10.0%
−5.0%
0.0%
+5.0%
+10.0%
Jan2025
Apr
Jul
Oct
Jan2026
Apr
Jul
Oct
−2.0%
Travelers screened at US airports: the daily count and its change on a year earlier.
Source: Transportation Security Administration.

### The stock's reaction

In 2024 to 2026 a ten percent one-day rise in heating-oil futures, the traded stand-in for jet fuel, went with a fall of 2.0% in United's shares against the market the same day. No further move over the next 20 or 60 trading days can be told from zero, and the trading rule that did best on earlier years lost money afterward.

The trading test

Average move in the share price
Range of uncertainty
−10.0%
−5.0%
0.0%
+5.0%
+10.0%
+15.0%
0
+10
+20
+30
+40
+50
+60
Trading days from the fuel move
Fuel move
United's shares against the market around large moves in fuel.
Sources: Yahoo Finance daily prices; Energy Information Administration.

### Daily fares in the 2022 fuel spike

Timing inside a quarter can be seen in one episode, a daily index of fares quoted on Expedia in 2022: United's index peaked on June 1, 2022, after jet fuel did on April 28, 2022, and ended within 0.9% of its start. The evidence is weak: the window spans the summer travel peak, and in 6 of 12 specifications the response cannot be told from zero.

The estimate behind this

United
Delta, American and all airlines
Fare index

100
120
140
All airlines
Delta
American
United
Jet fuel, $ a gallon

$3.00
$4.00
$5.00
May2022
Jun
Jul
Aug
Sep
Oct
Daily fare indexes and jet fuel through the 2022 spike.
Sources: Expedia quotes collected by Dillon Wong; Energy Information Administration.

### Three airlines pooled

Pooling United, Delta and American confirms that revenue responds but not by how much: the pooled share after a year is 85% (90% range 49% to 104%), no narrower than United's own. It is a cross-check, not an input.

Least-squares estimate
Range of the resamples, 5% to 95%
The three airlines pooled
0%
20%
40%
60%
80%
100%
120%
Share of a fuel cost change made up after one year
United
Delta
American
All pooled
No response
Full recovery
75% (49% to 99%)
177% (−157% to 494%)
78% (38% to 105%)
85% (49% to 104%)
Share of a fuel cost change made up after one year, by airline and pooled. The 68 airline-quarters share one fuel price and are worth about 16 independent ones.
Sources: airline filings with the SEC; Transportation Department traffic statistics; Energy Information Administration.

## Does it match reality

### Replay of past quarters

The full simulator cannot be rerun in past years, so the replay uses its chain: United's guidance midpoint, less the fuel cost that arrived after guidance and that the recovery curve says does not come back within the quarter. On the 10 guided quarters from 2024 it missed reported earnings by $0.26 a share on average, the same as the midpoint itself and as a rule with no recovery: a three-way tie. The method did not beat United's own number. Its range held 80% of results, but a midpoint given the same range scores no worse, and its gap to the midpoint did not predict the share move on results day.

The fuel step is the one clear positive result: at quarter end the chain's fuel price was on average 6 cents a gallon from the price United later reported; United's own stated price was 25 cents away.

How the chain was chosen, and the results-day test

United's guidance range
Middle of the guidance
No response (costs only)
As management says
As history shows, with its 10–90% range
What was reported
−$1.00
$0.00
$1.00
$2.00
$3.00
$4.00
$5.00
Adjusted earnings per share
2026 Q2
2026 Q1
2025 Q4
2025 Q3
2025 Q2
2025 Q1
2024 Q4
2024 Q3
2024 Q2
2024 Q1
Scores on the held-back quarters. Lower is better in both columns. The range score is the continuous ranked probability score, fixed in advance: it rewards a range for being narrow and right.

| Estimate | Average miss, dollars a share | Range score | Quarters |
|---|---|---|---|
| Measured response | $0.26 | 0.16 | 10 |
| Midpoint of United's guidance | $0.26 | 0.26 | 10 |
| The midpoint, given the same range | $0.26 | 0.15 | 10 |
| No response | $0.26 | 0.26 | 10 |
| Management's stated path | $0.17 | 0.17 | 1 |

Held-back quarters replayed: guidance, each rival's estimate, and what was reported. Each forecast is made at the quarter's last day, about three weeks before results.
Sources: United's guidance and earnings releases.

### Width of the ranges

The simulator's ranges were checked for width. With the assumed demand noise, the 80% range held 25 of 33 unit-revenue outcomes in 2022 to 2026 (76%) and 28 of 33 operating-margin outcomes (85%). It held every outcome before 2020, so it is too wide for calm markets. A value fitted on those years held only 61% later and was set aside.

How the check works, and what sets the width

### Delta's September quarter

Delta reported its September quarter on October 9, 2026, ahead of United. Fuel cost it $3.61 a gallon against a planned $3.15; unit revenue rose 15%, and adjusted earnings per share still came in at $1.72, below the guided $2.00 to $2.50.

The replay chain put Delta's quarter at $1.88 with United's measured share and $1.65 with none, so the no-recovery rule was closer.

### An estimate fixed before United reports

On July 15, 2026 United guided the third quarter of 2026 to adjusted earnings of $2.50 to $3.50 a share, planning on fuel at $3.69 a gallon. Our estimate, and its fingerprint, were fixed before United reports on October 20, 2026; it is a test of the method, not a claim that it beats United's number. With fuel at $3.93, it takes the $3.00 midpoint, subtracts $0.46 of fuel cost it expects not to be recovered, and gives $2.54 a share (range $2.18 to $2.82). Applying each of United's 14 earlier misses against its midpoint, 50% of outcomes fall below the guided range, 43% inside and 7% above.

The estimate's record, and the analyst consensus

The fixed estimate, link by link. Lower and upper leave a tenth of the values on each side. The unit revenue link is a side estimate and does not feed the earnings figure. United never defined its recovery figure, so it is not the same measure as the share beside it.

| Link | Lower | Middle | Upper | Management |
|---|---|---|---|---|
| Fuel price paid, a gallon | $3.87 | $3.93 | $4.00 | $3.69 |
| Share of the fuel surprise recovered within the quarter | 6% | 21% | 27% | 80% to 90% |
| Unit revenue, change on a year earlier | 11.1% | 14.7% | 17.5% | more than 12.1% |
| Adjusted earnings per share | $2.18 | $2.54 | $2.82 | $2.50 to $3.50 |

The estimate day by day

What would count as being wrong

A single miss settles nothing: one result in five should fall outside the range. The measured response is wrong if over several quarters it scores worse than the guidance midpoint or results keep landing outside its range. So far it only ties the midpoint, and the fuel-bill range already fails the second test.

## The agent

An analyst in the application plans, runs tools and writes. The language model never types a number: tool results reach it as labeled facts, it writes placeholders, and code puts the values in.

A second component reads guidance from United's filings and verifies each quoted value against the document: 159 documents gave 509 verified facts.

### What happens when a check fails

**Limits**

- Small samples. The recovery curve rests on 30 quarters, and its ranges assume the model is right.
- Co-movement, not cause. Fuel and travel demand move with the economy, so part of every measured response is demand.
- Few shocks. The fuel move of 2026 is larger than any the curve was first fitted on.
- Symmetry is assumed. A fall in fuel is treated as the mirror of a rise, and the one test had little power.
- Range width comes mostly from fuel volatility over the 65 trading days before the cutoff, 2.3 times that of 2013 to 2019.
- The simulator has no seasonality.
- The cash model is simplified. Advance ticket sales, debt service and profit sharing are not modeled.
- Shares of runs are not probabilities. They describe the model, not the chance of an event.

**Scaling**

Adding an airline takes its quarterly figures, its guidance, and a note of where its accounts differ. Delta was the trial: the same code fetched and indexed its filings, but its results needed their own reader, and only its adjusted figures compare with United's. Daily fares beyond 2022 and bookings, neither public, would improve it most.

## References and credits

Sources read for this project, each checked against its text. The list is generated from the project's literature file.

**Published research**

- Bailey, D. H.; Lopez de Prado, M. (2014). The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality. Journal of Portfolio Management 40(5), 94-107 (read as the July 31, 2014 author version).
- Carter, D. A.; Rogers, D. A.; Simkins, B. J. (2006). Does Hedging Affect Firm Value? Evidence from the US Airline Industry. Financial Management 35(1), 53-86.
- Chuang, S.-H. (2020). Cost pass-through in the airline industry: price responses and asymmetries. Economics Bulletin 40(1), 639-652.
- Dichev, I. D.; Huang, X.; Lee, D. K. K.; Zhao, J. (2023). You have a point - but a point is not enough: The case for distributional forecasts of earnings. Working paper draft dated November 15, 2023 (earlier version of SSRN 4547337).
- Dichev, I. D.; Huang, X.; Lee, D. K. K.; Zhao, J. (2023). Estimating and Using Distributional Forecasts of Earnings. SSRN working paper 4547337 (posted 27 August 2023, last revised 6 May 2025).
- Dong, C. (2024). Studies on the Aviation Industry Based on Industrial Organization, Part I: Cost Pass-through in the U.S. Aviation Industry. Ph.D. thesis, University of Ottawa (the chapter the 2025 JATM article is based on).
- Dong, C.; Atallah, G.; Galdo, J. (2025). Cost pass-through in the U.S. aviation industry. Journal of Air Transport Management 127, 102823.
- Driesprong, G.; Jacobsen, B.; Maat, B. (2008). Striking oil: Another puzzle? Journal of Financial Economics 89(2), 307-327.
- Gayle, P. G.; Lin, Y. (2021). Cost Pass-through in Commercial Aviation: Theory and Evidence. Economic Inquiry 59(2), 803-828 (read as MPRA Paper 102018, deposited 2020).
- Gneiting, T.; Raftery, A. E. (2007). Strictly Proper Scoring Rules, Prediction, and Estimation. Journal of the American Statistical Association 102(477).
- Harvey, C. R.; Liu, Y.; Zhu, H. (2016). ... and the Cross-Section of Expected Returns. The Review of Financial Studies 29(1), 5-68.
- Hong, H.; Torous, W.; Valkanov, R. (2007). Do industries lead stock markets? Journal of Financial Economics 83(2), 367-396 (read as the December 2005 draft).
- Jiang, Y.; Que, T. (2022). Asymmetric Pricing and Airline Performance. Applied Finance Letters 11.
- Jiang, Y.; Que, T.; Yu, M. (2022). Price asymmetries in the US airline industry. The Financial Review 57(4), 793-814.
- Koopmans, C.; Lieshout, R. (2016). Airline cost changes: To what extent are they passed through to the passenger? Journal of Air Transport Management 53, 1-11.
- Kristjanpoller, W. D.; Concha, D. (2016). Impact of fuel price fluctuations on airline stock returns. Applied Energy 178, 496-504.
- Pal, D.; Mitra, S. K. (2022). Do airfares respond asymmetrically to fuel price changes? A multiple threshold nonlinear ARDL model. Energy Economics 111.
- Wadud, Z. (2015). Imperfect reversibility of air transport demand: Effects of air fare, fuel prices and price transmission. Transportation Research Part A: Policy and Practice 72, 16-26 (read as the accepted manuscript).
- Wang, H.; Gao, X. (2020). Oil price dynamics and airline earnings predictability. Journal of Air Transport Management 87, 101854.
- Wolter, A. H.; Ehlers, T.; Luetjens, K.; Gollnick, V. (2021). Commodity price pass-through in the US airline industry and the hidden perks of consolidation. Journal of Air Transport Management 95, 102100.

**Filings, transcripts and analyst notes**

- Investing.com (Sam Boughedda), reporting a UBS note by Atul Maheswari (2026). UBS cuts price targets on airline stocks amid 'significant uncertainty'. Investing.com article republished on Yahoo Finance Canada, March 16, 2026.
- UBS (analyst Atul Maheswari), as quoted in a news article (2026). UBS Eyes Possible Bottom In Airline Stocks After Bear Market. Longbridge news page dated 2026-03-17 (the same text appears on freedombunker.com with that date).
- United Airlines Holdings, Inc. (2026). First-quarter 2026 earnings news release, April 21, 2026 (Exhibit 99.1 to Form 8-K). SEC EDGAR, accession 0000100517-26-000089.
- United Airlines Holdings, Inc. (2026). Investor Update, issue date April 21, 2026 (Exhibit 99.2 to Form 8-K). SEC EDGAR, accession 0000100517-26-000089.
- United Airlines Holdings, Inc. (2026). Quarterly Report on Form 10-Q for the quarter ended June 30, 2026 (filed 2026-07-16), Item 2 passages. SEC EDGAR, accession 0000100517-26-000139 (Item 2 read on a mirror).
- United Airlines Holdings, Inc. (2026). Second-quarter 2026 earnings news release, July 15, 2026 (Exhibit 99.1 to Form 8-K). SEC EDGAR, accession 0000100517-26-000135.
- United Airlines Holdings, Inc. (2026). Investor Update, issue date July 15, 2026 (Exhibit 99.2 to Form 8-K). SEC EDGAR, accession 0000100517-26-000135.
- United Airlines Holdings, Inc. (2026). Annual Report on Form 10-K for the fiscal year ended December 31, 2025 (filed 2026-02-12). SEC EDGAR, accession 0000100517-26-000023.
- United Airlines Holdings, Inc. (S. Kirby, A. Nocella, M. Leskinen) (2026). First-quarter 2026 earnings conference call, April 22, 2026 (transcript). The Motley Fool transcript.
- United Airlines Holdings, Inc. (S. Kirby, A. Nocella, M. Leskinen) (2026). Second-quarter 2026 earnings conference call, July 16, 2026 (transcript). AlphaStreet transcript.

**Credits**

- Filings. United, Delta and American documents are public filings on SEC EDGAR.
- Government statistics. Jet fuel prices are from the Energy Information Administration, traveler counts from the Transportation Security Administration, the airline fare index from the Bureau of Labor Statistics, and airline traffic from the Bureau of Transportation Statistics. These are works of the US government in the public domain. Two series were retrieved through FRED, the data service of the Federal Reserve Bank of St. Louis.
- Share prices. Daily prices are from Yahoo Finance's chart service, an unofficial feed, used here for research only.
- Fare quotes. The 2022 quotes are from Flight Prices by Dillon Wong, licensed CC BY 4.0. The uploader states the source as Expedia. Only index values built from the quotes are published here, never the quotes.
- Interface. Navigation adapted from Rare UI by Swami Malode: the section sidebar on this page from its Hook Sidebar (license), and the view switcher in the application from its Gooey Nav (license).
- Charts and type. Charts are drawn with Observable Plot and D3 (license, license). The typefaces are Geist and Geist Mono by Vercel (license). All are served from this site; nothing is loaded from elsewhere. Versions are listed in the vendor file.
