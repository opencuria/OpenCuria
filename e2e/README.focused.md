# Focused plugin/credential regression E2E

Requirements: `backend/.venv`, `webapp/node_modules`, `e2e/node_modules`, and Playwright Chromium (`npx playwright install chromium` when not already installed).

Run the isolated six-scenario UI + live API regression suite:

```bash
cd /workspace/OpenCuria
e2e/run-focused-plugin-credentials.sh
```

The script creates an empty SQLite database under `e2e/test-results/focused-db`, applies migrations, seeds its own organization, admin/member users, fixture runner, ready image artifact, Notion OAuth service/plugin, and workspace, then removes run-owned records. It does not require or mutate the app's Local org/database. Set `E2E_SEED_DB=/path/to/disposable.sqlite3` only to copy an explicit disposable seed database. Ports 8001/5174 use strict binding and process-group cleanup.

The test-only ASGI module answers only pinned metadata/registration/token requests for the fixture OAuth provider. Playwright intercepts the fixture authorization page and redirects the real API callback with its actual state/code and browser-binding cookie. No production provider or app REST API calls are mocked. Never deploy the fixture module.

Artifacts default to `e2e/test-results/focused-<run-id>`; set `E2E_OUTPUT_DIR` to choose another directory. Review screenshots default to `/workspace/.opencuria/playwright`; set `E2E_SCREENSHOTS_DIR` to choose another directory.
