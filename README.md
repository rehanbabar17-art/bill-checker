# Utility Bill Checker

Automated IESCO electricity bill checker with ntfy notifications.

## Features
- Checks configured IESCO accounts
- Detects new bills and payment status changes
- Sends ntfy notifications (name, ref no, amount, due date, paid/unpaid status)
- Includes itemized bill calculation (per-unit rate, fixed charges, fuel surcharge, taxes)
- Tracks bill history in `bill_state.json`

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
| `PROXY_URL` | Optional proxy for PITC (unreliable from cloud IPs) |

### Local run
Create a local `config.json` (git-ignored) with the same structure, then:
```
python3 check_bills.py
```

## Manual Run
Go to Actions → Check Utility Bills → Run workflow
