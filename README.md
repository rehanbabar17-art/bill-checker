# Utility Bill Checker

Automated IESCO electricity bill checker with ntfy notifications.

## Features
- Checks configured IESCO accounts using primary LumiProxy web proxy browser automation with automatic fallbacks
- Detects new bills, amount changes, and payment status changes
- Sends ntfy notifications with consumer name, reference number, bill month, amount, due date, and paid/unpaid status
- Extracts the detailed IESCO QR payload from the official PITC bill page
- Lists itemized charges: units, variable/fixed charges, meter/service rent, fuel surcharge, QTA, taxes, FPA, sanctioned load, and rate calculations
- Tracks the complete bill response in `bill_state.json`

## Security / Privacy
All account reference numbers are **secret** and never committed to this repo:
- On CI, refs are provided via the `BILL_REFS` GitHub Secret (JSON)
- Locally, refs live in a git-ignored `config.json`
- `bill_state.json` (bill history) is also git-ignored; on CI it is persisted via the Actions cache

## Setup

### GitHub Secrets
The workflow requires these repository secrets:
| Secret | Purpose |
|--------|---------|
| `BILL_REFS` | JSON with account list, e.g. `{"iesco":[{"name":"KhalaLower","ref":"123..."}]}` |
| `NTFY_KEY` | ntfy topic/key for notifications |
| `PROXY_URL` | Proxy (or comma-separated list) used for all bill-site requests |

### Proxying
The bill sites block many datacenter IP ranges, including GitHub Actions runners,
so every request to a bill site goes through a proxy when one is configured:

| Variable | Meaning |
|----------|---------|
| `PROXY_URL` / `PROXY_URLS` | One proxy URL, or several comma-separated; tried in order before a direct connection (`http://user:pass@host:port`, `socks5://...` with `requests[socks]`) |
| `PROXY_ONLY` | `1` to never fall back to a direct connection |

Standard `HTTPS_PROXY`/`HTTP_PROXY` are also honoured. Proxy credentials are
masked in logs.

### Local run
Install required dependencies (`requests`, `playwright` with Chromium browser):
```bash
pip install requests playwright
playwright install chromium
```
Create a local `config.json` (git-ignored) with the same structure, then:
```bash
python3 check_bills.py
```

### IESCO Fetching Chain
IESCO bills are fetched using a 3-tier fallback strategy:
1. **Primary**: Browser automation via Playwright navigating through LumiProxy (Pakistan location).
2. **Fallback 1**: Direct HTTP request to the official PITC bill site (`bill.pitc.com.pk`).
3. **Fallback 2**: Third-party aggregator (`onlinebill.com.pk`).

## Manual Run
Go to Actions → Check Utility Bills → Run workflow
