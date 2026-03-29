"""
Tool implementations for the market-price agent.

Tools exposed to Claude:
  fetch_mandi_prices   — pulls today's prices from data.gov.in CKAN API
  read_firebase_prices — reads the current /crop-prices node from RTDB
  push_prices          — writes normalized price records back to RTDB
"""

import os
import re
import time
import httpx
import firebase_admin
from firebase_admin import credentials, db as rtdb


# ── Firebase init (idempotent) ────────────────────────────────────────────────

def _init_firebase() -> None:
    if firebase_admin._apps:
        return
    sa_json = os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
    import json, tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(sa_json)
        sa_path = f.name
    cred = credentials.Certificate(sa_path)
    firebase_admin.initialize_app(cred, {
        "databaseURL": os.environ["FIREBASE_DATABASE_URL"]
    })


# ── Key normaliser ────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    """'Madhya Pradesh' → 'madhya_pradesh'"""
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


# ── Tool: fetch_mandi_prices ─────────────────────────────────────────────────
# data.gov.in resource: "Current Daily Price of Various Commodities from Various Markets"
# Resource ID: 9ef84268-d588-465a-a308-a864a43d0070

DATA_GOV_RESOURCE = "9ef84268-d588-465a-a308-a864a43d0070"
DATA_GOV_BASE     = "https://api.data.gov.in/resource"

def fetch_mandi_prices(states: list[str], limit: int = 200) -> dict:
    """
    Fetch today's mandi prices from data.gov.in for the given states.
    Returns a list of raw records with keys:
      state, district, market, commodity, variety,
      arrival_date, min_price, max_price, modal_price
    """
    api_key = os.environ["DATA_GOV_API_KEY"]
    records: list[dict] = []

    for state in states:
        url = (
            f"{DATA_GOV_BASE}/{DATA_GOV_RESOURCE}"
            f"?api-key={api_key}&format=json&limit={limit}"
            f"&filters[State]={state}"
        )
        try:
            resp = httpx.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("records", []):
                records.append({
                    "state":        r.get("State", "").strip(),
                    "district":     r.get("District", "").strip(),
                    "market":       r.get("Market", "").strip(),
                    "commodity":    r.get("Commodity", "").strip(),
                    "variety":      r.get("Variety", "").strip(),
                    "arrival_date": r.get("Arrival_Date", "").strip(),
                    "min_price":    _safe_int(r.get("Min_x0020_Price")),
                    "max_price":    _safe_int(r.get("Max_x0020_Price")),
                    "modal_price":  _safe_int(r.get("Modal_x0020_Price")),
                })
        except Exception as exc:
            records.append({"error": str(exc), "state": state})

    return {"count": len(records), "records": records}


def _safe_int(val) -> int:
    try:
        return int(str(val).replace(",", "").strip())
    except Exception:
        return 0


# ── Tool: read_firebase_prices ────────────────────────────────────────────────

def read_firebase_prices() -> dict:
    """
    Read the entire /crop-prices node from Firebase Realtime Database.
    Returns the raw nested dict {state_key → {district_key → {crop_key → record}}}.
    """
    _init_firebase()
    snapshot = rtdb.reference("/crop-prices").get()
    return snapshot or {}


# ── Tool: push_prices ─────────────────────────────────────────────────────────

def push_prices(prices: list[dict]) -> dict:
    """
    Write a list of normalised price records to Firebase Realtime Database.

    Each record must have:
      crop, state, district, market, price, prev_price, unit,
      trend ('up'|'down'|'stable'), change_pct, advice ('sell-now'|'hold'|'watch')

    Writes to: /crop-prices/{state_key}/{district_key}/{crop_key}
    """
    _init_firebase()
    root = rtdb.reference("/crop-prices")
    written = 0

    for p in prices:
        state_key    = _slug(p["state"])
        district_key = _slug(p["district"])
        crop_key     = _slug(p["crop"])

        root.child(state_key).child(district_key).child(crop_key).set({
            "crop":       p["crop"],
            "state":      p["state"],
            "district":   p["district"],
            "market":     p["market"],
            "price":      p["price"],
            "prevPrice":  p["prev_price"],
            "unit":       p.get("unit", "quintal"),
            "trend":      p["trend"],
            "changePct":  round(p["change_pct"], 2),
            "advice":     p["advice"],
            "updatedAt":  int(time.time() * 1000),
        })
        written += 1

    return {"written": written}


# ── Tool schema (passed to Claude) ────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "fetch_mandi_prices",
        "description": (
            "Fetch today's crop prices from India's data.gov.in CKAN API "
            "(resource: Current Daily Price of Various Commodities from Various Markets / Mandis). "
            "Returns raw records with modal, min, and max prices in ₹ per quintal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "states": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of state names exactly as used in Agmarknet, e.g. ['Maharashtra', 'Punjab', 'Uttar Pradesh']",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max records per state (default 200, max 500)",
                    "default": 200,
                },
            },
            "required": ["states"],
        },
    },
    {
        "name": "read_firebase_prices",
        "description": (
            "Read the current crop prices already stored in Firebase Realtime Database "
            "(/crop-prices). Use this BEFORE pushing new prices so you can compute "
            "the price change (trend) vs the previous value."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "push_prices",
        "description": (
            "Write normalised, enriched price records to Firebase Realtime Database. "
            "Each record must include: crop, state, district, market, price, prev_price, "
            "unit, trend ('up'|'down'|'stable'), change_pct (signed float), "
            "advice ('sell-now'|'hold'|'watch')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prices": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "crop":       {"type": "string"},
                            "state":      {"type": "string"},
                            "district":   {"type": "string"},
                            "market":     {"type": "string"},
                            "price":      {"type": "integer", "description": "Modal price ₹/quintal"},
                            "prev_price": {"type": "integer", "description": "Previous modal price (0 if first run)"},
                            "unit":       {"type": "string", "default": "quintal"},
                            "trend":      {"type": "string", "enum": ["up", "down", "stable"]},
                            "change_pct": {"type": "number", "description": "Signed % change vs prev_price"},
                            "advice":     {"type": "string", "enum": ["sell-now", "hold", "watch"]},
                        },
                        "required": ["crop", "state", "district", "market", "price",
                                     "prev_price", "trend", "change_pct", "advice"],
                    },
                }
            },
            "required": ["prices"],
        },
    },
]
