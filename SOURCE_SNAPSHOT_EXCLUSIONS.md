# Source snapshot exclusions

The following categories are intentionally excluded from the review branch:

- `.git/**` — repository internals
- `.env`, `.env.*`, `**/*.env` — raw environment and secret files
- `runtime/**` — operating database, bindings, and run records
- `**/*.sqlite`, `**/*.sqlite3`, `**/*.db` — databases
- `**/__pycache__/**`, `.pytest_cache/**` — generated caches
- `.deps/**` — local installed dependency cache
- `05_QA_EVIDENCE/**` — screenshots, execution evidence, and TEST_ONLY dumps
- `*.log`, `*.jsonl` — logs and session/run histories
- `cookies/**`, `sessions/**` — browser/auth sessions
- `credentials/**`, `secrets/**` — credential material
- Customer and other operating business data

Only source, tests, dependency definitions, the example service definition, and
related design/control documents are part of this snapshot.
