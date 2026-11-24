# OpenCuria push proxy

The only route for mobile push notifications and iOS Live Activities. Apple and
Google hand out push credentials per app, not per server, so self-hosted
OpenCuria servers cannot hold them. They send through the proxy that holds the
credentials for the app their users run: `https://push.opencuria.dev` for the
published OpenCuria app. The proxy is stateless (no database) and sees only
ciphertext.

## What the proxy sees

- **Sees:** which server (its public key and IP) sends to which device, when,
  and whether it is an alert or a Live Activity update or end. At registration
  it also sees the device's IP and native token.
- **Never sees:** titles, workspace or conversation names, IDs, states or
  server URLs. Servers encrypt every message for the device (RFC 8291,
  aes128gcm) and pad it to a fixed size. Only the app's notification service
  extension, its Live Activity widget or its Android messaging service can
  decrypt. The proxy builds every APNs and FCM payload from a fixed template,
  so a server cannot push plaintext through it either.

## Flow

1. The app asks its server for `proxy_url` and `server_key`
   (`/api/v1/notifications/capabilities/`). It only continues if `proxy_url`
   is the proxy built into the app.
2. The app posts its native token to `POST /v1/registrations` and gets back an
   opaque **handle**. The handle is the token sealed with AES-SIV for this
   proxy and bound to that server key. It is deterministic, so the server can
   recognise a device without ever learning the token.
3. The app registers the handle and its public encryption key with the server.
4. The server posts ciphertext to `POST /v1/push`, signed with VAPID
   (RFC 8292, ES256, `aud` = proxy origin). The proxy only accepts handles
   bound to the signing key.

| `/v1/push` answer | Server action |
| --- | --- |
| `200` | delivered |
| `410 unregistered` / `410 invalid_handle` | drop the handle; the app registers again |
| `429`, `503` (with `Retry-After`) | retry later |
| other `4xx`, `502 provider_rejected` | give up on this message |

## Run it

The proxy needs a domain with ports 80 and 443 open (Caddy obtains the
certificate) and the credentials of the app it serves:

```sh
cd push-proxy
cp .env.example .env            # domain, handle key, provider settings
mkdir -m 700 secrets            # apns.p8 and/or firebase.json go here
sudo chown -R 10001:10001 secrets   # the container runs as UID 10001
docker compose build && docker compose up -d   # or pull a published image
curl https://push.example.com/healthz
```

- **iOS:** an APNs auth key (`.p8`) from the team that signs the app. List the
  app's bundle IDs in `APNS_APP_IDS`; Live Activities use the same key.
- **Android:** a service account JSON of the app's Firebase project, with the
  package names in `FCM_APP_IDS`.
- **Your own reverse proxy:** remove `COMPOSE_PROFILES=tls`. Set
  `PUSH_PROXY_PORT` to publish the proxy and forward HTTPS to it. Keep
  `X-Forwarded-For`, which the per-client registration limit uses.

## Your own app and proxy

Run your own proxy when you build the app with your own bundle IDs and push
credentials. A proxy can only reach apps whose credentials it holds.

1. Deploy this directory with your APNs key and/or Firebase project.
2. Set `PUSH_PROXY_URL` in `mobile/app.config.ts` and build the app.
3. Set `PUSH_PROXY_URL=https://your-proxy` in the server's `.env` and restart.
   A server and an app with different proxies show which proxy each one uses
   in the app's notification settings.

## Operations

- **Handle keys:** `PUSH_PROXY_HANDLE_KEYS` lists keys separated by commas. The
  first key seals new handles; all of them open existing ones. To rotate,
  prepend a new key and drop the old one once apps have registered again (they
  do on their next start). A lost key only means re-registration.
- **Limits:** kept per process, in memory. Registrations: 60 per client IP,
  then 2 per second. Pushes: 500 per server key, then 50 per second; 30 per
  handle, then 1 per second. Run one process per proxy.
- **Logs:** JSON on stdout, without tokens, handles or payloads. Failed
  deliveries carry the platform, provider error code and the first characters
  of the server key's thumbprint. Successful ones appear only at
  `LOG_LEVEL=DEBUG`.
- **Tests:** `pip install -r requirements-dev.txt && pytest -q`.
