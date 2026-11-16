# OpenCuria Windows guest bootstrap. Runs as SYSTEM at every startup.
#
# Reads the NoCloud seed volume ("cidata") that the runner attaches to every
# guest. A new instance-id (fresh workspace, clone or reset) regenerates the SSH
# host keys and installs the runner key; the runner can only authenticate after
# that, so its first trusted host key is always the final one. A seed with
# action "generalize" prepares a copy for capture and powers off instead.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$StateDir = 'C:\ProgramData\opencuria'
$SshDir = 'C:\ProgramData\ssh'
$SshKeygen = 'C:\Program Files\OpenSSH\ssh-keygen.exe'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

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
    Add-Content -Path "$StateDir\bootstrap.log" -Value ("{0:o} {1}" -f (Get-Date), $Message)
    Write-Serial "opencuria-bootstrap: $Message"
}

function Set-Status([string] $InstanceId, [string] $Status, [string] $Detail = '') {
    $temp = "$StateDir\bootstrap.json.tmp"
    [ordered]@{
        instance_id = $InstanceId
        status = $Status
        detail = $Detail
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Compress | Set-Content -Encoding ascii -Path $temp
    Move-Item -Force $temp "$StateDir\bootstrap.json"
}

function Protect-AdminFile([string] $Path) {
    # sshd refuses keys that other principals can read or write.
    icacls.exe $Path /inheritance:r /grant 'SYSTEM:F' /grant '*S-1-5-32-544:F' | Out-Null
}

function Wait-SetupComplete {
    while ($true) {
        $setup = Get-ItemProperty 'HKLM:\SYSTEM\Setup'
        $oobe = Get-ItemProperty 'HKLM:\SYSTEM\Setup' -Name OOBEInProgress -ErrorAction SilentlyContinue
        if ($setup.SystemSetupInProgress -eq 0 -and -not ($oobe -and $oobe.OOBEInProgress -eq 1)) { return }
        Start-Sleep -Seconds 2
    }
}

function Find-Seed {
    for ($i = 0; $i -lt 60; $i++) {
        $volume = Get-Volume | Where-Object { $_.FileSystemLabel -eq 'cidata' } | Select-Object -First 1
        if ($volume) { return $volume }
        Start-Sleep -Seconds 1
    }
    throw 'Seed volume cidata not found'
}

function Read-SeedFile($Volume, [string] $Name) {
    # The volume path works with or without a drive letter.
    return [IO.File]::ReadAllText($Volume.Path + $Name)
}

function Hide-Seed($Volume) {
    # Like the seed device of Linux guests, the volume is not a user drive.
    try {
        if ($Volume.DriveLetter) {
            mountvol.exe ('{0}:' -f $Volume.DriveLetter) /D | Out-Null
        }
    } catch { Log "seed drive stays visible: $($_.Exception.Message)" }
}

function Set-ConsoleSession {
    # The desktop is the console session of root; it never locks or asks.
    $winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
    Set-ItemProperty $winlogon -Name AutoAdminLogon -Value '1'
    Set-ItemProperty $winlogon -Name DefaultUserName -Value 'root'
    Set-ItemProperty $winlogon -Name DefaultPassword -Value ''
    Set-ItemProperty $winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME
    Remove-ItemProperty $winlogon -Name AutoLogonCount -ErrorAction SilentlyContinue
}

function Expand-SystemVolume {
    # Workspace disks may grow between starts; C: is always the last partition.
    Update-HostStorageCache
    $partition = Get-Partition -DriveLetter C
    $supported = Get-PartitionSupportedSize -DriveLetter C
    if ($supported.SizeMax - $partition.Size -gt 16MB) {
        Resize-Partition -DriveLetter C -Size $supported.SizeMax
        Log "extended C: to $($supported.SizeMax) bytes"
    }
}

function Initialize-Instance([string] $InstanceId, $Seed) {
    Log "initializing instance $InstanceId"
    Stop-Service sshd -Force -ErrorAction SilentlyContinue
    Get-ChildItem -Path $SshDir -Filter 'ssh_host_*' -ErrorAction SilentlyContinue | Remove-Item -Force
    Push-Location $SshDir
    try { & $SshKeygen -A | Out-Null } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw 'ssh-keygen -A failed' }
    Get-ChildItem -Path $SshDir -Filter 'ssh_host_*_key' | ForEach-Object { Protect-AdminFile $_.FullName }
    $keys = "$SshDir\administrators_authorized_keys"
    Set-Content -Encoding ascii -Path $keys -Value @($Seed.ssh_authorized_keys)
    Protect-AdminFile $keys
    Set-Content -Encoding ascii -Path "$StateDir\instance-id" -Value $InstanceId
}

$instanceId = ''
try {
    Wait-SetupComplete
    $seedVolume = Find-Seed
    $meta = (Read-SeedFile $seedVolume 'meta-data') -split "\r?\n"
    $instanceId = (($meta | Where-Object { $_ -match '^instance-id:' }) -replace '^instance-id:\s*', '').Trim()
    if (-not $instanceId) { throw 'Seed has no instance-id' }
    $seed = Read-SeedFile $seedVolume 'opencuria.json' | ConvertFrom-Json
    Hide-Seed $seedVolume

    if ($seed.action -eq 'generalize') {
        Log 'generalizing for capture'
        & "$StateDir\generalize.ps1"
        return
    }

    Set-ConsoleSession
    $previous = ''
    if (Test-Path "$StateDir\instance-id") {
        $previous = (Get-Content -Raw "$StateDir\instance-id").Trim()
    }
    if ($previous -ne $instanceId) {
        Initialize-Instance $instanceId $seed
    }
    Expand-SystemVolume
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    Set-Status $instanceId 'ready'
    Log 'ready'
} catch {
    Log "failed: $($_.Exception.Message)"
    Set-Status $instanceId 'failed' $_.Exception.Message
    Write-Serial "OPENCURIA_BOOTSTRAP_FAILED $($_.Exception.Message)"
}
