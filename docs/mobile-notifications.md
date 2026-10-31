# Mobile notifications and Live Activities

OpenCuria sends native notifications directly through Apple Push Notification
service (APNs) on iOS and Firebase Cloud Messaging (FCM HTTP v1) on Android.
No Expo push relay, EAS project ID or background Socket.IO connection is needed.

## Behavior

- In the mobile app, open **Profile & settings → Notifications** to opt in.
  The app requests OS notification permission only after selecting On.
- Notifications cover root agent completions, failures and pending approvals or
  questions, across Native, Claude and Codex, including scheduled runs. A stopped
  run does not produce a misleading success/failure alert. Child-agent gates
  target their root conversation; child completions do not generate extra alerts.
- Tapping an alert opens its conversation after authenticated REST validation.
  Payloads for another installation/account/organization are ignored. Alerts for
  the currently visible conversation are suppressed in the foreground.
- On iOS, **Live Activities** is a separate opt-in. One card summarizes up to
  three conversations and the total number of active agents across the selected
  organization's workspaces. It supports the Lock Screen and compact, minimal
  and expanded Dynamic Island presentations. Approvals/questions take precedence
  in its status and tap target. Android receives native notifications.
- A Live Activity is created when the app is in the foreground with active work,
  including work started from web or schedules. Its ActivityKit update token is
  registered immediately and on every replacement. Once registered, server pushes
  update/end it while the phone is locked or the app is suspended or terminated.
  Reopening the app reconciles active instances and removes duplicate/stale cards.
  Completed cards are dismissed immediately; the separate completion alert remains.
