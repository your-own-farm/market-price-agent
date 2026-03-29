"""
market-price-agent
──────────────────
Claude agent that:
  1. Fetches today's APMC mandi prices from data.gov.in
  2. Reads previous prices from Firebase Realtime Database
  3. Computes trends, sell/hold signals, and enriches records
  4. Pushes the final dataset back to Firebase RTDB
     → rng-market frontend picks them up in real time via onValue()

Run:
  python agent.py

Environment variables required:
  ANTHROPIC_API_KEY
  DATA_GOV_API_KEY           (free key from data.gov.in)
  FIREBASE_DATABASE_URL      (e.g. https://your-project-default-rtdb.asia-southeast1.firebasedatabase.app)
  FIREBASE_SERVICE_ACCOUNT_JSON  (full JSON string of Firebase service account key)
"""

import json
import os
import sys

import anthropic
from tools import TOOL_SCHEMAS, fetch_mandi_prices, read_firebase_prices, push_prices

# ── Config ────────────────────────────────────────────────────────────────────

MODEL = "claude-sonnet-4-6"

# States to fetch prices for — extend as needed
TARGET_STATES = [
    "Maharashtra",
    "Punjab",
    "Uttar Pradesh",
    "Karnataka",
    "Gujarat",
    "Madhya Pradesh",
    "Andhra Pradesh",
    "Tamil Nadu",
    "Rajasthan",
    "Haryana",
]

SYSTEM_PROMPT = """You are a market intelligence agent for Indian agricultural commodities.

Your job every run:
1. Call read_firebase_prices to get prices already stored (use these as prev_price).
2. Call fetch_mandi_prices for the target states to get today's data.gov.in prices.
3. For each commodity record:
   - Use the modal_price as the current price (₹/quintal).
   - Look up the matching crop+district in the existing Firebase data to get prev_price.
     If no previous price exists, set prev_price = modal_price (0% change, stable).
   - Compute change_pct = (modal_price - prev_price) / prev_price * 100  (or 0 if prev == 0).
   - Set trend:
       "up"     if change_pct > 0.5
       "down"   if change_pct < -0.5
       "stable" otherwise
   - Set advice using this heuristic (you may apply light reasoning):
       "sell-now"  if trend == "up"   AND change_pct >= 5
       "hold"      if trend == "down" AND change_pct <= -3
       "watch"     otherwise
   - For crop name use the Commodity field; for unit always use "quintal".
4. Deduplicate by (state, district, commodity) — keep the record with the highest modal_price
   if the same commodity appears multiple times in the same market.
5. Filter out records with modal_price == 0.
6. Call push_prices with the final normalised list.
7. Report a brief summary: how many records fetched, how many pushed, top 5 biggest movers.

Be efficient. Minimise tool calls — batch everything into as few calls as possible."""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def dispatch(tool_name: str, tool_input: dict) -> str:
    if tool_name == "fetch_mandi_prices":
        result = fetch_mandi_prices(
            states=tool_input["states"],
            limit=tool_input.get("limit", 200),
        )
    elif tool_name == "read_firebase_prices":
        result = read_firebase_prices()
    elif tool_name == "push_prices":
        result = push_prices(tool_input["prices"])
    else:
        result = {"error": f"Unknown tool: {tool_name}"}

    return json.dumps(result, ensure_ascii=False)


# ── Agentic loop ──────────────────────────────────────────────────────────────

def run() -> None:
    client = anthropic.Anthropic()
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Fetch and push today's mandi prices for these states: "
                f"{', '.join(TARGET_STATES)}. "
                "Follow the instructions in your system prompt exactly."
            ),
        }
    ]

    print(f"[agent] Starting — model: {MODEL}")

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=8096,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract final text
            for block in response.content:
                if hasattr(block, "text"):
                    print("\n[agent] Final report:\n")
                    print(block.text)
            break

        if response.stop_reason != "tool_use":
            print(f"[agent] Unexpected stop_reason: {response.stop_reason}", file=sys.stderr)
            break

        # Execute tool calls and feed results back
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"[agent] → tool: {block.name}  input_keys: {list(block.input.keys())}")
            result_str = dispatch(block.name, block.input)
            preview = result_str[:200] + "…" if len(result_str) > 200 else result_str
            print(f"[agent] ← result preview: {preview}")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    for var in ("ANTHROPIC_API_KEY", "DATA_GOV_API_KEY",
                "FIREBASE_DATABASE_URL", "FIREBASE_SERVICE_ACCOUNT_JSON"):
        if not os.environ.get(var):
            sys.exit(f"[agent] Missing required env var: {var}")
    run()
