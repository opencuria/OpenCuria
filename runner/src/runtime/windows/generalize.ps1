# Generalize this Windows guest and power off.
#
# Removes everything that identifies one workspace (SSH host keys, the runner
# key, the installed product key, the bootstrap identity), then runs sysprep so
# every guest created from the published disk gets its own machine SID and
# licensing identity. The result is reported on COM1 because the guest has no
# network while it is generalized for a capture.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$StateDir = 'C:\ProgramData\opencuria'

function Write-Serial([string] $Line) {
    try {
        $port = New-Object System.IO.Ports.SerialPort 'COM1', 115200
        $port.Open(); $port.WriteLine($Line); $port.Close()
    } catch { }
}

try {
    Stop-Service sshd -Force -ErrorAction SilentlyContinue
    Remove-Item -Force -ErrorAction SilentlyContinue `
        'C:\ProgramData\ssh\ssh_host_*', 'C:\ProgramData\ssh\administrators_authorized_keys', `
        "$StateDir\instance-id", "$StateDir\bootstrap.json", "$StateDir\license.json"

    # Product keys belong to one workspace and never travel with an image.
    $slmgr = Join-Path $env:SystemRoot 'System32\slmgr.vbs'
    cscript.exe //Nologo $slmgr /upk | Out-Null
    cscript.exe //Nologo $slmgr /cpky | Out-Null
    cscript.exe //Nologo $slmgr /ckms | Out-Null

    # Per-user Store packages that are not provisioned make sysprep fail.
    $provisioned = @(Get-AppxProvisionedPackage -Online | ForEach-Object { $_.PackageName })
    Get-AppxPackage -AllUsers | Where-Object {
        -not $_.IsFramework -and -not $_.NonRemovable -and
        $_.SignatureKind -eq 'Store' -and $_.PackageFullName -notin $provisioned
    } | ForEach-Object {
        Remove-AppxPackage -Package $_.PackageFullName -AllUsers -ErrorAction SilentlyContinue
    }

    Remove-Item -Force -Recurse -ErrorAction SilentlyContinue 'C:\Windows\System32\Sysprep\Panther'
    # Freed clusters become holes in the published qcow2 disk (discard=unmap).
    Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue | Out-Null
    $sysprep = Join-Path $env:SystemRoot 'System32\Sysprep\sysprep.exe'
    $process = Start-Process -FilePath $sysprep -Wait -PassThru -ArgumentList @(
        '/generalize', '/oobe', '/quit', '/mode:vm', '/quiet',
        "/unattend:$StateDir\generalize-unattend.xml"
    )
    $tag = 'C:\Windows\System32\Sysprep\Sysprep_succeeded.tag'
    if ($process.ExitCode -ne 0 -or -not (Test-Path $tag)) {
        $errors = Get-Content -ErrorAction SilentlyContinue 'C:\Windows\System32\Sysprep\Panther\setuperr.log' |
            Select-Object -Last 5
        throw "sysprep failed ($($process.ExitCode)): $($errors -join ' | ')"
    }
    Write-Serial 'OPENCURIA_GENERALIZE_OK'
} catch {
    Write-Serial "OPENCURIA_GENERALIZE_FAILED $($_.Exception.Message)"
}
Stop-Computer -Force