- New Live Activities are not started remotely while the app is closed. This
  follows t3code's foreground creation model: it avoids remote-started cards whose
  per-activity update token never reaches the server after an unreliable background
  wake. Notifications still cover new runs while the app is closed.
  Reference: [t3code activity registration](https://github.com/pingdotgg/t3code/blob/7bafc40c17ab2a1cb8f0eb675cde4d6d3153f119/apps/mobile/src/features/agent-awareness/remoteRegistration.ts#L1057).
- Sign-out and organization changes revoke the old installation registration
  before changing account scope, then end local activities and remove listeners.
  If revocation cannot be confirmed, the app keeps the session and offers a retry.
  Registrations expire after 30 days without foreground renewal. Membership,
  account status, workspace ownership and current opt-in are rechecked at delivery.

## Server setup

Push providers belong to the hosted OpenCuria instance. The host configures them
once for all organizations using backend environment variables. Organization
admins cannot configure signing keys or select providers in the webapp.

1. Place the original provider files on the backend host:

   - APNs: the Apple `.p8` private key (EC P-256), unchanged.
   - FCM: the downloaded Firebase service-account JSON, unchanged. This is a
     server service account, separate from the Android `google-services.json`.

   For Docker Compose, put the files in `OpenCuria/secrets/push/`:

   ```text
   OpenCuria/
     .env
     secrets/push/
       apns.p8
       firebase.json
   ```

   Both Compose templates mount this directory read-only at `/run/secrets/` in
   the backend and recovery containers. The directory is excluded from Git and
   Docker build contexts. Only create the files for the providers you use.

   Set these variables in the hosting `OpenCuria/.env`:

   ```dotenv
   APNS_TEAM_ID=YOURTEAMID
   APNS_KEY_ID=YOURKEYID0
   APNS_PRIVATE_KEY_FILE=/run/secrets/apns.p8
   FCM_SERVICE_ACCOUNT_FILE=/run/secrets/firebase.json
   ```

   These are **container paths**. For a source deployment, use `backend/.env`
   with absolute paths on the backend host instead, for example:

   ```dotenv
   APNS_TEAM_ID=YOURTEAMID
   APNS_KEY_ID=YOURKEYID0
   APNS_PRIVATE_KEY_FILE=/etc/opencuria/secrets/apns.p8
   FCM_SERVICE_ACCOUNT_FILE=/etc/opencuria/secrets/firebase.json
   ```

   All three APNs variables must be set together. Leave all three empty to
   disable iOS notifications and Live Activities. Leave `FCM_SERVICE_ACCOUNT_FILE`
   empty to disable Android notifications. File paths are the only supported
   key source; inline JSON or private keys in environment variables are not
   supported. Restrict file access to the hosting administrator and backend
   process; files must be readable by the user running the backend.

   The APNs key must permit the app's bundle identifier and environment. Topics
   are restricted to `com.opencuria.mobile` and `com.opencuria.mobile.dev`;
   Live Activities append `.push-type.liveactivity`.

   For FCM, OpenCuria reads `project_id`, `client_email` and the RSA `private_key`
   from the service-account file; extra fields are ignored. OAuth and FCM URLs
   are fixed server-side. Enable the Firebase Cloud Messaging API and grant the
   service account permission to send messages for that Firebase project.

   Signing files are backend-only hosting secrets. They are never stored in
   the credential catalog, attached to workspaces, rendered in plugins or sent
   to clients. Do not include them in app bundles or `EXPO_PUBLIC_*` variables.

2. Update backend dependencies, apply migrations and restart the ASGI backend:

   ```sh
   cd backend
   .venv/bin/pip install -r requirements.txt
   .venv/bin/python manage.py migrate
   ```

   The migration deletes the old organization push configuration, push catalog
   entries and their credentials/references. It does not copy secrets into the
   hosting files or provide a fallback. Configure hosting settings explicitly.

   APNs requires HTTP/2; `httpx[http2]` installs its transport. The notification
   worker starts/stops with the existing ASGI lifespan alongside the scheduler.
   Use the project's normal single-worker Uvicorn command with `--lifespan on`.
   A WSGI server or ASGI server with lifespan disabled does not run this worker.
   Partial configuration, invalid keys and missing or unreadable files prevent
   backend startup with a bounded error that identifies the affected settings.
   Files are loaded once at startup; restart the backend and recovery processes
   after replacing them. Recreate Compose containers after changing `.env`:

   ```sh
   docker compose -f compose.server.yml up -d --force-recreate backend recovery
   ```

   Omit `-f compose.server.yml` for the local Compose deployment.

3. Each person enables notifications in the mobile app. The authenticated
   capabilities endpoint reports the same provider availability to all
   organizations; device registrations and delivery authorization remain scoped
   to the signed-in user and selected organization.

## Native builds

Install npm dependencies from the repository root with `npm ci`, then rebuild the
native development/release client. Expo Go and previously built binaries do not
contain the new native modules. JavaScript export alone does not add entitlements,
Firebase initialization or the widget extension.

### iOS

- Use an Apple Developer provisioning profile with Push Notifications and the
  generated App Group enabled. Expo config adds the notification entitlement,
  Live Activities support and the Expo Widgets extension.
- Development builds use `com.opencuria.mobile.dev`, App Group
  `group.com.opencuria.mobile.dev`, extension
  `com.opencuria.mobile.dev.ExpoWidgetsTarget`, and the APNs sandbox.
  Production uses the equivalent identifiers without `.dev` and production APNs.
  The signed Apple entitlement is `aps-environment=development` for the sandbox
  and `aps-environment=production` for production.
  Set `APNS_ENVIRONMENT=production` when distributing a development-variant app
  through TestFlight/ad-hoc signing that uses production APNs. Signing and the
  built `aps-environment` must match; JavaScript `__DEV__` does not choose it.
  Registration reads the native application ID and signed provisioning profile
  using Expo Application, with an explicit App Store fallback. Metro configuration
  cannot silently change the APNs topic/environment of the installed binary.
- The app already targets iOS 16.4+. Live Activities also depend on device and
  system authorization; Dynamic Island presentations require supported hardware.
- Generate on a Mac with `APP_VARIANT=development npx expo prebuild --platform ios`,
  configure signing for the app and extension, then `npm run ios -w mobile`.
  `expo-widgets` uses a runtime Live Activity factory; it needs no `widgets[]`
  configuration entry. Keep the `'widget'` layout pure and self-contained.

### Android

- Register the matching application ID in Firebase. Download that Android app's
  `google-services.json` and provide its path when generating native code:

  ```sh
  cd mobile
  APP_VARIANT=development GOOGLE_SERVICES_JSON=/secure/path/google-services.json \
    npx expo prebuild --platform android
  npm run android
  ```

- Use a Firebase-enabled physical device or Google Play emulator. Android 13+
  notification permission is requested after creating the **Agent activity**
  channel (`agent-activity`). Standard background notifications are displayed by
  the OS; no foreground/background service is needed.
- The Firebase service-account file belongs on the backend host. The Android
  client configuration file is separate. Do not commit a service-account key or put it in
  `EXPO_PUBLIC_*` variables.

## Delivery and privacy

Harness lifecycle events publish to a separate notification consumer; the harness
never imports this app. Receivers persist idempotent outbox jobs and never perform
provider I/O in the run/streaming loop. Lifecycle signals plus a 15-minute repair
window cover missed completions and unanswered gates after a restart. Live Activities reconcile
from durable sessions/gates and receive a five-minute heartbeat; cards become stale
after ten minutes without an update.

Outbox claims have a 60-second lease and fenced acknowledgements. Provider/network
errors use bounded exponential retry (six attempts, up to 120 seconds between
attempts); jobs expire after 15 minutes. Invalid provider destinations are removed
conditionally so an old failure cannot erase a newer token. Jobs are retained for
two days, and unused registrations for 30 days. APNs/FCM remain best-effort delivery
services; accepting a request does not prove a notification was shown on a phone.

The server stores device/update tokens encrypted with the same vault key and uses
a one-way token fingerprint to retire a previous account binding. SecureStore
contains account-scoped random installation IDs and consent, not hardware IDs or
transcripts. Notification payloads contain IDs and bounded conversation/workspace
titles, which may appear on the Lock Screen and pass through Apple/Google.
They never contain prompts, assistant text, tool output, question bodies, tokens
or provider credentials. Users can control lock-screen previews in OS settings.

Delivery errors store bounded codes such as `apns_403`, `fcm_auth_failed` or
`provider_unreachable` in `notifications.PushDelivery.error_code`. HTTP client
request logging is kept at WARNING because APNs URLs contain device tokens.
Configuration readiness means validated server settings, not proof that Apple
or Google has accepted that account/key.

## Verification

See [notification verification](mobile-notifications-verification.md) for executed
checks and remaining platform acceptance. Before release, test on provisioned devices:
permission grant/denial and OS revocation, token rotation, foreground suppression,
background completion/failure/question/approval, notification cold start, Live
Activity update/end after locking and force-quit, multiple runs/devices, account
and organization changes, lost connectivity and backend restart.

Primary platform references: [Expo Notifications](https://docs.expo.dev/versions/v57.0.0/sdk/notifications/),
[Expo Widgets](https://docs.expo.dev/versions/v57.0.0/sdk/widgets/),
[Apple ActivityKit push updates](https://developer.apple.com/documentation/ActivityKit/starting-and-updating-live-activities-with-activitykit-push-notifications),
and [FCM HTTP v1](https://firebase.google.com/docs/cloud-messaging/send/v1-api).
