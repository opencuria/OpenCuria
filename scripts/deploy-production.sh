#!/usr/bin/env bash
# Compatible HTTP/worker cutover only. Never replace stable process plane or old runs.
set -Eeuo pipefail
umask 077
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ $# == 2 ]] || fail 'Usage: deploy-production.sh IMMUTABLE_TAG RELEASE_PROJECT_SUFFIX'
TAG="$1"; SLOT="$2"
[[ "$TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ && "$TAG" != latest ]] || fail 'Use an immutable release tag (not latest)'
[[ "$SLOT" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || fail 'Invalid release project suffix'
[[ -f .env && -f .env.operations && -f release.json ]] || fail 'Prepare .env, .env.operations and release.json'
command -v docker >/dev/null || fail 'Docker Compose v2 is required'
exec 9>"${OPENCURIA_DEPLOY_LOCK:-$ROOT/.deployment.lock}"
flock -n 9 || fail 'Another deployment is running'
export OPENCURIA_TAG="$TAG"
PROJECT="opencuria-$SLOT"
# Never `compose up` an existing worker project: even identical code/env can
# replace a process with live run ownership. Resume/rollback is an explicit switch.
if docker container inspect "$PROJECT-worker-1" >/dev/null 2>&1; then
    fail "$PROJECT already exists; use a fresh release suffix or explicitly retire it"
fi
export OPENCURIA_DEPLOYMENT_VERSION="$(python3 -c 'import json; print(json.load(open("release.json"))["version"])')"
export OPENCURIA_WORKER_ID="$PROJECT-$(date -u +%Y%m%d%H%M%S)-${RANDOM}"
[[ "$OPENCURIA_DEPLOYMENT_VERSION" == "$TAG" ]] || fail 'Manifest version must match the immutable tag'
STATE="$ROOT/deploy/releases/$SLOT"
[[ ! -e "$STATE" ]] || fail 'Release metadata already exists; use a fresh suffix'
mkdir -p "$STATE"
cp release.json "$STATE/release.json"
export OPENCURIA_RELEASE_MANIFEST="$STATE/release.json"
TIMEOUT="${OPENCURIA_UPGRADE_TIMEOUT:-900}"
[[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail 'Invalid readiness timeout'
# Stable infra/operations are separately installed and must be present; no update
# of DB, Redis, socket gateway, scheduler or recovery is hidden in this command.
for name in db redis proxy; do
    docker container inspect "opencuria-infra-$name-1" >/dev/null || fail "Stable $name is not installed"
done
docker container inspect opencuria-operations-socket-gateway-1 >/dev/null || fail 'Stable operations project is not installed'
docker pull "ghcr.io/opencuria/backend:$TAG"
docker pull "ghcr.io/opencuria/webapp:$TAG"
compose=(docker compose --env-file .env -p "$PROJECT" -f compose.upgrade.yml)
"${compose[@]}" --profile upgrade run --rm migrate
"${compose[@]}" --profile app up -d worker gateway webapp
container="$PROJECT-gateway-1"
ready=0
for ((elapsed=0; elapsed<TIMEOUT; elapsed+=2)); do
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    if [[ "$state" == healthy ]]; then ready=1; break; fi
    sleep 2
done
[[ "$ready" == 1 ]] || fail 'Candidate not ready; previous upstream and all workers were retained'
# Directory bind mount is essential: atomic rename updates the visible inode.
UPSTREAMS="$ROOT/deploy/upstreams.conf"
backup="$(mktemp)"; cp "$UPSTREAMS" "$backup"
tmp="${UPSTREAMS}.tmp.$$"
cat > "$tmp" <<EOF
upstream opencuria_backend { server ${container}:8000; }
upstream opencuria_webapp { server ${PROJECT}-webapp-1:80; }
upstream opencuria_socket_gateway { server opencuria-operations-socket-gateway-1:8000; }
EOF
chmod 0644 "$tmp"; mv "$tmp" "$UPSTREAMS"
proxy=opencuria-infra-proxy-1
if ! docker exec "$proxy" nginx -t -c /etc/nginx/opencuria/nginx.conf || ! docker exec "$proxy" nginx -s reload -c /etc/nginx/opencuria/nginx.conf; then
    cp "$backup" "$tmp"; chmod 0644 "$tmp"; mv "$tmp" "$UPSTREAMS"
    docker exec "$proxy" nginx -s reload -c /etc/nginx/opencuria/nginx.conf || true
    rm -f "$backup"
    fail 'Proxy cutover failed; old upstream restored, candidate retained'
fi
rm -f "$backup"
printf 'Cutover complete: %s. Stable connections and every old worker were retained.\n' "$PROJECT"
printf 'Drain old release explicitly: python manage.py drain_release --release-version OLD --once\n'
