# Utility Bill Checker

Automated IESCO and SNGPL bill checker with ntfy notifications.

## Private data storage

The active storage is being migrated from Mega to Firebase Realtime Database. During migration, Mega remains available as a rollback source.

Firebase path: `bill-checker`

- `config` — machine-readable configuration with separate `iesco` and `sngpl` arrays.
- `state` — private bill history.
- `reference-guide` — labeled IESCO/SNGPL reference guide.

The legacy Mega folder is `github-data/bill-checker` and contains the equivalent private files.

## GitHub Secrets
| Secret | Purpose |
|---|---|
| `FIREBASE_SERVICE_ACCOUNT` | Firebase Admin SDK service-account JSON |
| `FIREBASE_DATABASE_URL` | Firebase Realtime Database URL |
| `MEGA_EMAIL` | Temporary rollback/migration source |
| `MEGA_PASSWORD` | Temporary rollback/migration source |
| `BILL_REFS` | Legacy configuration migration/bootstrap |
| `SNGPL_REFS` | SNGPL reference migration input |
| `NTFY_KEY` | ntfy topic/key for notifications |
| `PROXY_URL` | Proxy, or comma-separated proxy list, for bill-site requests |

## Migration

1. Run **Actions → Migrate Bill Data from Mega to Firebase**.
2. Confirm the workflow verifies the Firebase data.
3. Switch the regular **Check Utility Bills** workflow to Firebase.
4. Keep Mega secrets and files until Firebase has passed several scheduled runs.
5. Remove Mega only after rollback is no longer needed.

The migration copies the existing IESCO/SNGPL configuration, bill history, and labeled reference guide without changing the Mega source.
