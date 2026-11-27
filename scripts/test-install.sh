#!/usr/bin/env bash
# Exercises install.sh against stubbed Docker, libvirt, systemd, apt and curl.
# Runs in a private mount namespace with tmpfs over every host path the
# installer touches, so the host stays unchanged. Needs root:
#
#   sudo scripts/test-install.sh [TEST_NAME...]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="${REPO_DIR}/install.sh"
SANDBOX_DIRS=(/opt /var/lib /etc/systemd/system /etc/libvirt /root /run /srv)
MARKER=".install-test-sandbox"

if [[ -z "${INSTALL_TEST_SANDBOX:-}" ]]; then
  [[ "${EUID}" -eq 0 ]] || { echo "Run as root: it needs a private mount namespace." >&2; exit 1; }
  exec env INSTALL_TEST_SANDBOX=1 unshare --mount --propagation private "${BASH}" "$0" "$@"
fi

for dir in "${SANDBOX_DIRS[@]}"; do
  mkdir -p "${dir}"
  mount -t tmpfs tmpfs "${dir}"
  touch "${dir}/${MARKER}"
done

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export FAKE="/run/fake" STUBS="${WORK}/stubs" BIN="${WORK}/bin"
mkdir -p "${STUBS}" "${BIN}"

# -- source archive ---------------------------------------------------------------

# The installer re-runs the install.sh of the archive, here the one under test.
mkdir -p "${WORK}/src/OpenCuria-main/runner/systemd"
cp "${INSTALLER}" "${REPO_DIR}/compose.yml" "${REPO_DIR}/Caddyfile" "${WORK}/src/OpenCuria-main/"
cp "${REPO_DIR}/runner/.env.example" "${REPO_DIR}/runner/requirements.txt" \
  "${WORK}/src/OpenCuria-main/runner/"
cp "${REPO_DIR}/runner/systemd/opencuria-runner.service" \
  "${WORK}/src/OpenCuria-main/runner/systemd/"
mkdir -p "${WORK}/src/OpenCuria-main/push-proxy"
cp "${REPO_DIR}"/push-proxy/{compose.yml,compose.colocated.yml,Caddyfile} \
  "${WORK}/src/OpenCuria-main/push-proxy/"
tar -czf "${WORK}/source.tar.gz" -C "${WORK}/src" OpenCuria-main
export SOURCE_TGZ="${WORK}/source.tar.gz"

# Another version whose installer only reports how it was called (FAKE/alt_source).
mkdir -p "${WORK}/alt/OpenCuria-main"
cp -r "${WORK}/src/OpenCuria-main/runner" "${REPO_DIR}/compose.yml" "${WORK}/alt/OpenCuria-main/"
cat >"${WORK}/alt/OpenCuria-main/install.sh" <<'EOF'
#!/usr/bin/env bash
printf 'archive installer: %s\n' "$*"
printf 'archive source: %s\n' "${OPENCURIA_INSTALLER_SOURCE}"
EOF
tar -czf "${WORK}/old.tar.gz" -C "${WORK}/alt" OpenCuria-main
export OLD_SOURCE_TGZ="${WORK}/old.tar.gz" # predates the push proxy (FAKE/old_source)
cp -r "${WORK}/src/OpenCuria-main/push-proxy" "${WORK}/alt/OpenCuria-main/"
tar -czf "${WORK}/alt.tar.gz" -C "${WORK}/alt" OpenCuria-main
export ALT_SOURCE_TGZ="${WORK}/alt.tar.gz"

# -- stubs ------------------------------------------------------------------------

stub() { cat >"${STUBS}/$1"; chmod +x "${STUBS}/$1"; ln -sf "${STUBS}/$1" "${BIN}/$1"; }

stub docker <<'EOF'
#!/usr/bin/env bash
# Containers, volumes and networks named other_* belong to another project.
# A container file holds its state, image and Compose project; volumes and
# networks carry their project as name prefix.
S="${FAKE}/docker"
mkdir -p "${S}"/{containers,volumes,networks,images}
echo "docker $*" >>"${FAKE}/calls"
if [[ "$1 ${2:-}" == "compose version" ]]; then echo "Docker Compose version v2.30.0"; exit 0; fi
if [[ ! -f "${S}/up" ]]; then
  echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock." >&2
  exit 1
