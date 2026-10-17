# Mobile: isolated live-backend / Socket.IO E2E fixture

This fixture is designed for the Android/Expo app, not a mocked client test. It
runs the real Django ASGI application, REST API, JWT login, organization and
workspace authorization checks, and the production Socket.IO `/frontend`
namespace with a process-local in-memory manager. Harness run admission,
stream persistence, workspace subscription and gate resolution are the real
OpenCuria code paths.

The LLM and workspace runtime are intentionally local stand-ins:

* `MobileFakeProvider` implements the production `ProviderAdapter` and emits
  deterministic streamed text or real harness question/permission tool calls.
  No model API, provider network, credential, API key or external service is
  contacted. A fake encrypted OpenAI-compatible connection/catalog only exists
  so the native model picker displays `Local deterministic fixture`; its base
  URL is a closed localhost port and the provider is never constructed during a
  harness run.
* `LocalWorkspaceAccessor` implements the real typed `WorkspaceAccessor`
  contract and confines read/write/list/stat/limited exec operations to one
  seeded directory under the private run dir. It is not a container, VM, or
  actual runner daemon. Desktop, process and stream operations deliberately
  fail closed.
* Socket.IO still authenticates actual app JWTs and checks membership/ownership
  before workspace subscription. No runner process is needed and the fake
  seeded Runner SID cannot receive commands.

Do not describe this as an external-LLM, Docker/QEMU or production runner test.

## Start a fresh isolated instance

From `/workspace/OpenCuria`, use a new private run directory each time. Set a
throwaway password in the parent shell; it is not stored in tracked files. Do
not use any production, database, Django secret, provider, or runner credentials.

```bash
cd /workspace/OpenCuria
export MOBILE_E2E_RUN_DIR="/tmp/opencuria-mobile-e2e/$(date +%Y%m%d-%H%M%S)-$(openssl rand -hex 4)"
export MOBILE_E2E_PASSWORD="$(openssl rand -base64 24)"
export MOBILE_E2E_HTTP_URL=http://127.0.0.1:8022
export MOBILE_E2E_SOCKET_URL=http://127.0.0.1:8022
export DJANGO_SETTINGS_MODULE=e2e.mobile.settings
export PYTHONPATH="$PWD:$PWD/backend"

backend/.venv/bin/python e2e/mobile/fixture.py seed
# In another shell with identical exports, start the backend:
backend/.venv/bin/python -m uvicorn e2e.mobile.asgi:application \
  --app-dir . --host 0.0.0.0 --port 8022 --workers 1 --lifespan on
# In another shell with identical exports, start the isolated runner protocol:
backend/.venv/bin/python -m e2e.mobile.runner_standin
# Then run protocol verification from any further same-env shell:
backend/.venv/bin/python e2e/mobile/fixture.py smoke
backend/.venv/bin/python e2e/mobile/socket_smoke.py
```

The settings module verifies that SQLite lives under `MOBILE_E2E_RUN_DIR`, uses
an in-memory Channels layer, and does not use Redis. At ASGI import the fixture
asserts the actual Socket.IO manager is not Redis. It never starts, stops,
clears or inspects backend port 8000, Redis, a worker, or any runner process.
Port 8022 is deliberately separate from the already-running dev backend. Bind
`0.0.0.0` so an Android emulator can reach the host; if it is a physical device,
use a LAN-reachable host URL instead and configure the OS firewall as needed.

Expo's `EXPO_PUBLIC_DEFAULT_SERVER_URL` should be the same host-reachable
`http://…:8022` URL when launching the mobile app. Android cleartext HTTP is
allowed only in the Expo development build. Login email is `mobile@example.test`
and the password is the ephemeral value held in `MOBILE_E2E_PASSWORD`.

The ASGI process itself takes ownership of `/frontend` Socket.IO and the real
JWT verifier; no separate Redis/pubsub daemon is needed. The mobile ASGI wrapper
skips only the process-global scheduled-task scheduler lifespan (unrelated to
chat, and intentionally a single-owner lock in a development DB). All HTTP,
Socket.IO, auth, organization, harness streaming, and gates remain the real ASGI
application. The smoke command checks unauthenticated HTTP, JWT/authz,
organization protection and the fake model catalog. The socket smoke exercises
actual HTTP harness creation, streamed Socket.IO updates, question/permission
futures, abort and a large chunked frontend file upload through the authenticated
Socket.IO runner stand-in. No provider network request is issued.

```bash
curl -fsS http://127.0.0.1:8022/api/v1/health/
backend/.venv/bin/python e2e/mobile/fixture.py smoke
backend/.venv/bin/python e2e/mobile/socket_smoke.py
```

