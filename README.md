# Utility Bill Checker

Automated IESCO and SNGPL bill checker with ntfy notifications.

## Private storage migration

Backblaze B2 is now the active production storage. Mega remains configured as a rollback and migration source until B2 has completed several scheduled cycles. The shared B2 bucket uses the dedicated `bill-checker/` folder for this repository:

- `bill-checker/config.json` — IESCO/SNGPL configuration
- `bill-checker/bill_state.json` — bill history/state
- `bill-checker/REFERENCE_NUMBERS.txt` — labeled private guide

The checker reports an explicit **PAID** or **UNPAID** status for each IESCO bill and includes that status in ntfy notifications. If the bill page does not show a paid marker, a bill with payable data is reported as **UNPAID**.

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

For the existing installation, run **Actions → Organize Bill Checker B2 Files** once. It copies the verified root objects into `bill-checker/`, verifies them, and then removes the old root copies. Parcel-tracker uses its separate `parcel-tracker/` folder.

## Resetting bill history

Run **Actions → Reset B2 Bill History** to replace only `bill_state.json` with an empty object. It preserves the IESCO/SNGPL configuration and reference guide. The next bill-check run treats all fetched bills as new and sends a fresh ntfy notification for each detected bill.

## Verified test run

After resetting B2 history, the B2-backed checker was run successfully:

- 8 IESCO accounts were processed with explicit `UNPAID` status output.
- 5 SNGPL bills were fetched.
- 12 fresh bill changes were sent through ntfy.
- Updated configuration and bill state uploaded successfully to B2.
- Local private files were removed from the GitHub Actions runner.

One IESCO account returned no bill data during the test, so the run reported one data error. This is an upstream bill-page/fetch limitation, not a B2 or ntfy failure; the workflow remains successful and will retry it on the next hourly run.

## Folder persistence test

After moving the objects into `bill-checker/`, bill history was reset and the checker was run twice:

- Run 1 restored **0 bill-state records**, fetched 8 IESCO and 5 SNGPL accounts, sent 12 ntfy updates, and uploaded **12** state records to `bill-checker/bill_state.json`.
- Run 2 restored those **12 bill-state records**, completed successfully, uploaded the state again, and generated **0 new updates**. This confirms the server-side folder and history persistence are working.

Each run writes to the same B2 object keys under `bill-checker/`. Backblaze may display prior uploads as file versions for recovery, but they are not separate application files; the current/latest object is what the workflow reads.