fi
names() { local f; for f in "$1"/*; do [[ -e "${f}" ]] && basename "${f}"; done; return 0; }
project_filter() { # the project of a --filter label=com.docker.compose.project=NAME
  local a
  for a in "$@"; do [[ "${a}" != label=com.docker.compose.project=* ]] || echo "${a#*=*=}"; done
}
add_container() { # add_container NAME IMAGE PROJECT
  printf 'running\n%s\n%s\n' "$2" "$3" >"${S}/containers/$1"
  printf '%s\n' "$2" >"${S}/images/$(printf '%s' "$2" | md5sum | cut -c1-12)"
}
group="$1" sub="${2:-}"
shift 2 || shift
case "${group}" in
  info) exit 0 ;;
  compose)
    while [[ "${sub}" == --profile ]]; do sub="$2"; shift 2; done
    project=opencuria
    [[ "${PWD}" != */push-proxy ]] || project=opencuria-push
    echo "compose[${project}] ${sub} $*" >>"${FAKE}/calls"
    case "${sub}" in
      pull)
        [[ ! -f "${FAKE}/pull_fails" ]] || { echo "pull access denied" >&2; exit 1; }
        ;;
      up)
        if [[ "${project}" == opencuria-push ]]; then
          grep -q '^PUSH_PROXY_HANDLE_KEYS=.' .env ||
            { echo "required variable PUSH_PROXY_HANDLE_KEYS is missing a value" >&2; exit 1; }
          if [[ "$(sed -n 's/^COMPOSE_FILE=//p' .env)" == *compose.colocated.yml* ]]; then
            net="$(sed -n 's/^CONTROL_PLANE_NETWORK=//p' .env)"
            [[ -e "${S}/networks/${net}" ]] ||
              { echo "network ${net} declared as external, but could not be found" >&2; exit 1; }
          fi
          [[ ! -f "${FAKE}/push_unhealthy" ]] ||
            { echo "container opencuria_push_proxy is unhealthy" >&2; exit 1; }
          add_container opencuria_push_proxy ghcr.io/opencuria/push-proxy:latest opencuria-push
          if [[ ",$(sed -n 's/^COMPOSE_PROFILES=//p' .env)," == *,tls,* ]]; then
            add_container opencuria_push_caddy caddy:2-alpine opencuria-push
            touch "${S}/volumes/opencuria-push_caddy-data" "${S}/volumes/opencuria-push_caddy-config"
          fi
          touch "${S}/networks/opencuria-push_push-net"
          exit 0
        fi
        grep -q '^DB_PASSWORD=.' .env ||
          { echo "required variable DB_PASSWORD is missing a value" >&2; exit 1; }
        for c in db redis backend execution webapp recovery proxy; do
          case "${c}" in
            db) image=postgres:16-alpine ;; redis) image=redis:7-alpine ;;
            proxy) image=caddy:2-alpine ;; webapp) image=ghcr.io/opencuria/webapp:latest ;;
            *) image=ghcr.io/opencuria/backend:latest ;;
          esac
          add_container "opencuria_${c}" "${image}" opencuria
        done
        for v in pgdata redisdata backend-runtime caddy-data caddy-config; do
          touch "${S}/volumes/opencuria_${v}"
        done
        touch "${S}/networks/opencuria_opencuria-net" "${S}/networks/opencuria_edge"
        ;;
      exec)
        if [[ "$*" == *"caddy reload"* ]]; then
          grep -q '^[a-z0-9.-]* {$' Caddyfile || { echo "Error: adapting config" >&2; exit 1; }
          echo "$*" >>"${FAKE}/caddy_reloads"
        else
          echo "$*" >>"${FAKE}/bootstrap"
        fi
        ;;
      rm) [[ " $* " != *" caddy "* ]] || rm -f "${S}/containers/opencuria_push_caddy" ;;
      logs) echo '{"event": "ConfigError: APNS_PRIVATE_KEY_FILE must contain the P-256 .p8 key"}' ;;
      *) echo "unexpected compose ${sub}" >&2; exit 2 ;;
    esac
    ;;
  container)
    case "${sub}" in
      ls)
        running=0 project="$(project_filter "$@")"
        [[ " $* " != *" status=running "* ]] || running=1
        for n in $(names "${S}/containers"); do
          [[ "${n}" != other_* ]] || continue
          [[ -z "${project}" || "$(sed -n 3p "${S}/containers/${n}")" == "${project}" ]] || continue
          [[ "${running}" -eq 0 || "$(head -n 1 "${S}/containers/${n}")" == running ]] || continue
          echo "${n}"
        done
        ;;
      inspect)
        for n in "$@"; do
          [[ "${n}" != --format && "${n}" != *'{{'* ]] || continue
          sed -n 2p "${S}/containers/${n}"
        done
        ;;
      rm) for n in "$@"; do [[ "${n}" == -f ]] || rm -f "${S}/containers/${n}"; done ;;
      *) exit 2 ;;
    esac
    ;;
  volume)
    case "${sub}" in
      ls) names "${S}/volumes" | grep -v '^other_' | grep "^$(project_filter "$@")_" || true ;;
      inspect) [[ -e "${S}/volumes/$1" ]] ;;
      rm) for n in "$@"; do [[ "${n}" == -f ]] || rm -f "${S}/volumes/${n}"; done ;;
      *) exit 2 ;;
    esac
    ;;
  network)
    case "${sub}" in
      ls) names "${S}/networks" | grep -v '^other_' | grep "^$(project_filter "$@")_" || true ;;
      rm) for n in "$@"; do rm -f "${S}/networks/${n}"; done ;;
      *) exit 2 ;;
    esac
    ;;
  image)
    case "${sub}" in
      ls)
        for id in $(names "${S}/images"); do
          [[ "$(cat "${S}/images/${id}")" != ghcr.io/opencuria/* ]] || echo "${id}"
        done
        ;;
      rm)
        force=0
        for ref in "$@"; do
          if [[ "${ref}" == -f ]]; then force=1; continue; fi
          for id in $(names "${S}/images"); do
            [[ "${id}" == "${ref}" || "$(cat "${S}/images/${id}")" == "${ref}" ]] || continue
            image="$(cat "${S}/images/${id}")"
            if [[ "${force}" -eq 0 ]] && grep -rqxF "${image}" "${S}/containers"; then
              echo "conflict: image is being used by a container" >&2
              exit 1
            fi
            rm -f "${S}/images/${id}"
          done
        done
        ;;
      *) exit 2 ;;
    esac
    ;;
  *) echo "unexpected docker ${group}" >&2; exit 2 ;;
esac
EOF

stub virsh <<'EOF'
#!/usr/bin/env bash
S="${FAKE}/libvirt"
mkdir -p "${S}"/{domains,networks}
echo "virsh $*" >>"${FAKE}/calls"
while [[ "$1" == -q || "$1" == -c ]]; do [[ "$1" == -c ]] && shift; shift; done
if [[ ! -f "${S}/up" ]]; then
  echo "error: failed to connect to the hypervisor" >&2
  exit 1
fi
cmd="$1" name="${2:-}"
dom="${S}/domains/${name}" net="${S}/networks/${name}"
names() { local f; for f in "$1"/*; do [[ -e "${f}" ]] && basename "${f}"; done; return 0; }
case "${cmd}" in
  list)
    for n in $(names "${S}/domains"); do
      [[ " $* " != *" --state-running "* || "$(cat "${S}/domains/${n}")" == running ]] || continue
      echo "${n}"
    done
    echo
    ;;
  net-list) names "${S}/networks"; echo ;;
  domstate) [[ -f "${dom}" ]] || { echo "error: failed to get domain '${name}'" >&2; exit 1; }
    cat "${dom}"; echo ;;
  dominfo) [[ -f "${dom}" ]] ;;
  destroy)
    [[ -f "${dom}" ]] || exit 1
    [[ "$(cat "${dom}")" != "shut off" ]] || { echo "error: domain is not running" >&2; exit 1; }
    echo "shut off" >"${dom}"
    ;;
  undefine)
    [[ -f "${dom}" ]] || exit 1
    if [[ -f "${FAKE}/old_virsh" && " $* " == *" --checkpoints-metadata "* ]]; then
      echo "error: command 'undefine' doesn't support option --checkpoints-metadata" >&2
      exit 1
    fi
    [[ "$(cat "${dom}")" == "shut off" ]] || { echo "error: domain is running" >&2; exit 1; }
    rm -f "${dom}"
    ;;
  net-info) [[ -f "${net}" ]] ;;
  net-destroy)
    [[ -f "${net}" && "$(cat "${net}")" == active ]] || { echo "error: network is not active" >&2; exit 1; }
    echo inactive >"${net}"
    ;;
  net-undefine) [[ -f "${net}" ]] || exit 1; rm -f "${net}" ;;
  *) echo "unexpected virsh ${cmd}" >&2; exit 2 ;;
esac
EOF

stub systemctl <<'EOF'
#!/usr/bin/env bash
S="${FAKE}/systemd"
mkdir -p "${S}"
echo "systemctl $*" >>"${FAKE}/calls"
cmd="$1"
shift
now=0 quiet=0 units=()
for a in "$@"; do
  case "${a}" in
    --now) now=1 ;;
    --quiet | -q) quiet=1 ;;
    -*) ;;
    *) units+=("${a%.service}") ;;
  esac
done
unit_file=/etc/systemd/system/opencuria-runner.service
start() {
  case "$1" in
    docker)
      [[ ! -f "${FAKE}/docker_broken" ]] ||
        { echo "Job for docker.service failed because the control process exited with error code." >&2; return 1; }
      touch "${FAKE}/docker/up"
      ;;
    libvirtd)
      [[ ! -f "${FAKE}/libvirt_broken" ]] ||
        { echo "Job for libvirtd.service failed." >&2; return 1; }
      touch "${FAKE}/libvirt/up"
      ;;
    opencuria-runner)
      [[ -e "${unit_file}" ]] || { echo "Unit opencuria-runner.service not found." >&2; return 5; }
      [[ "$(readlink "${unit_file}")" != /dev/null ]] ||
        { echo "Unit opencuria-runner.service is masked." >&2; return 1; }
      # journalctl --since: only this start's log lines.
      if [[ -f "${FAKE}/runner_rejected" ]]; then
        echo '{"event": "websocket_connect_error"}' >"${FAKE}/journal"
      else
        echo '{"event": "websocket_connected"}' >"${FAKE}/journal"
      fi
      ;;
  esac
  echo active >"${S}/$1"
}
stop() {
  case "$1" in
    docker) rm -f "${FAKE}/docker/up" ;;
    libvirtd) rm -f "${FAKE}/libvirt/up" ;;
  esac
  echo inactive >"${S}/$1"
}
rc=0
case "${cmd}" in
  start | restart) for u in "${units[@]}"; do start "${u}" || rc=$?; done ;;
  stop) for u in "${units[@]}"; do stop "${u}"; done ;;
  enable)
    for u in "${units[@]}"; do
      if [[ "${u}" == opencuria-runner && "$(readlink "${unit_file}")" == /dev/null ]]; then
        echo "Failed to enable unit: Unit file ${unit_file} is masked." >&2
        exit 1
      fi
      touch "${S}/${u}.enabled"
      [[ "${now}" -eq 0 ]] || start "${u}" || rc=$?
    done
    ;;
  disable) for u in "${units[@]}"; do rm -f "${S}/${u}.enabled"; done ;;
  is-active)
    state="$(cat "${S}/${units[0]}" 2>/dev/null || echo inactive)"
    [[ "${quiet}" -eq 1 ]] || echo "${state}"
    [[ "${state}" == active ]] || rc=3
    ;;
  daemon-reload) [[ ! -f "${FAKE}/daemon_reload_fails" ]] || rc=1 ;;
  reset-failed | cat | unmask) ;;
  *) echo "unexpected systemctl ${cmd}" >&2; rc=2 ;;
esac
exit "${rc}"
EOF

stub journalctl <<'EOF'
#!/usr/bin/env bash
cat "${FAKE}/journal" 2>/dev/null || true
EOF

stub apt-get <<'EOF'
#!/usr/bin/env bash
echo "apt-get $*" >>"${FAKE}/calls"
[[ ! -f "${FAKE}/apt_fails" ]] || { echo "E: Unable to locate package" >&2; exit 100; }
EOF

stub ss <<'EOF'
#!/usr/bin/env bash
[[ ! -f "${FAKE}/port_busy" ]] || echo "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*"
EOF

stub pgrep <<'EOF'
#!/usr/bin/env bash
[[ -f "${FAKE}/manual_runner" ]] || exit 1
[[ "$1" != -af ]] || echo "4242 /opt/opencuria/runner/.venv/bin/python -m src"
EOF

stub pkill <<'EOF'
#!/usr/bin/env bash
echo "pkill $*" >>"${FAKE}/calls"
[[ -f "${FAKE}/unkillable" ]] || rm -f "${FAKE}/manual_runner"
EOF

stub python3 <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == -m && "$2" == venv ]]; then
  mkdir -p "$3/bin"
  printf '#!/bin/sh\nexit 0\n' >"$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
[[ "$1" != -c ]] || echo 1
EOF

stub curl <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >>"${FAKE}/calls"
url=""
for a in "$@"; do [[ "${a}" != http*://* ]] || url="${a}"; done
case "${url}" in
  */api/v1/health/)
    [[ ! -f "${FAKE}/backend_down" ]] || { echo "curl: (7) Failed to connect" >&2; exit 7; } ;;
  */api/v1/auth/login/) cat >/dev/null ;;
  https://get.docker.com) echo "ln -sf '${STUBS}/docker' '${BIN}/docker'" ;;
  *)
    if [[ -f "${FAKE}/old_source" ]]; then
      cat "${OLD_SOURCE_TGZ}"
    elif [[ -f "${FAKE}/alt_source" ]]; then
      cat "${ALT_SOURCE_TGZ}"
    else
      cat "${SOURCE_TGZ}"
    fi
    ;;
