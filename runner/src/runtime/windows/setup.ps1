# OpenCuria Windows base provisioning. Runs once as SYSTEM from SetupComplete.cmd
# after Windows Setup, installs the pinned tools from the setup media and powers
# off. Every step is idempotent so a retried build reaches the same state.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$StateDir = 'C:\ProgramData\opencuria'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$Log = Join-Path $StateDir 'setup.log'

function Write-Serial([string] $Line) {
    try {
        $port = New-Object System.IO.Ports.SerialPort 'COM1', 115200
        $port.Open()
        $port.Write($Line + "`r`n")
        # Closing discards unsent bytes: drain the driver buffer and the UART.
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        while ($port.BytesToWrite -gt 0 -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 20
        }
        Start-Sleep -Milliseconds 100
        $port.Close()
    } catch { }
}

function Log([string] $Message) {
    $line = "opencuria-setup: $Message"
    Add-Content -Path $Log -Value ("{0:o} {1}" -f (Get-Date), $Message)
    Write-Serial $line
}

function Invoke-Checked([string] $File, [string[]] $Arguments) {
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -Wait -PassThru -NoNewWindow
    if ($process.ExitCode -notin 0, 3010) {
        throw "$File exited with $($process.ExitCode)"
    }
}

function Find-Media([string] $RelativePath) {
    foreach ($volume in Get-Volume) {
        if (-not $volume.DriveLetter) { continue }
        $candidate = '{0}:\{1}' -f $volume.DriveLetter, $RelativePath
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    throw "Setup media file not found: $RelativePath"
}

try {
    Log 'started'
    $manifest = Get-Content -Raw (Find-Media 'opencuria\media.json') | ConvertFrom-Json

    # Paravirtual drivers, QEMU guest agent and SPICE agent (clipboard).
    Log 'installing virtio guest tools'
    Invoke-Checked (Find-Media 'virtio-win-guest-tools.exe') @('/install', '/quiet', '/norestart')

    Log 'installing OpenSSH server'
    Invoke-Checked 'msiexec.exe' @('/i', (Find-Media "installers\$($manifest.openssh)"), '/qn', '/norestart')

    Log 'installing MSYS2'
    $msys = 'C:\msys64'
    if (-not (Test-Path "$msys\usr\bin\bash.exe")) {
        Invoke-Checked (Find-Media "installers\$($manifest.msys2)") @('-y', '-oC:\')
    }
    New-Item -ItemType Directory -Force -Path 'C:\workspace' | Out-Null
    # /workspace and /root resolve to the same directories in bash and in native
    # programs started from it, so guest paths are identical to Linux guests.
    # /root keeps POSIX permissions (ssh refuses keys others can read).
    Set-Content -Encoding ascii -Path "$msys\etc\fstab" -Value @(
        'none / cygdrive binary,posix=0,noacl,user 0 0',
        'C:/workspace /workspace ntfs binary,posix=0,noacl,user 0 0',
        'C:/Users/root /root ntfs binary,posix=0,user 0 0'
    )
    $nsswitch = Get-Content "$msys\etc\nsswitch.conf" | Where-Object { $_ -notmatch '^db_home:' }
    Set-Content -Encoding ascii -Path "$msys\etc\nsswitch.conf" -Value ($nsswitch + 'db_home: windows')
    [Environment]::SetEnvironmentVariable('MSYS2_PATH_TYPE', 'inherit', 'Machine')
    [Environment]::SetEnvironmentVariable('CHERE_INVOKING', '1', 'Machine')
    [Environment]::SetEnvironmentVariable('MSYS', 'winsymlinks:nativestrict', 'Machine')
    [Environment]::SetEnvironmentVariable('LANG', 'C.UTF-8', 'Machine')
    [Environment]::SetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', "$msys\usr\bin\bash.exe", 'Machine')
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($machinePath -notlike "*$msys\usr\bin*") {
        [Environment]::SetEnvironmentVariable('Path', "$machinePath;$msys\usr\bin", 'Machine')
    }
    # Native tools print UTF-8, matching the runner's decoding of guest output.
    $nls = 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage'
    Set-ItemProperty $nls -Name ACP -Value '65001'
    Set-ItemProperty $nls -Name OEMCP -Value '65001'
    Set-ItemProperty $nls -Name MACCP -Value '65001'

    $bash = "$msys\usr\bin\bash.exe"
    function Invoke-Bash([string] $Script) {
        # Windows PowerShell turns native stderr lines into terminating errors
        # under 'Stop'; MSYS2 tools report progress and warnings there.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $bash -lc $Script 2>&1 | ForEach-Object { Log "msys: $_" }
        } finally {
            $ErrorActionPreference = $previous
        }
        return $LASTEXITCODE
    }
    function Invoke-Msys([string] $Script, [int] $Attempts = 1) {
        for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
            if ((Invoke-Bash $Script) -eq 0) { return }
            Log "msys command failed ($LASTEXITCODE), attempt $attempt of $Attempts"
            Start-Sleep -Seconds 15
        }
        throw "MSYS2 command failed: $Script"
    }
    Log 'waiting for network'
    for ($i = 0; $i -lt 60 -and -not (Test-Connection -ComputerName repo.msys2.org -Count 1 -Quiet); $i++) {
        Start-Sleep -Seconds 5
    }
    Log 'initializing MSYS2'
    Invoke-Msys 'true'
    # The first upgrade replaces the runtime and ends every MSYS2 process.
    Invoke-Bash 'pacman --noconfirm --noprogressbar -Syuu' | Out-Null
    Invoke-Msys 'pacman --noconfirm --noprogressbar -Syuu' 3
    Invoke-Msys ("pacman --noconfirm --noprogressbar -S --needed " + ($manifest.msys2_packages -join ' ')) 3
    Invoke-Msys 'pacman --noconfirm -Scc >/dev/null; rm -rf /var/cache/pacman/pkg/*'
    Invoke-Bash 'for tool in python3 git tar base64 pgrep setsid flock; do command -v "$tool" || echo "missing: $tool"; done' | Out-Null

    Log 'configuring sshd'
    $sshd = 'C:\ProgramData\ssh'
    New-Item -ItemType Directory -Force -Path $sshd | Out-Null
    Set-Content -Encoding ascii -Path "$sshd\sshd_config" -Value @(
        'Port 22',
        'PubkeyAuthentication yes',
        'PasswordAuthentication no',
        'PermitEmptyPasswords no',
        'KbdInteractiveAuthentication no',
        'MaxSessions 64',
        'MaxStartups 64:30:128',
        'AllowUsers root',
        'Subsystem sftp sftp-server.exe',
        'Match Group administrators',
        '    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys'
    )
    New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value "$msys\usr\bin\bash.exe"
    Set-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShellCommandOption -Value '-c'
    Set-Service sshd -StartupType Automatic
    if (-not (Get-NetFirewallRule -Name 'OpenCuria-SSH' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name 'OpenCuria-SSH' -DisplayName 'OpenCuria SSH' `
            -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
    }

    Log 'configuring session and power policy'
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '1'
    Set-ItemProperty $winlogon -Name DefaultUserName -Value 'root'
    Set-ItemProperty $winlogon -Name DefaultPassword -Value ''
    Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
    New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' -Name NoLockScreen -Value 1 -Type DWord
    Set-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name InactivityTimeoutSecs -Value 0 -Type DWord
    powercfg.exe /hibernate off
    powercfg.exe /change monitor-timeout-ac 0
    powercfg.exe /change standby-timeout-ac 0
    powercfg.exe /change disk-timeout-ac 0
    # ACPI power button shuts down immediately, so libvirt stops are graceful.
    powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3
    powercfg.exe /setactive SCHEME_CURRENT
    $system = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
    Set-ItemProperty $system -Name shutdownwithoutlogon -Value 1 -Type DWord
    Set-ItemProperty $system -Name EnableFirstLogonAnimation -Value 0 -Type DWord
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control' -Name WaitToKillServiceTimeout -Value '5000'
    # The account profile is created from the default hive on first logon:
    # running applications never block a stop.
    reg.exe load 'HKU\OpenCuriaDefault' 'C:\Users\Default\NTUSER.DAT' | Out-Null
    try {
        $desktop = 'Registry::HKEY_USERS\OpenCuriaDefault\Control Panel\Desktop'
        Set-ItemProperty $desktop -Name AutoEndTasks -Value '1'
        Set-ItemProperty $desktop -Name WaitToKillAppTimeout -Value '2000'
        Set-ItemProperty $desktop -Name HungAppTimeout -Value '2000'
        $engagement = 'Registry::HKEY_USERS\OpenCuriaDefault\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement'
        New-Item -Path $engagement -Force | Out-Null
        Set-ItemProperty $engagement -Name ScoobeSystemSettingEnabled -Value 0 -Type DWord
    } finally {
        [GC]::Collect()
        reg.exe unload 'HKU\OpenCuriaDefault' | Out-Null
    }

    # Workspaces never reboot on their own; images opt into updates explicitly.
    $au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU'
    New-Item -Path $au -Force | Out-Null
    Set-ItemProperty $au -Name NoAutoUpdate -Value 1 -Type DWord
    Set-ItemProperty $au -Name NoAutoRebootWithLoggedOnUsers -Value 1 -Type DWord
    # Store app updates make sysprep fail for per-user packages.
    New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' -Name AutoDownload -Value 2 -Type DWord
    New-Item -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Force | Out-Null
    Set-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' -Name DisableWindowsConsumerFeatures -Value 1 -Type DWord
    Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' -Name PreventDeviceEncryption -Value 1 -Type DWord
    Add-MpPreference -ExclusionPath 'C:\workspace', "$msys\opt\opencuria" -ErrorAction SilentlyContinue

    Log 'installing bootstrap'
    Copy-Item -Force (Join-Path $PSScriptRoot 'bootstrap.ps1') "$StateDir\bootstrap.ps1"
    Copy-Item -Force (Join-Path $PSScriptRoot 'generalize.ps1') "$StateDir\generalize.ps1"
    Copy-Item -Force (Join-Path $PSScriptRoot 'generalize-unattend.xml') "$StateDir\generalize-unattend.xml"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$StateDir\bootstrap.ps1`""
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable
    Register-ScheduledTask -TaskName 'OpenCuria Bootstrap' -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null

    # Setup places WinRE after C:; without it C: can always grow with the disk.
    Log 'removing the recovery partition'
    reagentc.exe /disable | Out-Null
    $recovery = '{de94bba4-06d1-4d40-a16a-bfd50179d6ac}'
    Get-Partition -DiskNumber (Get-Partition -DriveLetter C).DiskNumber |
        Where-Object { $_.GptType -eq $recovery } |
        Remove-Partition -Confirm:$false
    $size = (Get-PartitionSupportedSize -DriveLetter C).SizeMax
    if ($size -gt (Get-Partition -DriveLetter C).Size) { Resize-Partition -DriveLetter C -Size $size }

    Log 'cleaning up'
    Dism.exe /Online /Cleanup-Image /StartComponentCleanup /ResetBase | Out-Null
    compact.exe /CompactOS:always | Out-Null
    # Freed clusters become holes in the qcow2 disk (discard=unmap).
    Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue | Out-Null
    Log 'completed'
    Write-Serial 'OPENCURIA_SETUP_OK'
} catch {
    Log "failed: $($_.Exception.Message)"
    Write-Serial "OPENCURIA_SETUP_FAILED $($_.Exception.Message)"
}
Stop-Computer -Force
