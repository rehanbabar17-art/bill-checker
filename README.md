# Utility Bill Checker

Automated IESCO and SNGPL bill checker with ntfy notifications.

## Private storage migration

The current production workflow remains on Mega while Backblaze B2 is staged and verified. The B2 bucket is private and will contain:

- `config.json` — IESCO/SNGPL configuration
- `bill_state.json` — bill history/state
- `REFERENCE_NUMBERS.txt` — labeled private guide

## Backblaze B2 secrets

Add these encrypted GitHub Actions secrets to `rehanbabar17-art/bill-checker`:

| Secret | Value |
|---|---|
| `B2_KEY_ID` | Replacement B2 key ID |
| `B2_APPLICATION_KEY` | Replacement B2 application key |
| `B2_BUCKET` | `GithubRepoSecretRB17` |
| `B2_ENDPOINT` | `https://s3.us-east-005.backblazeb2.com` |

The B2 key must have read/write access to the bucket. Keep the old Mega secrets until B2 passes migration, verification, and a full bill-check cycle. Revoke any previously exposed Backblaze application key.

## Migration steps

1. Create a replacement B2 application key and revoke the exposed old key.
2. Add the four B2 secrets above.
3. Run **Actions → Verify Backblaze B2 Storage** to test access.
4. Run **Actions → Migrate Bill Data from Mega to Backblaze B2**.
5. The migration reads `config.json` and the labeled guide from Mega. It does not require `bill_state.json` from Mega; existing B2 state is preserved, otherwise an empty B2 state object is initialized.
6. After verification, switch the hourly workflow to `b2:download` and `b2:upload`.
7. Keep Mega as rollback for several successful scheduled runs before removing it.
