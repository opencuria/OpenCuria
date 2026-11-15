"""cloud-init seed files and sshd session cap for Linux guests."""

from __future__ import annotations

_CLOUD_INIT_META = """\
instance-id: {bootstrap_id}
local-hostname: opencuria-workspace
"""

# Every exec, upload, terminal and agent stream shares one SSH connection per
# VM (see ``QemuRuntime._get_ssh``), so sshd's default of 10 sessions per
# connection is raised. write_files runs before sshd starts: the runner's first
# connection predates runcmd's ssh restart and keeps its boot-time limits. sshd
# uses the first value it reads, so the drop-in sorts before the image's own
# files. Guests seeded before the drop-in existed receive it on the runner's
# next connection (see ``QemuRuntime._ensure_guest_session_cap``).
GUEST_SSH_MAX_SESSIONS = 64
GUEST_SSH_SESSIONS_CONF = "/etc/ssh/sshd_config.d/10-opencuria-sessions.conf"
GUEST_SSH_SESSIONS_RELOADED = "opencuria-sshd-reloaded"
# Install the drop-in once, validate it, and reload sshd. Reloading keeps open
# connections (and their limits); only connections opened afterwards get it.
GUEST_SSH_SESSIONS_SCRIPT = f"""\
PATH="$PATH:/usr/sbin:/sbin"
conf={GUEST_SSH_SESSIONS_CONF}
[ -f "$conf" ] && exit 0
[ -d "${{conf%/*}}" ] || exit 0
printf 'MaxSessions {GUEST_SSH_MAX_SESSIONS}\\n' > "$conf.tmp" || exit 1
chmod 0644 "$conf.tmp" && mv -f "$conf.tmp" "$conf" || exit 1
sshd -t >/dev/null 2>&1 || {{ rm -f "$conf"; exit 1; }}
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || exit 1
echo {GUEST_SSH_SESSIONS_RELOADED}
"""

_CLOUD_INIT_USER = """\
#cloud-config
users:
  - name: ubuntu
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    shell: /bin/bash
    ssh_authorized_keys:
      - {ssh_public_key}
write_files:
  - path: /root/.ssh/authorized_keys
    permissions: '0600'
    content: |
      {ssh_public_key}
  - path: {sessions_conf}
    permissions: '0644'
    content: |
      MaxSessions {max_sessions}
runcmd:
  - mkdir -p /workspace
  - mkdir -p /root/.ssh
  - chmod 700 /root/.ssh
  - chmod 600 /root/.ssh/authorized_keys
  - echo 'PermitRootLogin prohibit-password' > /etc/ssh/sshd_config.d/99-opencuria-root.conf
  - systemctl restart ssh
  - tune2fs -e continue /dev/vda1
"""

# Cloud-init v2 (netplan) network config for static IP assignment inside the VM.
# Applied at first boot so the VM always comes up with a predictable
# address, independent of any DHCP server.
# ``match: name: "en*"`` covers both enp1s0 (q35/PCI) and ens3 (i440fx/ISA).
_CLOUD_INIT_NETWORK = """\
version: 2
ethernets:
  id0:
    match:
      name: "en*"
    addresses:
      - {vm_ip}/30
    routes:
      - to: default
        via: {gateway}
    nameservers:
      addresses:
        - {gateway}
"""


def cloud_init_files(
    *, bootstrap_id: str, public_key: str, vm_ip: str | None, gateway: str | None
) -> dict[str, str]:
    """Return ``user-data``, ``meta-data`` and, with an address, ``network-config``."""
    files = {
        "user-data": _CLOUD_INIT_USER.format(
            ssh_public_key=public_key,
            sessions_conf=GUEST_SSH_SESSIONS_CONF,
            max_sessions=GUEST_SSH_MAX_SESSIONS,
        ),
        "meta-data": _CLOUD_INIT_META.format(bootstrap_id=bootstrap_id),
    }
    if vm_ip and gateway:
        files["network-config"] = _CLOUD_INIT_NETWORK.format(vm_ip=vm_ip, gateway=gateway)
    return files
