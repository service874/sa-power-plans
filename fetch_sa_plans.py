#!/usr/bin/env python3
"""
fetch_sa_plans.py
-----------------
Pull EVERY residential electricity plan available in South Australia (SA Power
Networks distribution zone) from the Australian Energy Regulator's Consumer Data
Right (CDR) Energy Product Reference Data API, normalise it, and write a single
sa_plans.json that the dashboard (index.html) reads.

This is the same plan data that powers the government's Energy Made Easy site.
No authentication is required - these are public PRD endpoints.

Usage:
    python3 fetch_sa_plans.py                 # full run (all brands, SA plans, with rates)
    python3 fetch_sa_plans.py --fuel ELECTRICITY
    python3 fetch_sa_plans.py --no-detail     # fast: skip per-plan rate detail
    python3 fetch_sa_plans.py --max-per-brand 50
    python3 fetch_sa_plans.py --brands agl,origin-energy,amber

Output:
    sa_plans.json   (normalised, what the dashboard loads)
    sa_plans.csv    (flat table for spreadsheets / Xero / quick sorting)
    cache/<planId>.json  (raw plan detail cache so re-runs are fast + resumable)

Stdlib only. Python 3.8+.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

REGISTER_JSON = "https://raw.githubusercontent.com/jxeeno/energy-cdr-prd-endpoints/main/docs/energy-prd-endpoints.json"
EME_PLAN_DOC = "https://www.energymadeeasy.gov.au/plan?id="
CACHE_DIR = "cache"
USER_AGENT = "sa-plans-dashboard/1.0 (personal price comparison)"

# Fallback brand list (slug only) baked in case the live register fetch fails.
# The fetcher prefers the live register; this is only a safety net.
FALLBACK_BRAND_SLUGS = [
    "1st-energy", "agl", "alinta", "amber", "ampol", "arc-energy", "arcline",
    "aurora-energy", "blue-nrg", "covau", "cpe-mascot", "diamond-energy",
    "discover-energy", "dodo", "電-energy", "electricityinabox", "energy-locals",
    "energyaustralia", "engie", "flow-power", "future-x", "globird", "glow-power",
    "kogan", "localvolts", "lumo", "macarthur", "metered-energy", "momentum",
    "nectr", "next-business-energy", "origin-energy", "ovo-energy", "pacific-blue",
    "people-energy", "powerdirect", "powershop", "radian", "real-utilities",
    "red-energy", "reamped", "sanctuary", "savant", "shell-energy", "simply-energy",
    "smart-energy", "sumo", "tango", "telstra-energy", "winenergy",
]

SA_DISTRIBUTOR_HINTS = ("sa power", "sapn", "sa power networks")


def http_get_json(url, version=1, timeout=40, retries=3):
    """GET a CDR/JSON endpoint with the mandatory x-v header. Returns parsed dict or raises."""
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "x-v": str(version),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 406 = unsupported version: retry one version lower (detail supports >1, list = 1)
            if e.code == 406 and version > 1:
                version -= 1
                continue
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def load_brands(brand_filter=None):
    """Return list of (brand_name, base_uri, slug). Live register first, fallback baked-in."""
    brands = []
    try:
        data = http_get_json(REGISTER_JSON)["data"]
        for x in data:
            uri = x.get("productReferenceDataBaseUri")
            if not uri:
                continue
            slug = uri.rstrip("/").split("/")[-1]
            brands.append((x.get("brandName", slug), uri.rstrip("/"), slug))
        print(f"  register: {len(brands)} brands loaded live")
    except Exception as e:
        print(f"  register fetch failed ({e}); using baked-in fallback list")
        for slug in FALLBACK_BRAND_SLUGS:
            brands.append((slug, f"https://cdr.energymadeeasy.gov.au/{slug}", slug))

    if brand_filter:
        wanted = {b.strip().lower() for b in brand_filter}
        brands = [b for b in brands if b[2].lower() in wanted]
    return brands


def is_sa_plan(geography):
    """True if a plan's geography covers South Australia."""
    if not geography:
        return False
    dists = geography.get("distributors") or []
    for d in dists:
        if any(h in str(d).lower() for h in SA_DISTRIBUTOR_HINTS):
            return True
    for pc in (geography.get("includedPostcodes") or []):
        if str(pc).startswith("5"):
            return True
    return False


def fetch_brand_plans(base_uri, fuel):
    """Page through a brand's /energy/plans and return SA plan summaries."""
    out = []
    page = 1
    while True:
        url = f"{base_uri}/cds-au/v1/energy/plans?type=ALL&fuelType={fuel}&page-size=1000&page={page}"
        try:
            body = http_get_json(url, version=1)
        except Exception as e:
            print(f"    plans page {page} failed: {e}")
            break
        plans = (body.get("data") or {}).get("plans") or []
        for p in plans:
            if is_sa_plan(p.get("geography")):
                out.append(p)
        meta = body.get("meta") or {}
        total_pages = meta.get("totalPages") or 1
        if page >= total_pages or not plans:
            break
        page += 1
        time.sleep(0.2)
    return out


