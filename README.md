# SA Power Plans — electricity plan comparison dashboard

Pulls **every** residential electricity plan available on SA Power Networks (South
Australia) and ranks them by what they'd actually cost *your* home, modelled live
against your solar + battery setup.

No scraping. The data comes from the **AER's Consumer Data Right Energy API** — the
same plan data behind the government's [Energy Made Easy](https://www.energymadeeasy.gov.au)
site. Every licensed retailer is legally required to publish all their plans there.
Public endpoints, no API key.

## Files

| File | What it is |
|------|------------|
| `fetch_sa_plans.py` | Backend fetcher. Pulls + normalises all SA plans → `sa_plans.json` (+ `.csv`). |
| `index.html` | The dashboard. Reads `sa_plans.json`, does the solar/battery modelling in-browser. |
| `sa_plans.sample.json` | Illustrative sample data so the dashboard works before you run the fetcher. **Not real prices.** |

## Why two pieces

The CDR endpoints don't allow direct browser calls (CORS), and getting every plan
means thousands of polite requests with caching and retries — that needs a backend.
So: **Python fetches once → dashboard reads the file.** Fits a GitHub Pages workflow:
commit `sa_plans.json` next to `index.html` and the page is live.

## Run it

```bash
# 1. Get the data (first full run takes a while; results are cached + resumable)
python3 fetch_sa_plans.py

# faster options:
python3 fetch_sa_plans.py --no-detail          # plan list only, no pricing
python3 fetch_sa_plans.py --brands agl,origin-energy,amber,red-energy
python3 fetch_sa_plans.py --max-per-brand 30

# 2. View it (any static server — fetch() needs http, not file://)
python3 -m http.server 8000
# open http://localhost:8000
```

The fetcher writes `sa_plans.json`; the dashboard loads it automatically. Until then
it falls back to `sa_plans.sample.json` so you can explore the interface. You can also
use the **“load JSON…”** button top-right to point it at any plan file.

Re-running is cheap: every plan's detail is cached in `cache/`, so subsequent runs
only fetch what's new. Run it on a schedule (cron / GitHub Action) to keep prices fresh.

## How the cost model works

Per plan, per year:

```
annual = supply (c/day × 365) + grid-import energy − solar feed-in credit
```

- **Solar generation** = system kW × Adelaide yield (default ~1480 kWh/kW/yr).
- **Self-consumption** = how much solar you use on-site vs export (slider).
- **Battery** raises self-consumption — bounded by usable capacity, daytime export
  surplus, evening load and round-trip efficiency.
- **Time-of-use plans** split grid import across peak/shoulder/off-peak using a typical
  household profile.

Every assumption is a control at the top of the page and recalculates everything live.
The **scenario toggle** (No solar / Solar / Solar + battery) is the core comparison —
it shows, for each plan, how much your solar/battery actually changes the bill, and
which retailers reward export most (high feed-in tariff).

## Caveats

Estimates for comparison only, GST-inclusive — not a quote or financial advice.
The model uses each plan's published supply + usage + feed-in rates; it does **not**
fully cost demand charges, controlled-load split, sign-up credits or conditional
discounts (those are flagged but not always priced). Always confirm a plan on its
official document (linked in each plan's detail drawer) before switching.

Data source: AER Consumer Data Right Energy Product Reference Data.
Brand register: `jxeeno/energy-cdr-prd-endpoints` (hydrated from the ACCC CDR register).
