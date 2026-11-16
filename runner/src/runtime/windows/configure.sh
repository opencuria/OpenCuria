# OpenCuria Windows guest configuration (MSYS2 bash, run by every image build).
#
# Idempotent settings that do not need a new base installation. The runner
# pipes this script to "bash -s" before the image recipe runs.
set -eu

# Line endings and file modes behave as on Linux guests.
git config --system core.autocrlf false
git config --system core.filemode false
git config --system core.symlinks true
git config --system init.defaultBranch main

mkdir -p /workspace /c/ProgramData/opencuria

# MSYS2 would rewrite "/tn"-style switches as paths for native programs.
export MSYS2_ARG_CONV_EXCL='*'

# Desktop size and clipboard belong to root's console session: the runner
# starts these tasks there (the display task also runs at every logon).
session_task() {
  name=$1 script=$2
  shift 2
  schtasks.exe /create /f /tn "$name" /ru root /it /rl highest "$@" \
    /tr "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\\ProgramData\\opencuria\\$script" \
    >/dev/null 2>&1
}
session_task 'OpenCuria Display' display.ps1 /sc onlogon
session_task 'OpenCuria Clipboard' clipboard.ps1 /sc once /st 00:00

# No OneDrive or backup prompts on the desktop agents and users share.
reg.exe add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\OneDrive' /v DisableFileSyncNGSC /t REG_DWORD /d 1 /f >/dev/null
echo "opencuria-configure: done"