def fetch_detail(base_uri, plan_id, use_cache=True):
    """Fetch + cache a single plan's full detail."""
    safe = plan_id.replace("/", "_").replace("@", "_at_")
    path = os.path.join(CACHE_DIR, f"{safe}.json")
    if use_cache and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    url = f"{base_uri}/cds-au/v1/energy/plans/{plan_id}"
    detail = http_get_json(url, version=3)  # detail supports v>1; falls back on 406
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(detail, f)
    return detail


# ---------- normalisation helpers ----------

def _to_cents_per_kwh(unit_price):
    try:
        return round(float(unit_price) * 100, 4)
    except (TypeError, ValueError):
        return None


def _first_rate(rate_obj):
    rates = (rate_obj or {}).get("rates") or []
    if rates:
        return _to_cents_per_kwh(rates[0].get("unitPrice"))
    # some versions use generalUnitPrice
    return _to_cents_per_kwh((rate_obj or {}).get("generalUnitPrice"))


def normalise(summary, detail):
    """Collapse a CDR plan summary+detail into the flat shape the dashboard expects."""
    d = (detail or {}).get("data") or {}
    geo = d.get("geography") or summary.get("geography") or {}
    ec = d.get("electricityContract") or {}

    rec = {
        "plan_id": summary.get("planId") or d.get("planId"),
        "brand": summary.get("brand") or d.get("brand"),
        "brand_name": summary.get("brandName") or d.get("brandName"),
        "display_name": summary.get("displayName") or d.get("displayName") or "",
        "type": summary.get("type") or d.get("type"),            # MARKET / STANDING / REGULATED
        "fuel_type": summary.get("fuelType") or d.get("fuelType"),
        "distributors": geo.get("distributors") or [],
        "supply_cents_per_day": None,
        "tariff_type": None,                                     # SINGLE_RATE / TIME_OF_USE
        "single_rate_cents_per_kwh": None,
        "usage_rates": [],                                       # [{name,type,cents_per_kwh}]
        "controlled_load_cents_per_kwh": None,
        "feed_in_cents_per_kwh": None,
        "feed_in_detail": [],
        "discounts": [],
        "fees": [],
        "green_power": bool(ec.get("greenPowerCharges")),
        "contract_term_months": None,
        "pay_on_time_only": False,
        "plan_doc_url": EME_PLAN_DOC + (summary.get("planId") or ""),
    }

    # contract term
    terms = ec.get("contractTerms") or {}
    try:
        rec["contract_term_months"] = int(terms) if isinstance(terms, (int, str)) and str(terms).isdigit() else None
    except Exception:
        pass

    # supply charge + usage rates live in tariffPeriod[]
    periods = ec.get("tariffPeriod") or []
    for per in periods:
        # supply charge (string dollars/day)
        sc = per.get("dailySupplyCharges") or per.get("dailySupplyChargeType")
        if sc and rec["supply_cents_per_day"] is None:
            try:
                rec["supply_cents_per_day"] = round(float(sc) * 100, 3)
            except (TypeError, ValueError):
                pass

        block = per.get("rateBlockUType")
        if block == "singleRate" or per.get("singleRate"):
            c = _first_rate(per.get("singleRate"))
            if c is not None:
                rec["tariff_type"] = "SINGLE_RATE"
                rec["single_rate_cents_per_kwh"] = c
                rec["usage_rates"].append({"name": "Anytime", "type": "SINGLE", "cents_per_kwh": c})
        if block == "timeOfUseRates" or per.get("timeOfUseRates"):
            rec["tariff_type"] = "TIME_OF_USE"
            for tou in (per.get("timeOfUseRates") or []):
                c = _first_rate(tou)
                if c is not None:
                    rec["usage_rates"].append({
                        "name": tou.get("displayName") or tou.get("type") or "Usage",
                        "type": (tou.get("type") or "").upper(),
                        "cents_per_kwh": c,
                    })

    # controlled load (separate section in some plans)
    cl = ec.get("controlledLoad") or []
    if isinstance(cl, list):
        for c in cl:
            for per in (c.get("tariffPeriod") or []):
                val = _first_rate(per.get("singleRate"))
                if val is not None:
                    rec["controlled_load_cents_per_kwh"] = val
                    break

    # solar feed-in tariffs
    fits = ec.get("solarFeedInTariff") or []
    best_fit = None
    for fit in fits:
        utype = fit.get("tariffUType")
        amount = None
        if utype == "singleTariff" or fit.get("singleTariff"):
            st = fit.get("singleTariff") or {}
            if "amount" in st:
                amount = _to_cents_per_kwh(st.get("amount"))
            else:
                amount = _first_rate(st)
        elif utype == "timeVaryingTariffs" or fit.get("timeVaryingTariffs"):
            tv = fit.get("timeVaryingTariffs") or []
            cand = []
            for t in tv:
                v = _first_rate(t) or _to_cents_per_kwh((t or {}).get("amount"))
                if v is not None:
                    cand.append(v)
            amount = max(cand) if cand else None
        rec["feed_in_detail"].append({
            "scheme": fit.get("scheme"),
            "display_name": fit.get("displayName"),
            "payer_type": fit.get("payerType"),
            "cents_per_kwh": amount,
        })
        if amount is not None and (best_fit is None or amount > best_fit):
            best_fit = amount
    rec["feed_in_cents_per_kwh"] = best_fit

    # discounts (note name + any conditions)
    for disc in (ec.get("discounts") or []):
        rec["discounts"].append({
            "name": disc.get("displayName"),
            "type": disc.get("type"),
            "category": disc.get("category"),
            "method": disc.get("methodUType"),
        })
        cat = (disc.get("category") or "").upper()
        if "PAY" in cat or "ON_TIME" in cat:
            rec["pay_on_time_only"] = True

    # fees
    for fee in (ec.get("fees") or []):
        rec["fees"].append({
            "name": fee.get("term") or fee.get("type"),
            "type": fee.get("type"),
            "amount": fee.get("amount"),
        })

    return rec