esac
EOF
rm -f "${BIN}/docker" # installed per test

# -- fixtures and assertions --------------------------------------------------------

OUT="" STATUS=0 FAILURES=0 CURRENT=""

reset_host() {
  local dir
  for dir in "${SANDBOX_DIRS[@]}"; do
    [[ -f "${dir}/${MARKER}" ]] || { echo "Refusing to clean ${dir}: not a sandbox" >&2; exit 1; }
    find "${dir}" -mindepth 1 -maxdepth 1 ! -name "${MARKER}" -exec rm -rf {} +
  done
  mkdir -p /run/systemd/system "${FAKE}"/docker/{containers,volumes,networks,images} \
    "${FAKE}"/libvirt/{domains,networks}
  touch "${FAKE}/docker/up" "${FAKE}/libvirt/up"
  ln -sf "${STUBS}/docker" "${BIN}/docker"
  ln -sf "${STUBS}/virsh" "${BIN}/virsh"
  printf '# libvirt network settings\n#firewall_backend = "nftables"\n' >/etc/libvirt/network.conf
  # Resources of other software must survive every uninstall.
  printf 'running\nnginx:latest\n' >"${FAKE}/docker/containers/other_web"
  echo nginx:latest >"${FAKE}/docker/images/nginx01"
  touch "${FAKE}/docker/volumes/other_data"
  echo running >"${FAKE}/libvirt/domains/other-vm"
  echo active >"${FAKE}/libvirt/networks/default"
}

add_workspace() { # add_workspace ID STATE
  echo "$2" >"${FAKE}/libvirt/domains/opencuria-workspace-$1"
  echo active >"${FAKE}/libvirt/networks/opencuria-ws-$1"
  mkdir -p /var/lib/opencuria/disks
  touch "/var/lib/opencuria/disks/$1.qcow2"
}

run() { # run ARGS...: no terminal
  set +e
  OUT="$(PATH="${BIN}:${PATH}" bash "${INSTALLER}" "$@" 2>&1 </dev/null)"
  STATUS=$?
  set -e
}

run_tty() { # run_tty INPUT ARGS...: answers INPUT on a terminal
  local input="$1"
  shift
  set +e
  OUT="$(printf '%b' "${input}" |
    PATH="${BIN}:${PATH}" script -qec "bash ${INSTALLER} $*" /dev/null 2>&1)"
  STATUS=$?
  set -e
}

fail() {
  FAILURES=$((FAILURES + 1))
  printf '  FAIL %s: %s\n' "${CURRENT}" "$*"
  printf '%s\n' "${OUT}" | sed 's/^/      | /' | tail -n 40
}

