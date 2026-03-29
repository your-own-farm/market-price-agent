# market-price-agent

AI agent powered by Claude that scrapes live APMC mandi prices from India's [data.gov.in](https://data.gov.in) and pushes them to Firebase Realtime Database — where the [rng-market](https://github.com/your-own-farm/rng-market) frontend picks them up in real time.

---

## How it works

```
GitHub Actions cron (every 30 min · Mon–Sat · 6 AM–8 PM IST)
  └─▶ agent.py  (Claude claude-sonnet-4-6 with tool use)
        ├─▶ read_firebase_prices     ← previous prices for trend calculation
        ├─▶ fetch_mandi_prices       ← data.gov.in CKAN API (10 states · JSON)
        │     Claude normalises, deduplicates, and derives:
        │       trend · change_pct · sell advice
        └─▶ push_prices              ← /crop-prices/{state}/{district}/{crop}
              └─▶ Firebase Realtime Database
                    └─▶ rng-market frontend (live via onValue)
```

### What Claude does

The agent is not a dumb ETL script. Claude:

- **Deduplicates** records (same commodity, same district, multiple varieties) keeping the best modal price
- **Computes trends** by comparing today's modal price against what is already stored in Firebase
- **Derives sell signals** — `sell-now`, `hold`, or `watch` — based on price momentum
- **Filters noise** (zero-price records, missing market names)
- **Reports** a summary of the run: records fetched, pushed, and the top 5 biggest movers

---

## Data source

**data.gov.in — Current Daily Price of Various Commodities from Various Markets (Mandi)**
- Resource ID: `9ef84268-d588-465a-a308-a864a43d0070`
- Format: JSON via CKAN API
- Auth: free API key (register at [data.gov.in](https://data.gov.in/user/register))
- Coverage: All states · APMC mandis · Modal / Min / Max price per quintal

---

## Firebase schema

Prices are written at:

```
/crop-prices
  /{state_key}           e.g. "maharashtra"
    /{district_key}      e.g. "nashik"
      /{crop_key}        e.g. "onion"
        crop:       "Onion"
        state:      "Maharashtra"
        district:   "Nashik"
        market:     "Lasalgaon APMC"
        price:      1260          ← modal price ₹/quintal
        prevPrice:  1310          ← previous run's price
        unit:       "quintal"
        trend:      "down"
        changePct:  -3.82
        advice:     "hold"
        updatedAt:  1748500000000 ← Unix ms
```

---

## Sell advice logic

| Signal | Condition |
|---|---|
| `sell-now` | trend is `up` AND change ≥ +5% |
| `hold` | trend is `down` AND change ≤ −3% |
| `watch` | everything else |

Claude applies light reasoning on top of this heuristic — e.g. it can recognise seasonal anomalies or unusually high arrivals that suggest a short-lived spike.

---

## States tracked

Maharashtra · Punjab · Uttar Pradesh · Karnataka · Gujarat · Madhya Pradesh · Andhra Pradesh · Tamil Nadu · Rajasthan · Haryana

Add more states in `agent.py → TARGET_STATES`.

---

## Project structure

```
market-price-agent/
├── agent.py          — agentic loop (Claude + tool use)
├── tools.py          — tool implementations + JSON schemas for Claude
├── requirements.txt
├── .env.example
└── .github/
    └── workflows/
        └── run.yml   — scheduled cron + manual dispatch
```

---

## Setup

### 1. Clone and install

```bash
git clone git@github.com:your-own-farm/market-price-agent.git
cd market-price-agent
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Where to get |
|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) |
| `DATA_GOV_API_KEY` | [data.gov.in → Register](https://data.gov.in/user/register) (free) |
| `FIREBASE_DATABASE_URL` | Firebase console → Realtime Database → Data tab |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Firebase console → Project Settings → Service Accounts → Generate new private key |

### 3. Run locally

```bash
export $(cat .env | xargs)
python agent.py
```

### 4. GitHub Actions (production)

Add the four variables above as **repository secrets** in `your-own-farm/market-price-agent → Settings → Secrets`.

The workflow runs automatically every 30 minutes during Indian market hours (Mon–Sat, 6 AM–8 PM IST). You can also trigger it manually from the **Actions** tab using `workflow_dispatch`.

---

## Firebase Realtime Database rules

Set these rules to allow the agent (server-side) to write and the frontend to read publicly:

```json
{
  "rules": {
    "crop-prices": {
      ".read": true,
      ".write": false
    }
  }
}
```

Write access is granted only via the service account used by this agent — not from the browser.

---

## Related

- [rng-market](https://github.com/your-own-farm/rng-market) — frontend that displays these prices live
- [your-roots](https://github.com/your-own-farm/your-roots) — main platform monorepo
