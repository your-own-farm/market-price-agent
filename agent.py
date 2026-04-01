"""
market-price-agent
──────────────────
Architecture:
  - Python fetches raw data from data.gov.in for each target state
  - Python normalises, deduplicates, and computes trends directly
  - Firebase RTDB is updated → rng-market frontend shows live prices

  AI-agent features (crop advice, profit maximisation) are planned for a
  future iteration and will be layered on top of this foundation.

Auth: Application Default Credentials (gcloud auth application-default login)
Run:  python agent.py
"""

import os, sys
from tools import fetch_mandi_prices, read_firebase_prices, push_prices, _slug

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_STATES = [
    "Maharashtra", "Punjab", "Uttar Pradesh", "Karnataka",
    "Gujarat", "Madhya Pradesh", "Andhra Pradesh",
    "Tamil Nadu", "Rajasthan", "Haryana",
]


# ── Price processing ──────────────────────────────────────────────────────────

def process_prices(raw_records: list[dict], prev_prices: dict) -> list[dict]:
    """
    Normalise, deduplicate, and enrich raw mandi records.

    - Deduplicates by (state, district, commodity) — keeps highest modal_price.
    - Drops records where modal_price is 0 or missing.
    - Computes change_pct, trend, and advice vs the previous Firebase snapshot.
    """
    # Deduplicate: keep record with highest modal_price per (state, district, commodity)
    deduped: dict[tuple, dict] = {}
    for r in raw_records:
        price = r.get("modal_price", 0)
        if not price:
            continue
        key = (_slug(r["state"]), _slug(r["district"]), _slug(r["commodity"]))
        if key not in deduped or price > deduped[key].get("modal_price", 0):
            deduped[key] = r

    enriched: list[dict] = []
    for (state_key, district_key, crop_key), r in deduped.items():
        modal_price = r["modal_price"]

        # Look up previous price from Firebase snapshot
        prev_record = (
            prev_prices
            .get(state_key, {})
            .get(district_key, {})
            .get(crop_key, {})
        )
        prev_price = prev_record.get("price", modal_price) if prev_record else modal_price

        if prev_price and prev_price > 0:
            change_pct = (modal_price - prev_price) / prev_price * 100
        else:
            change_pct = 0.0

        if change_pct > 0.5:
            trend = "up"
        elif change_pct < -0.5:
            trend = "down"
        else:
            trend = "stable"

        if trend == "up" and change_pct >= 5:
            advice = "sell-now"
        elif trend == "down" and change_pct <= -3:
            advice = "hold"
        else:
            advice = "watch"

        enriched.append({
            "crop":       r["commodity"],
            "state":      r["state"],
            "district":   r["district"],
            "market":     r["market"],
            "price":      modal_price,
            "prev_price": prev_price,
            "unit":       "quintal",
            "trend":      trend,
            "change_pct": round(change_pct, 2),
            "advice":     advice,
        })

    return enriched


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    # Step 1: Read existing Firebase prices (used to compute trends)
    print("[agent] Fetching existing Firebase prices...")
    prev_prices = read_firebase_prices()
    print(f"[agent] Firebase has {len(prev_prices)} state buckets")

    # Step 2: Fetch today's mandi prices from data.gov.in
    print(f"[agent] Fetching today's mandi prices for {len(TARGET_STATES)} states...")
    raw = fetch_mandi_prices(TARGET_STATES, limit=200)
    errors = [r for r in raw.get("records", []) if "error" in r]
    good   = [r for r in raw.get("records", []) if "error" not in r]
    print(f"[agent] Fetched {len(good)} records ({len(errors)} state error(s))")
    for err in errors:
        print(f"[agent] State error — {err.get('state')}: {err.get('error')}")

    if not good:
        sys.exit("[agent] No price data fetched — check DATA_GOV_API_KEY")

    # Step 3: Normalise, deduplicate, compute trends
    print("[agent] Processing prices...")
    processed = process_prices(good, prev_prices)
    print(f"[agent] Processed {len(processed)} unique crop-district records")

    # Step 4: Push to Firebase
    print("[agent] Pushing prices to Firebase...")
    result = push_prices(processed)
    print(f"[agent] Pushed {result['written']} records to Firebase RTDB")

    # Step 5: Print top movers summary
    movers = sorted(processed, key=lambda x: abs(x["change_pct"]), reverse=True)[:5]
    if movers:
        print("\n[agent] Top 5 movers:")
        for m in movers:
            print(
                f"  {m['crop']:20s}  {m['district']:20s}  {m['state']:15s}"
                f"  {m['change_pct']:+.1f}%  {m['advice']}"
            )


if __name__ == "__main__":
    for var in ("DATA_GOV_API_KEY", "FIREBASE_DATABASE_URL"):
        if not os.environ.get(var):
            sys.exit(f"[agent] Missing env var: {var}")
    if not os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
        print("[agent] Using Application Default Credentials for Firebase")
    run()