expect_status() { [[ "${STATUS}" -eq "$1" ]] || fail "exit status ${STATUS}, expected $1"; }
expect_out() { grep -qF -- "$1" <<<"${OUT}" || fail "output lacks: $1"; }
expect_no_out() { ! grep -qF -- "$1" <<<"${OUT}" || fail "output contains: $1"; }
expect_path() { [[ -e "$1" || -L "$1" ]] || fail "missing: $1"; }
expect_no_path() { [[ ! -e "$1" && ! -L "$1" ]] || fail "still present: $1"; }
expect_called() { grep -qF -- "$1" "${FAKE}/calls" || fail "not called: $1"; }
expect_not_called() { ! grep -qF -- "$1" "${FAKE}/calls" 2>/dev/null || fail "called: $1"; }
expect_count() { # expect_count DIR N
  local n
  n="$(find "$1" -mindepth 1 -maxdepth 1 | wc -l)"
  [[ "${n}" -eq "$2" ]] || fail "$1 holds ${n} entries, expected $2"
}
env_of() { sed -n "s/^$2=//p" "$1" | tail -n 1; }

expect_foreign_intact() {
  expect_path "${FAKE}/docker/containers/other_web"
  expect_path "${FAKE}/docker/images/nginx01"
  expect_path "${FAKE}/docker/volumes/other_data"
  expect_path "${FAKE}/libvirt/domains/other-vm"
  expect_path "${FAKE}/libvirt/networks/default"
}

expect_fully_installed() {
  expect_path /opt/opencuria/.env
  expect_path /opt/opencuria/.bootstrapped
  expect_path /opt/opencuria/runner/.env
  expect_path /etc/systemd/system/opencuria-runner.service
  expect_path "${FAKE}/docker/volumes/opencuria_pgdata"
  [[ "$(cat "${FAKE}/systemd/opencuria-runner" 2>/dev/null)" == active ]] ||
    fail "runner service is not active"
}

expect_uninstalled() {
  expect_no_path /opt/opencuria
  expect_no_path /var/lib/opencuria
  expect_no_path /var/lib/opencuria-runner
  expect_no_path /root/.local/share/opencuria
  expect_no_path /etc/systemd/system/opencuria-runner.service
  expect_count "${FAKE}/docker/containers" 1
  expect_count "${FAKE}/docker/volumes" 1
  expect_count "${FAKE}/docker/networks" 0
  expect_count "${FAKE}/libvirt/domains" 1
  expect_count "${FAKE}/libvirt/networks" 1
  if find "${FAKE}/docker/images" -type f -exec cat {} + | grep -q '^ghcr.io/opencuria/'; then
    fail "OpenCuria images remain"
  fi
  expect_foreign_intact
}

# A full installation with two workspaces, the way the runner leaves them.
install_full() {
  run full --domain opencuria.example.com --admin-email admin@example.com \
    --admin-password correct-horse -y
  [[ "${STATUS}" -eq 0 ]] || { fail "fixture: full install failed"; return 0; }
  add_workspace 1111 running
  add_workspace 2222 "shut off"
  mkdir -p /var/lib/opencuria-runner /root/.local/share/opencuria/ssh
  touch /var/lib/opencuria-runner/journal.sqlite3 /root/.local/share/opencuria/ssh/runner_key
  : >"${FAKE}/calls"
}

KEYS=/root/keys
APNS_KEY="${KEYS}/AuthKey_KEYID12345.p8"
FCM_KEY="${KEYS}/firebase.json"
PUSH_ENV=/opt/opencuria/push-proxy/.env

make_push_keys() { # provider keys as Apple and Google hand them out (shape only)
  mkdir -p "${KEYS}"
  printf -- '-----BEGIN PRIVATE KEY-----\nMIGTAgEA\n-----END PRIVATE KEY-----\n' >"${APNS_KEY}"
  printf '{"client_email": "push@app.iam.gserviceaccount.com", "private_key": "-----BEGIN"}\n' \
    >"${FCM_KEY}"
}

# A full installation with the push proxy for iOS next to the control plane.
install_full_with_proxy() {
  make_push_keys
  run full --proxy --domain opencuria.example.com --admin-email admin@example.com \
    --admin-password correct-horse --proxy-domain push.example.com \
    --apns-key "${APNS_KEY}" --apns-team-id TEAM123456 -y
  [[ "${STATUS}" -eq 0 ]] || { fail "fixture: full install with push proxy failed"; return 0; }
  : >"${FAKE}/calls"
}

expect_proxy_colocated() {
  [[ "$(env_of "${PUSH_ENV}" COMPOSE_FILE)" == compose.yml:compose.colocated.yml &&
    -z "$(env_of "${PUSH_ENV}" COMPOSE_PROFILES)" &&
    "$(env_of "${PUSH_ENV}" CONTROL_PLANE_NETWORK)" == opencuria_edge ]] ||
    fail "push proxy is not set up behind the control plane's HTTPS proxy"
  [[ "$(grep -cx 'push.example.com {' /opt/opencuria/Caddyfile)" -eq 1 ]] ||
    fail "the control plane's Caddy does not serve the push proxy (once)"
  grep -q 'reverse_proxy push-proxy:8080' /opt/opencuria/Caddyfile ||
    fail "the control plane's Caddy does not forward to the push proxy"
  expect_path "${FAKE}/docker/containers/opencuria_push_proxy"
  expect_no_path "${FAKE}/docker/containers/opencuria_push_caddy"
}

# -- tests ------------------------------------------------------------------------

test_fresh_without_terminal_needs_action() {
  run
  expect_status 1
  expect_out "No terminal to ask on: pass an action"
  expect_no_path /opt/opencuria
}