Private, per-run data is under the run dir: `backend.sqlite3`, `workspace-root/`,
`manifest.json`, `http-smoke.json`, and a secret-free runner manifest. The
password is never written to disk. Runner/API-key credentials exist only in
owner-only (`0600`) files under this mode-`0700` run directory. No artifact
belongs in Git. Stop only the two parent-owned fixture processes, then remove
the exact private run directory if desired.

## Seed contents

Two running owned workspaces and one stopped workspace, one primary and one
foreign organization, and a foreign-owned private workspace. The primary chat
history includes 80 messages, a child explore session, Markdown, a PDF tool
attachment and real tool parts. Separate idle `Question gate demo`, `Permission
gate demo`, `Editable mobile test` and `Abort mobile test` sessions support
repeatable live flows.
The main seeded transcript has 80 extra messages plus markdown table/link/image,
a PDF attachment, and tool parts. Fresh live fixture prefixes:

* `mobile-e2e:question` → actual `question` tool persists a pending request,
  streams `harness.question_required`, and pauses until REST answer resumes it.
  Use the seeded **Question gate demo** session (it remains idle/openable on
  startup).
* `mobile-e2e:permission` → actual `edit` tool asks approval for
  `/workspace/README.md`; resolving once resumes the edit against the typed
  local workspace accessor. The dedicated demo workspace starts with a fresh
  README, so this scenario is repeatable.
* `mobile-e2e:abort` → deterministic slow streamed run for stop/abort UI. Use
  the seeded **Abort mobile test** session in the second running workspace.
* Any other prompt → incremental markdown stream with a table, link and image.

`e2e/mobile/runner_standin.py` is an optional, run-scoped Socket.IO runner
stand-in. Start it after the fixture ASGI server:

```bash
backend/.venv/bin/python -m e2e.mobile.runner_standin
```

It uses the fixture's private runner API token to authenticate against the real
`/ws/runner`, register the fixture runner SID, and answer actual `files:*`
Socket.IO requests using bounded files under `workspace-root/`. It does not
start a container, VM, arbitrary shell, or production runner. It is required
for Expo file uploads and the mobile native socket tests.

`e2e/mobile/android_ui.py` is an optional UIAutomator2 host runner. It expects a
real Android emulator/device, the built dev client already installed, and
`uiautomator2` in the host Python. It drives login, seeded history, real
Socket.IO stream, and both question/permission pause-resume flows. No
uiautomator2 package is imported or required by backend fixture setup.

## Cleanup / failure safety

Stop only the uvicorn process launched for this run. Verify its private run path
then delete only that exact `/tmp/opencuria-mobile-e2e/<run-id>` directory if
cleanup is wanted. This fixture's database and file workspace are private and
run-scoped. Do not migrate the ordinary `backend/db.sqlite3`, share the live
Django server's process state or fixed Redis channel, or run any shared DB/Redis
cleanup command.

## Native acceptance

After the emulator/device development client is installed and connected to Metro:

```sh
# Optional host-only UI driver; not a backend production dependency.
backend/.venv/bin/python -m pip install uiautomator2
adb reverse tcp:8081 tcp:8081
adb reverse tcp:8022 tcp:8022
backend/.venv/bin/python e2e/mobile/android_ui.py
```

The script reuses an authenticated app session or signs in through the actual
login screen. It selects fresh chats for gate tests so prior answers cannot
short-circuit the deterministic provider. It writes secret-free results and
real native screenshots to `/workspace/.opencuria/playwright/mobile/` (override
with `MOBILE_E2E_ARTIFACTS`). A failed step raises rather than being recorded as
passed. The native platform picker flows also have separate manually driven
UIAutomator acceptance in `docs/mobile-verification.md`; they are not asserted
by the automated core script. Use the fixture-owned sample PNG under `assets/`
for a reproducible private-media preview.

Do not run native Gradle compilation, a low-memory emulator and the entire
Python test suite concurrently on a 4-GB host. Start Metro with one worker and
wait for its initial bundle before booting the emulator when resources are tight.

## Existing web chat regressions

Serve the real Webapp on port 5179, then run its existing chat-image/sidebar
regressions without the legacy global database teardown:

```sh
npm run dev -w webapp -- --host 127.0.0.1 --port 5179 --strictPort
# In another shell; e2e has its own npm lockfile:
cd e2e && npm ci
E2E_BASE_URL=http://127.0.0.1:5179 npx playwright test -c mobile/web-regression.config.ts
```

The protocol/native fixture is not used by these browser regressions.
