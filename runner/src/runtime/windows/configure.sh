# OpenCuria Windows guest configuration (MSYS2 bash, run by every image build).
#
# Idempotent settings that do not need a new base installation. The runner
# pipes this script to "bash -s" before the image recipe runs.
set -eu

# Non-login shells started by sshd inherit the Windows PATH, where System32's
# find, sort, tar and curl would shadow the GNU tools runner commands expect.
cat > /etc/opencuria-bashenv <<'EOF'
case "$PATH" in
  /usr/local/bin:/usr/bin:/bin:*) ;;
  *) PATH="/usr/local/bin:/usr/bin:/bin:$PATH" ;;
esac
EOF
powershell.exe -NoProfile -NonInteractive -Command \
  "[Environment]::SetEnvironmentVariable('BASH_ENV', '/etc/opencuria-bashenv', 'Machine')"

# Line endings and file modes behave as on Linux guests.
git config --system core.autocrlf false
git config --system core.filemode false
git config --system core.symlinks true
git config --system init.defaultBranch main

mkdir -p /workspace /c/ProgramData/opencuria

# The desktop size belongs to root's console session: the runner asks this
# task (also run at every logon) to apply display.json there.
# MSYS2 would rewrite "/tn"-style switches as paths for native programs.
MSYS2_ARG_CONV_EXCL='*' schtasks.exe /create /f /tn 'OpenCuria Display' /sc onlogon /ru root /it /rl highest \
  /tr 'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\ProgramData\opencuria\display.ps1' \
  >/dev/null
echo "opencuria-configure: done"