test_fresh_full_install() {
  install_full
  expect_fully_installed
  expect_out "OpenCuria is ready."
  [[ "$(env_of /opt/opencuria/runner/.env RUNNER_BACKEND_URL)" == http://127.0.0.1:8000 ]] ||
    fail "runner does not use the local control plane"
  grep -q -- "--runner-name" "${FAKE}/bootstrap" || fail "bootstrap did not register the runner"
  [[ "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" == https://push.opencuria.dev ]] ||
    fail "push proxy URL missing from .env"
  expect_no_path /opt/opencuria/secrets
}

test_update_adds_the_push_proxy_and_keeps_a_chosen_one() {
  install_full
  sed -i '/^PUSH_PROXY_URL=/d' /opt/opencuria/.env
  run update
  expect_status 0
  [[ "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" == https://push.opencuria.dev ]] ||
    fail "update did not add the push proxy URL"
  sed -i 's|^PUSH_PROXY_URL=.*|PUSH_PROXY_URL=|' /opt/opencuria/.env
  run update
  expect_status 0
  [[ "$(grep -c '^PUSH_PROXY_URL=' /opt/opencuria/.env)" -eq 1 &&
    -z "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" ]] ||
    fail "update replaced a deliberately empty push proxy URL"
}

test_rerun_without_terminal_updates_and_keeps_config() {
  install_full
  local db_password token
  db_password="$(env_of /opt/opencuria/.env DB_PASSWORD)"
  token="$(env_of /opt/opencuria/runner/.env RUNNER_API_TOKEN)"
  rm -f "${FAKE}/bootstrap"
  run
  expect_status 0
  expect_out "updating it"
  expect_out "OpenCuria is up to date (latest)."
  expect_no_out "What should this host run?"
  [[ "$(env_of /opt/opencuria/.env DB_PASSWORD)" == "${db_password}" ]] || fail "DB password changed"
  [[ "$(env_of /opt/opencuria/runner/.env RUNNER_API_TOKEN)" == "${token}" ]] || fail "token changed"
  expect_no_path "${FAKE}/bootstrap"
  expect_path /var/lib/opencuria/disks/1111.qcow2
  expect_fully_installed
}

test_rerun_menu_offers_update_and_uninstall() {
  install_full
  run_tty '1\n\n'
  expect_status 0
  expect_out "OpenCuria is installed on this host:"
  expect_out "1) Update to the latest version"
  expect_out "2) Uninstall OpenCuria completely"
  expect_out "Workspaces     2 VMs, 1 running"
  expect_no_out "What should this host run?"
  expect_no_out "Runner token"
  expect_out "OpenCuria is up to date"
  expect_fully_installed
}

test_menu_uninstall_removes_everything() {
  install_full
  mkdir -p /opt/opencuria/secrets/push
  run_tty '2\nuninstall\n'
  expect_status 0
  expect_out 'Type "uninstall" to delete everything'
  expect_out "Workspaces: 2 VMs (1 running) and 2 networks"
  expect_out "OpenCuria has been removed from this host."
  expect_no_out "delete it under Settings > Runners" # the control plane is gone too
  expect_uninstalled
  expect_called "undefine opencuria-workspace-1111 --managed-save"
  expect_not_called ".tar.gz"
}

test_menu_uninstall_needs_exact_confirmation() {
  install_full
  run_tty '2\nyes\n'
  expect_status 1
  expect_out "Aborted; nothing was changed."
  expect_fully_installed
  expect_path "${FAKE}/libvirt/domains/opencuria-workspace-1111"
}

test_uninstall_without_terminal_needs_yes() {
  install_full
  run uninstall
  expect_status 1
  expect_out "confirm with --yes"
  expect_fully_installed
}

test_uninstall_after_manual_stops() {
  install_full
  rm -f "${FAKE}/docker/up" "${FAKE}/libvirt/up"
  echo inactive >"${FAKE}/systemd/opencuria-runner"
  sed -i '1s/.*/exited/' "${FAKE}/docker/containers/opencuria_backend"
  touch "${FAKE}/manual_runner"
  run uninstall --yes
  expect_status 0
  expect_out "Starting Docker"
  expect_out "Starting libvirt"
  expect_called "pkill -TERM"
  expect_uninstalled
}

test_uninstall_stops_when_docker_cannot_start() {
  install_full
  rm -f "${FAKE}/docker/up"
  touch "${FAKE}/docker_broken"
  run uninstall --yes
  expect_status 1
  expect_out "Docker is not running and could not be started: Job for docker.service failed"
  expect_path /opt/opencuria/.env
  expect_path "${FAKE}/libvirt/domains/opencuria-workspace-1111"
  expect_path "${FAKE}/docker/volumes/opencuria_pgdata"
  expect_not_called "systemctl stop opencuria-runner"
}

test_uninstall_stops_when_libvirt_cannot_start() {
  install_full
  rm -f "${FAKE}/libvirt/up"
  touch "${FAKE}/libvirt_broken"
  run uninstall --yes
  expect_status 1
  expect_out "libvirt is not running and could not be started"
  expect_fully_installed
}

test_uninstall_without_compose_files() {
  install_full
  rm -f /opt/opencuria/.env /opt/opencuria/compose.yml /opt/opencuria/.bootstrapped
  run uninstall --yes
  expect_status 0
  expect_uninstalled
}

test_uninstall_twice() {
  install_full
  run uninstall --yes
  expect_status 0
  run uninstall --yes
  expect_status 0
  expect_out "nothing to remove"
}

test_uninstall_with_old_virsh() {
  install_full
  touch "${FAKE}/old_virsh"
  run uninstall --yes
  expect_status 0
  expect_uninstalled
}

test_uninstall_keeps_foreign_files_and_custom_paths() {
  install_full
  mkdir -p /opt/opencuria/runtimes /srv/opencuria-disks /srv/snapshots
  touch /srv/snapshots/keep
  {
    echo "RUNNER_QEMU_DISK_DIR=/srv/opencuria-disks"
    echo 'RUNNER_QEMU_SNAPSHOT_DIR="/srv/snapshots"'
  } >>/opt/opencuria/runner/.env
  run uninstall --yes
  expect_status 0
  expect_out "Kept /opt/opencuria: it holds other files (runtimes"
  expect_out "Kept /srv/snapshots"
  expect_path /opt/opencuria/runtimes
  expect_no_path /opt/opencuria/.env
  expect_no_path /opt/opencuria/runner
  expect_no_path /srv/opencuria-disks
  expect_path /srv/snapshots/keep
}

PIN=$'\n# Set by the OpenCuria installer: works next to Docker.\nfirewall_backend = "iptables"'
ORIGINAL_NETWORK_CONF=$'# libvirt network settings\n#firewall_backend = "nftables"'

test_uninstall_keeps_libvirt_pin_next_to_docker() {
  install_full
  printf '%s\n' "${PIN}" >>/etc/libvirt/network.conf
  run uninstall --yes
  expect_status 0
  grep -q 'firewall_backend = "iptables"' /etc/libvirt/network.conf ||
    fail "pin removed although Docker stays"
}

test_uninstall_removes_libvirt_pin_without_docker() {
  run runner --url https://cp.example.com --token abcdefghijklmnop1234 -y
  printf '%s\n' "${PIN}" >>/etc/libvirt/network.conf
  rm -f "${BIN}/docker"
  run uninstall --yes
  expect_status 0
  [[ "$(cat /etc/libvirt/network.conf)" == "${ORIGINAL_NETWORK_CONF}" ]] ||
    fail "network.conf not restored: $(cat /etc/libvirt/network.conf)"
}

test_uninstall_leftovers_only() {
  reset_host
  add_workspace 3333 running
  run
  expect_status 1
  run_tty '3\nhttps://cp.example.com\n\n'
  expect_out "Found remains of an earlier installation"
  run uninstall -y
  expect_status 0
  expect_no_path /var/lib/opencuria
  expect_count "${FAKE}/libvirt/domains" 1
}

test_update_after_manual_stops() {
  install_full
  rm -f "${FAKE}/docker/up" "${FAKE}/libvirt/up"
  echo inactive >"${FAKE}/systemd/opencuria-runner"
  rm -f "${FAKE}/systemd/opencuria-runner.enabled"
  run update
  expect_status 0
  expect_out "Starting Docker"
  expect_path "${FAKE}/docker/up"
  expect_path "${FAKE}/libvirt/up"
  expect_path "${FAKE}/systemd/opencuria-runner.enabled"
  expect_fully_installed
}

test_update_when_docker_cannot_start() {
  install_full
  rm -f "${FAKE}/docker/up"
  touch "${FAKE}/docker_broken"
  run update
  expect_status 1
  expect_out "Docker is not running and could not be started"
  expect_not_called "apt-get"
}

test_update_refuses_without_env_but_with_database() {
  install_full
  rm -f /opt/opencuria/.env
  run update
  expect_status 1
  expect_out "Found the control plane database (Docker volume opencuria_pgdata) but not /opt/opencuria/.env"
  expect_not_called "docker compose"
}

test_update_rejects_incomplete_env() {
  install_full
  sed -i '/^CREDENTIAL_ENCRYPTION_KEY=/d' /opt/opencuria/.env
  run update
  expect_status 1
  expect_out "/opt/opencuria/.env has no CREDENTIAL_ENCRYPTION_KEY"
}

test_update_recreates_missing_database() {
  install_full
  rm -f "${FAKE}/docker/volumes/opencuria_pgdata" "${FAKE}/bootstrap"
  run update
  expect_status 0
  expect_out "a new, empty database is created"
  expect_out "Password:"
  grep -q -- "--admin-email admin@example.com" "${FAKE}/bootstrap" ||
    fail "bootstrap did not reuse the admin email"
  grep -q -- "--runner-name" "${FAKE}/bootstrap" || fail "local runner not registered again"
}

test_update_unmasks_runner() {
  install_full
  ln -sf /dev/null /etc/systemd/system/opencuria-runner.service
  run update
  expect_status 0
  [[ -f /etc/systemd/system/opencuria-runner.service &&
    ! -L /etc/systemd/system/opencuria-runner.service ]] || fail "unit is still masked"
}

test_update_recovers_interrupted_swap() {
  install_full
  mv /opt/opencuria/runner /opt/opencuria/runner.new
  run update
  expect_status 0
  expect_out "Restored /opt/opencuria/runner after an interrupted update"
  expect_path /opt/opencuria/runner/.env
  expect_no_path /opt/opencuria/runner.new
}

test_update_pull_failure_is_reported() {
  install_full
  touch "${FAKE}/pull_fails"
  run update
  expect_status 1
  expect_out "Could not pull the OpenCuria images (latest)"
}

test_dev_install_and_updates_stay_on_dev() {
  run full --dev --domain opencuria.example.com --admin-email admin@example.com \
    --admin-password correct-horse -y
  expect_status 0
  expect_called "archive/dev.tar.gz"
  [[ "$(env_of /opt/opencuria/.env OPENCURIA_TAG)" == dev ]] || fail "control plane not on dev"
  [[ "$(env_of /opt/opencuria/runner/.env OPENCURIA_TAG)" == dev ]] || fail "runner not on dev"
  : >"${FAKE}/calls"
  run update
  expect_status 0
  expect_out "OpenCuria is up to date (dev)."
  expect_called "archive/dev.tar.gz"
  expect_not_called "archive/main.tar.gz"
  [[ "$(env_of /opt/opencuria/.env OPENCURIA_TAG)" == dev ]] || fail "update left dev"
  run_tty '1\n\n'
  expect_status 0
  expect_out "1) Update to the latest dev build"
  expect_no_out "Leaving dev"
}

test_runner_only_stays_on_dev() {
  run runner --dev --url https://cp.example.com --token abcdefghijklmnop1234 -y
  expect_status 0
  : >"${FAKE}/calls"
  run update
  expect_status 0
  expect_called "archive/dev.tar.gz"
}

test_leaving_dev_warns() {
  run full --dev --domain opencuria.example.com --admin-email admin@example.com \
    --admin-password correct-horse -y
  run update --version latest
  expect_status 0
  expect_out "Leaving dev for latest"
  expect_called "archive/main.tar.gz"
  [[ "$(env_of /opt/opencuria/.env OPENCURIA_TAG)" == latest ]] || fail "still on dev"
}

test_installer_of_target_version_does_the_work() {
  touch "${FAKE}/alt_source"
  run server --dev --domain oc.example.com --admin-email admin@example.com -y
  expect_status 0
  expect_out "archive installer: server --dir /opt/opencuria --version dev --domain oc.example.com --admin-email admin@example.com --yes"
  expect_no_out "OpenCuria is ready."
  expect_no_path /opt/opencuria/.env
  local src
  src="$(sed -n 's/^archive source: //p' <<<"${OUT}")"
  [[ -n "${src}" ]] || fail "archive installer got no source directory"
  expect_no_path "${src}"
}

test_unexpected_failure_is_reported() {
  install_full
  touch "${FAKE}/daemon_reload_fails"
  run update
  expect_status 1
  expect_out 'Installing the runner in /opt/opencuria/runner failed: "systemctl daemon-reload" exited with status 1'
  expect_out "run the installer again"
}

test_update_nothing_installed() {
  run update
  expect_status 1
  expect_out "OpenCuria is not installed on this host (/opt/opencuria); nothing to update."
}

test_runner_only_lifecycle() {
  run runner --url https://cp.example.com --token abcdefghijklmnop1234 -y
  expect_status 0
  expect_no_path /opt/opencuria/.env
  [[ "$(env_of /opt/opencuria/runner/.env RUNNER_BACKEND_URL)" == https://cp.example.com ]] ||
    fail "runner URL not written"
  # Workspace storage: QEMU may traverse, other local users cannot list or read.
  local dir
  for dir in /var/lib/opencuria /var/lib/opencuria/{images,base-images,disks,snapshots}; do
    [[ "$(stat -c %a "${dir}")" == 711 ]] || fail "${dir} is not 0711"
  done
  grep -qx 'UMask=0077' /etc/systemd/system/opencuria-runner.service ||
    fail "runner unit does not keep new files private"
  touch "${FAKE}/backend_down"
  run update
  expect_status 1
  expect_out "Cannot reach the control plane at https://cp.example.com"
  rm -f "${FAKE}/backend_down"
  run_tty '1\n\n'
  expect_status 0
  expect_no_out "Control plane URL"
  expect_out "Runner         /opt/opencuria/runner (service active, control plane https://cp.example.com)"
  run uninstall -y
  expect_status 0
  expect_out "delete it under Settings > Runners"
  expect_no_path /opt/opencuria
  expect_count "${FAKE}/docker/containers" 1
}

test_update_keeps_websocket_url() {
  run runner --url https://cp.example.com --token abcdefghijklmnop1234 -y
  sed -i 's|^RUNNER_BACKEND_URL=.*|RUNNER_BACKEND_URL=wss://cp.example.com|' \
    /opt/opencuria/runner/.env
  run update
  expect_status 0
  expect_called "https://cp.example.com/api/v1/health/"
  [[ "$(env_of /opt/opencuria/runner/.env RUNNER_BACKEND_URL)" == wss://cp.example.com ]] ||
    fail "runner URL changed"
}

test_runner_rejected_token() {
  run runner --url https://cp.example.com --token abcdefghijklmnop1234 -y
  touch "${FAKE}/runner_rejected"
  run update
  expect_status 1
  expect_out "The control plane rejected the runner token"
}

test_runner_without_token_explains() {
  install_full
  rm -rf /opt/opencuria/runner
  run update
  expect_status 1
  expect_out "This runner has no token: create one under Settings > Runners"
}

test_server_only_install_and_docker_bootstrap() {
  rm -f "${BIN}/docker"
  run server --domain 203.0.113.7 --admin-email admin@example.com -y
  expect_status 0
  expect_out "Installing Docker"
  expect_out "Password:"
  [[ "$(env_of /opt/opencuria/.env PUBLIC_URL)" == http://203.0.113.7 ]] || fail "wrong PUBLIC_URL"
  grep -q -- "--no-runner" "${FAKE}/bootstrap" || fail "server-only bootstrap registered a runner"
  run_tty '1\n\n'
  expect_status 0
  expect_out "Control plane  http://203.0.113.7 (7 of 7 containers running)"
  expect_no_out "Runner "
}

test_fresh_install_refuses_busy_ports() {
  touch "${FAKE}/port_busy"
  run server --admin-email admin@example.com -y
  expect_status 1
  expect_out "Port 80 is already in use"
}

test_custom_dir_is_found_from_the_unit() {
  run full --dir /srv/oc --domain oc.example.com --admin-email admin@example.com \
    --admin-password correct-horse -y
  expect_status 0
  run uninstall -y
  expect_status 0
  expect_out "Found the installation in /srv/oc"
  expect_no_path /srv/oc
}

test_invalid_arguments() {
  run --dir /
  expect_status 1
  expect_out "Invalid --dir"
  run update uninstall
  expect_status 1
  expect_out "Pass only one action"
}

test_wizard_hides_the_push_proxy_without_flag() {
  run_tty '9\n'
  expect_status 1
  expect_out "3) Runner only"
  expect_no_out "4)"
  if grep -qi push <<<"${OUT}"; then fail "the wizard mentions the push proxy"; fi
  run --help
  expect_out "--proxy"
}

test_wizard_with_proxy_offers_proxy_choices() {
  run_tty '9\n' --proxy
  expect_status 1
  expect_out "1) Everything: control plane, push proxy and a runner, wired together"
  expect_out "2) Control plane and push proxy"
  expect_out "3) Runner only"
  expect_out "4) Push proxy only"
}

test_full_install_with_proxy() {
  install_full_with_proxy
  expect_fully_installed
  expect_proxy_colocated
  expect_out "Push proxy: https://push.example.com"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_DOMAIN)" == push.example.com ]] || fail "domain not written"
  [[ "$(env_of "${PUSH_ENV}" APNS_KEY_ID)" == KEYID12345 ]] || fail "key ID not taken from the file name"
  [[ "$(env_of "${PUSH_ENV}" APNS_TEAM_ID)" == TEAM123456 ]] || fail "team ID not written"
  [[ "$(env_of "${PUSH_ENV}" APNS_APP_IDS)" == com.opencuria.mobile,com.opencuria.mobile.dev ]] ||
    fail "the published app's IDs are not the default"
  [[ -z "$(env_of "${PUSH_ENV}" FCM_SERVICE_ACCOUNT_FILE)" ]] || fail "FCM set up without a key"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_HANDLE_KEYS | base64 -d | wc -c)" -eq 64 ]] ||
    fail "the handle key is not 64 random bytes"
  [[ "$(stat -c '%a %u' "${PUSH_ENV}")" == "600 0" ]] || fail "push proxy .env is not private"
  [[ "$(stat -c '%a %u' /opt/opencuria/push-proxy/secrets)" == "700 10001" ]] ||
    fail "secrets/ is not private to the proxy's user"
  [[ "$(stat -c '%a %u' /opt/opencuria/push-proxy/secrets/apns.p8)" == "600 10001" ]] ||
    fail "the APNs key is not private to the proxy's user"
  cmp -s "${APNS_KEY}" /opt/opencuria/push-proxy/secrets/apns.p8 || fail "APNs key not copied"
  [[ "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" == https://push.example.com ]] ||
    fail "the control plane does not send through its push proxy"
}

test_wizard_server_with_proxy() {
  make_push_keys
  # Choice, domain, admin email, password, push domain, APNs key, key ID, team ID,
  # Firebase key, app IDs, confirmation.
  run_tty "2\noc.example.com\nadmin@example.com\n\npush.example.com\n${APNS_KEY}\n\nTEAM123456\n${FCM_KEY}\n\n\n" --proxy
  expect_status 0
  expect_out "Push proxy in /opt/opencuria/push-proxy: https://push.example.com, for iOS and Android apps com.opencuria.mobile,com.opencuria.mobile.dev"
  expect_out "The control plane serves the push proxy's domain and sends mobile push through it"
  expect_out "The push proxy needs DNS for push.example.com pointing here"
  expect_out "Apps use it when built with PUSH_PROXY_URL=https://push.example.com"
  expect_no_path /opt/opencuria/runner
  expect_proxy_colocated
  [[ "$(env_of "${PUSH_ENV}" FCM_APP_IDS)" == com.opencuria.mobile,com.opencuria.mobile.dev ]] ||
    fail "FCM app IDs missing"
  [[ "$(stat -c '%a %u' /opt/opencuria/push-proxy/secrets/firebase.json)" == "600 10001" ]] ||
    fail "the Firebase key is not private to the proxy's user"
  grep -q -- "--no-runner" "${FAKE}/bootstrap" || fail "server bootstrap registered a runner"
}

test_proxy_only_lifecycle() {
  make_push_keys
  run proxy --proxy-domain https://Push.Example.com/ --fcm-key "${FCM_KEY}" -y
  expect_status 0
  expect_out "OpenCuria is ready."
  expect_out "Push proxy: https://push.example.com"
  expect_no_out "Web app:"
  expect_no_out "Build an image"
  expect_not_called "apt-get"
  expect_no_path /opt/opencuria/.env
  expect_no_path /opt/opencuria/runner
  [[ "$(env_of "${PUSH_ENV}" COMPOSE_FILE)" == compose.yml &&
    "$(env_of "${PUSH_ENV}" COMPOSE_PROFILES)" == tls ]] || fail "no HTTPS proxy of its own"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_DOMAIN)" == push.example.com ]] || fail "domain not normalized"
  [[ -z "$(env_of "${PUSH_ENV}" APNS_TEAM_ID)" ]] || fail "APNs set up without a key"
  expect_path "${FAKE}/docker/containers/opencuria_push_caddy"
  local key
  key="$(env_of "${PUSH_ENV}" PUSH_PROXY_HANDLE_KEYS)"
  : >"${FAKE}/calls"
  run_tty '1\n\n'
  expect_status 0
  expect_out "Push proxy     https://push.example.com (2 of 2 containers running)"
  expect_no_out "Control plane  "
  expect_out "OpenCuria is up to date (latest)."
  expect_called "compose[opencuria-push] pull"
  expect_not_called "compose[opencuria] "
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_HANDLE_KEYS)" == "${key}" ]] || fail "update replaced the handle key"
  run uninstall -y
  expect_status 0
  expect_out "Push proxy: 2 containers, 2 volumes"
  expect_out "Removing the push proxy"
  expect_no_out "delete it under Settings > Runners"
  expect_uninstalled
}

test_proxy_only_refuses_busy_ports() {
  make_push_keys
  touch "${FAKE}/port_busy"
  run proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 1
  expect_out "Port 80 is already in use. Free it, or run the push proxy behind your own reverse proxy"
}

test_update_updates_an_installed_proxy() {
  install_full_with_proxy
  local key
  key="$(env_of "${PUSH_ENV}" PUSH_PROXY_HANDLE_KEYS)"
  run update --dev
  expect_status 0
  expect_out "Push proxy in /opt/opencuria/push-proxy: image dev, https://push.example.com"
  expect_called "compose[opencuria-push] pull"
  expect_called "compose[opencuria-push] up"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_TAG)" == dev ]] || fail "push proxy not on dev"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_HANDLE_KEYS)" == "${key}" ]] || fail "update replaced the handle key"
  expect_proxy_colocated
  expect_no_path "${FAKE}/caddy_reloads" # unchanged Caddyfile
  run update --proxy-domain other.example.com
  expect_status 0
  expect_out "Push proxy options are ignored for an existing push proxy"
  [[ "$(env_of "${PUSH_ENV}" PUSH_PROXY_DOMAIN)" == push.example.com ]] || fail "domain changed"
}

test_update_with_proxy_adds_it_to_the_control_plane() {
  install_full
  make_push_keys
  run_tty '9\n' --proxy
  expect_out "1) Update to the latest version and add the push proxy, keeping configuration and data"
  local inode
  inode="$(stat -c %i /opt/opencuria/Caddyfile)"
  run update --proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}"
  expect_status 0
  # The running Caddy bind-mounts the file: a new inode would hide the new site.
  [[ "$(stat -c %i /opt/opencuria/Caddyfile)" == "${inode}" ]] || fail "Caddyfile replaced, not rewritten"
  expect_out "Push proxy in /opt/opencuria/push-proxy: https://push.example.com, for Android apps"
  expect_proxy_colocated
  expect_path "${FAKE}/caddy_reloads"
  [[ "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" == https://push.example.com ]] ||
    fail "the control plane does not send through its push proxy"
  expect_fully_installed
  expect_path /var/lib/opencuria/disks/1111.qcow2
}

test_proxy_action_on_a_control_plane_updates_both() {
  run server --domain oc.example.com --admin-email admin@example.com -y
  make_push_keys
  run proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 0
  expect_out "The control plane on this host serves the push proxy; updating it as well."
  expect_proxy_colocated
  expect_path "${FAKE}/caddy_reloads"
}

test_server_takes_over_a_standalone_proxy() {
  make_push_keys
  run proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  run server --domain oc.example.com --admin-email admin@example.com -y
  expect_status 0
  expect_out "Handing ports 80/443 of the push proxy to the control plane"
  expect_proxy_colocated
  [[ "$(env_of /opt/opencuria/.env PUSH_PROXY_URL)" == https://push.example.com ]] ||
    fail "the new control plane does not send through the push proxy on its host"
}

test_proxy_settings_are_validated() {
  make_push_keys
  run proxy --proxy-domain push.example.com -y
  expect_status 1
  expect_out "needs an APNs key (--apns-key) and/or a Firebase service account (--fcm-key)"
  run proxy --proxy-domain 203.0.113.9 --fcm-key "${FCM_KEY}" -y
  expect_status 1
  expect_out "The push proxy needs its own domain name (--proxy-domain)"
  run server --domain oc.example.com --admin-email admin@example.com \
    --proxy-domain oc.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 1
  expect_out "needs its own domain, not the control plane's (oc.example.com)"
  run proxy --proxy-domain push.example.com --apns-key "${FCM_KEY}" --apns-team-id TEAM123456 -y
  expect_status 1
  expect_out "is not an APNs auth key (.p8)"
  run proxy --proxy-domain push.example.com --apns-key "${APNS_KEY}" -y
  expect_status 1
  expect_out "A valid Apple team ID (10 characters) is required (--apns-team-id)."
  run proxy --proxy-domain push.example.com --fcm-key "${APNS_KEY}" -y
  expect_status 1
  expect_out "is not a Firebase service account"
  run proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}" --push-app-ids 'a b;c' -y
  expect_status 1
  expect_out "Invalid app IDs"
  expect_no_path /opt/opencuria
}

test_runner_refuses_the_proxy() {
  run runner --proxy --url https://cp.example.com --token abcdefghijklmnop1234 -y
  expect_status 1
  expect_out "--proxy adds the push proxy to full, server or update"
  run runner --url https://cp.example.com --token abcdefghijklmnop1234 -y
  run update --proxy -y
  expect_status 1
  expect_out "This host runs a runner only"
  expect_no_path /opt/opencuria/push-proxy
}

test_push_proxy_failure_shows_its_log() {
  make_push_keys
  touch "${FAKE}/push_unhealthy"
  run proxy --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 1
  expect_out "ConfigError: APNS_PRIVATE_KEY_FILE"
  expect_out "The push proxy did not become healthy (log above)."
}

test_uninstall_removes_the_colocated_proxy() {
  install_full_with_proxy
  add_workspace 1111 running
  run uninstall --yes
  expect_status 0
  expect_out "Push proxy: 1 container, 0 volumes"
  expect_out "Removing the control plane and the push proxy"
  expect_uninstalled
}

test_installer_of_target_version_gets_the_proxy_settings() {
  touch "${FAKE}/alt_source"
  run proxy --dev --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 0
  expect_out "archive installer: proxy --dir /opt/opencuria --version dev --proxy --proxy-domain push.example.com --fcm-key ${FCM_KEY} --yes"
  touch "${FAKE}/old_source"
  run proxy --version abc1234 --proxy-domain push.example.com --fcm-key "${FCM_KEY}" -y
  expect_status 1
  expect_out "OpenCuria abc1234 cannot install the push proxy; choose a newer version."
}

# -- runner ------------------------------------------------------------------------

mapfile -t TESTS < <(declare -F | awk '{print $3}' | grep '^test_')
[[ $# -eq 0 ]] || TESTS=("$@")
for test in "${TESTS[@]}"; do
  CURRENT="${test}"
  reset_host
  : >"${FAKE}/calls"
  before="${FAILURES}"
  "${test}"
  if [[ "${FAILURES}" -eq "${before}" ]]; then printf '  ok   %s\n' "${test}"; fi
done
printf '\n%s tests, %s failures\n' "${#TESTS[@]}" "${FAILURES}"
[[ "${FAILURES}" -eq 0 ]]
