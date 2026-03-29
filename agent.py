"""
market-price-agent  (google-genai + Vertex AI edition)
───────────────────────────────────────────────────────
Architecture:
  - Python directly fetches raw data (data.gov.in + Firebase)
  - Gemini 2.5 Flash receives the raw data and does the intelligent
    normalisation, dedup, trend analysis, and calls push_prices
  - Firebase RTDB is updated → rng-market frontend shows live prices

Auth: Application Default Credentials (gcloud auth application-default login)
Run:  python agent.py
"""

import json, os, sys
from google import genai
from google.genai import types
from tools import (
    PUSH_TOOL_GENAI,
    fetch_mandi_prices, read_firebase_prices, push_prices,
)

# ── Config ────────────────────────────────────────────────────────────────────

GCP_PROJECT  = os.environ.get("GCP_PROJECT", "your-roots-6874d")
GCP_LOCATION = "us-central1"
MODEL        = "publishers/google/models/gemini-2.5-flash"

TARGET_STATES = [
    "Maharashtra", "Punjab", "Uttar Pradesh", "Karnataka",
    "Gujarat", "Madhya Pradesh", "Andhra Pradesh",
    "Tamil Nadu", "Rajasthan", "Haryana",
]

SYSTEM_PROMPT = """You are a market intelligence agent for Indian agricultural commodities.

You will receive:
- RAW_PRICES: today's records from data.gov.in (modal_price, min_price, max_price in Rs/quintal)
- PREV_PRICES: existing Firebase data keyed as {state_key}/{district_key}/{crop_key}

Your task:
1. For each record in RAW_PRICES:
   - current price = modal_price
   - Find matching entry in PREV_PRICES using lowercased state/district/commodity as keys
   - prev_price = that entry's "price" field (use modal_price if not found)
   - change_pct = (modal_price - prev_price) / prev_price * 100  (0 if prev_price is 0)
   - trend: "up" if change_pct > 0.5, "down" if < -0.5, else "stable"
   - advice: "sell-now" if trend=up AND change_pct >= 5, "hold" if trend=down AND change_pct <= -3, else "watch"
   - unit = "quintal"
2. Deduplicate by (state, district, commodity) — keep the record with the highest modal_price.
3. Exclude records where modal_price == 0 or missing.
4. Call push_prices with the final normalised list.
5. After pushing, respond with a text summary:
   - Total records received
   - Records pushed
   - Top 5 biggest movers (crop, district, state, change_pct, advice)"""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def dispatch(name: str, args: dict) -> str:
    if name == "push_prices":
        result = push_prices(args["prices"])
    else:
        result = {"error": f"Unknown tool: {name}"}
    return json.dumps(result, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> None:
    # Step 1: Fetch data directly in Python (reliable, no AI needed here)
    print("[agent] Fetching existing Firebase prices...")
    prev_prices = read_firebase_prices()
    print(f"[agent] Firebase has {len(prev_prices)} state buckets")

    print(f"[agent] Fetching today's mandi prices for {len(TARGET_STATES)} states...")
    raw = fetch_mandi_prices(TARGET_STATES, limit=200)
    errors = [r for r in raw.get("records", []) if "error" in r]
    good   = [r for r in raw.get("records", []) if "error" not in r]
    print(f"[agent] Fetched {len(good)} records ({len(errors)} state errors)")

    if not good:
        sys.exit("[agent] No price data fetched — check DATA_GOV_API_KEY")

    # Step 2: Hand off to Gemini for processing + push
    client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=[PUSH_TOOL_GENAI])],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode="AUTO")
        ),
        temperature=0,
    )

    user_msg = (
        f"RAW_PRICES ({len(good)} records):\n{json.dumps(good, ensure_ascii=False)}\n\n"
        f"PREV_PRICES:\n{json.dumps(prev_prices, ensure_ascii=False)}\n\n"
        "Process these prices and call push_prices."
    )

    print(f"[agent] Sending to Gemini ({MODEL}) for processing...")
    history: list[types.Content] = [
        types.Content(role="user", parts=[types.Part(text=user_msg)])
    ]

    while True:
        response = client.models.generate_content(
            model=MODEL, contents=history, config=config
        )
        candidate = response.candidates[0]
        if not candidate.content or not candidate.content.parts:
            print(f"[agent] Empty response — finish: {candidate.finish_reason}")
            break

        history.append(candidate.content)
        fn_calls = [p for p in candidate.content.parts if p.function_call]

        if not fn_calls:
            for part in candidate.content.parts:
                if part.text:
                    print("\n[agent] Summary:\n")
                    print(part.text)
            break

        result_parts = []
        for part in fn_calls:
            fc = part.function_call
            args = dict(fc.args) if fc.args else {}
            n = len(args.get("prices", []))
            print(f"[agent] >> push_prices({n} records)")
            result_str = dispatch(fc.name, args)
            print(f"[agent] << {result_str}")
            result_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result_str},
                )
            ))
        history.append(types.Content(role="user", parts=result_parts))


if __name__ == "__main__":
    for var in ("DATA_GOV_API_KEY", "FIREBASE_DATABASE_URL"):
        if not os.environ.get(var):
            sys.exit(f"[agent] Missing env var: {var}")
    if not os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON"):
        print("[agent] Using Application Default Credentials for Firebase")
    run()