def main():
    ap = argparse.ArgumentParser(description="Fetch all SA electricity plans from the AER CDR API.")
    ap.add_argument("--fuel", default="ELECTRICITY", choices=["ELECTRICITY", "GAS"])
    ap.add_argument("--no-detail", action="store_true", help="Skip per-plan rate detail (fast, no pricing).")
    ap.add_argument("--max-per-brand", type=int, default=0, help="Cap SA plans fetched per brand (0 = no cap).")
    ap.add_argument("--brands", default="", help="Comma-separated brand slugs to limit to (e.g. agl,origin-energy).")
    ap.add_argument("--sleep", type=float, default=0.15, help="Seconds between detail requests (politeness).")
    ap.add_argument("--out", default="sa_plans.json")
    args = ap.parse_args()

    brand_filter = [b for b in args.brands.split(",") if b.strip()] or None

    print("Loading retailer register...")
    brands = load_brands(brand_filter)
    print(f"Working through {len(brands)} brand(s).\n")

    all_records = []
    detail_calls = 0
    for i, (name, base, slug) in enumerate(brands, 1):
        print(f"[{i}/{len(brands)}] {name}")
        try:
            sa_summaries = fetch_brand_plans(base, args.fuel)
        except Exception as e:
            print(f"    brand failed: {e}")
            continue
        if args.max_per_brand:
            sa_summaries = sa_summaries[:args.max_per_brand]
        print(f"    {len(sa_summaries)} SA {args.fuel.lower()} plan(s)")

        for s in sa_summaries:
            plan_id = s.get("planId")
            detail = None
            if not args.no_detail and plan_id:
                try:
                    detail = fetch_detail(base, plan_id)
                    detail_calls += 1
                    if detail_calls % 25 == 0:
                        print(f"      ... {detail_calls} detail calls")
                    time.sleep(args.sleep)
                except Exception as e:
                    print(f"      detail {plan_id} failed: {e}")
            all_records.append(normalise(s, detail))

    # de-dupe by plan_id
    seen, deduped = set(), []
    for r in all_records:
        if r["plan_id"] and r["plan_id"] not in seen:
            seen.add(r["plan_id"])
            deduped.append(r)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "AER Consumer Data Right Energy PRD (cdr.energymadeeasy.gov.au) - same data as Energy Made Easy",
        "region": "South Australia (SA Power Networks)",
        "fuel": args.fuel,
        "gst_note": "Unit prices and supply charges are GST-inclusive, in cents.",
        "plan_count": len(deduped),
        "plans": deduped,
    }
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote {args.out}  ({len(deduped)} plans)")

    # flat CSV
    csv_path = os.path.splitext(args.out)[0] + ".csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["plan_id", "brand_name", "display_name", "type", "tariff_type",
                    "supply_c_per_day", "single_rate_c_per_kwh", "feed_in_c_per_kwh",
                    "controlled_load_c_per_kwh", "pay_on_time_only", "plan_doc_url"])
        for r in deduped:
            w.writerow([r["plan_id"], r["brand_name"], r["display_name"], r["type"],
                        r["tariff_type"], r["supply_cents_per_day"], r["single_rate_cents_per_kwh"],
                        r["feed_in_cents_per_kwh"], r["controlled_load_cents_per_kwh"],
                        r["pay_on_time_only"], r["plan_doc_url"]])
    print(f"Wrote {csv_path}")
    print("\nNow open index.html (it will load sa_plans.json automatically).")


if __name__ == "__main__":
    main()
