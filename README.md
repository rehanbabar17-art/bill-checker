# Utility Bill Checker

Automated IESCO and SNGPL bill checker with ntfy notifications.

## Features
- Checks configured IESCO and SNGPL accounts
- Detects new bills, amount changes, and payment status changes
- Sends ntfy notifications with consumer name, reference number, bill month, amount, due date, and paid/unpaid status
- Extracts detailed IESCO charges and calculation data
- Stores private configuration and bill history in Mega under `github-data/bill-checker`

## Security / Privacy
Account reference numbers and bill history are private and are never committed to this repository.

- `config.json` and `bill_state.json` are downloaded from Mega before each check.
- `BILL_REFS` remains a GitHub Secret only as a one-time migration/bootstrap fallback.
- After the first successful run, `config.json` in Mega is the source of truth.
- Local private files are securely removed from the GitHub Actions runner after each run.

## GitHub Secrets
| Secret | Purpose |
|---|---|
| `MEGA_EMAIL` | Mega account email |
| `MEGA_PASSWORD` | Mega account password |
| `BILL_REFS` | Existing JSON account configuration, used only for first-time Mega initialization/migration |
| `NTFY_KEY` | ntfy topic/key for notifications |
| `PROXY_URL` | Proxy, or comma-separated proxy list, for bill-site requests |

`BILL_REFS` uses the existing structure, for example:

```json
{"iesco":[{"name":"KhalaLower","ref":"123..."}],"sngpl":[{"name":"Gas","consumer":"456..."}]}
```

## First-time setup
1. Add the secrets above to the repository.
2. Run **Actions → Initialize Missing Mega Files** once. It creates `github-data/bill-checker/config.json` and an empty `bill_state.json` without overwriting existing files.
3. Run **Actions → Verify Mega Storage** to confirm authentication and file validity.
4. Run **Actions → Check Utility Bills**, or let the hourly schedule run.

The regular workflow also supports one-time migration automatically: if the Mega config is absent, it validates `BILL_REFS`, uses it for that run, and uploads it to Mega after a successful bill check.

## Proxying
The bill sites may block datacenter IP ranges. `PROXY_URL` / `PROXY_URLS` can contain one proxy URL or several comma-separated URLs. `PROXY_ONLY=1` can be set as a repository variable to disable direct fallback.

## Local run
Create a git-ignored `config.json` with the same structure, then run:

```bash
python3 check_bills.py
```

To test Mega synchronization locally, set `MEGA_EMAIL` and `MEGA_PASSWORD`, then run `npm ci` followed by one of:

```bash
npm run mega:download
npm run mega:upload
npm run mega:verify
```
