# Utility Bill Checker

Automated IESCO and SNGPL bill checker with ntfy notifications.

## Private Mega layout

The private Mega folder is `github-data/bill-checker` and contains:

- `config.json` — machine-readable configuration with separate top-level `iesco` and `sngpl` arrays.
- `REFERENCE_NUMBERS.txt` — human-readable guide with clear **IESCO REFERENCE NUMBERS** and **SNGPL REFERENCE NUMBERS** headers.
- `bill_state.json` — private bill history.

## Features
- Checks configured IESCO and SNGPL accounts
- Detects new bills, amount changes, and payment status changes
- Sends ntfy notifications with consumer name, reference number, bill month, amount, due date, and paid/unpaid status
- Extracts detailed IESCO charges and calculation data
- Stores private configuration and bill history in Mega

## Security / Privacy
Account reference numbers and bill history are never committed to this repository. They are downloaded from Mega before each check and the local private files are removed from the GitHub Actions runner afterward.

## GitHub Secrets
| Secret | Purpose |
|---|---|
| `MEGA_EMAIL` | Mega account email |
| `MEGA_PASSWORD` | Mega account password |
| `BILL_REFS` | JSON account configuration used for migration/bootstrap |
| `NTFY_KEY` | ntfy topic/key for notifications |
| `PROXY_URL` | Proxy, or comma-separated proxy list, for bill-site requests |

`BILL_REFS` uses separate arrays:

```json
{
  "iesco": [{"name": "IESCO Home", "ref": "IESCO_REFERENCE_NUMBER"}],
  "sngpl": [{"name": "SNGPL Home", "consumer": "SNGPL_CONSUMER_NUMBER"}]
}
```

## Separating references in Mega

Run **Actions → Separate IESCO and SNGPL References** after updating `BILL_REFS`. It merges new entries into Mega without deleting existing entries and refreshes `REFERENCE_NUMBERS.txt` with clearly labeled sections. It also preserves the existing notification key.

The regular **Check Utility Bills** workflow runs hourly and reads the separated arrays from Mega.

## First-time setup
1. Add the secrets above to the repository.
2. Run **Actions → Initialize Missing Mega Files** once.
3. Run **Actions → Separate IESCO and SNGPL References** whenever reference entries need to be merged or relabeled.
4. Run **Actions → Verify Mega Storage** to confirm all private files are valid.

## Local run
Create a git-ignored `config.json` with the same structure, then run:

```bash
python3 check_bills.py
```
