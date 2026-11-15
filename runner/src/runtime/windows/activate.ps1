# Install and activate the Windows license of this workspace.
#
# Reads {"mode": "key"|"kms"|"none", "product_key"?, "kms_host"?} from stdin, so
# a product key never appears in a command line. Prints one JSON line
# {"status": "activated"|"failed"|"not_activated", "code": "<diagnostic>"}.
# The key is removed from the registry after installation (slmgr /cpky).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$WindowsApplication = '55c92734-d682-4d71-983e-d6ec3f16059f'
# Public KMS client setup keys (GVLK) published by Microsoft per edition.
$Gvlk = @{
    'Professional' = 'W269N-WFGWX-YVC9B-4J6C9-T83GX'
    'ProfessionalN' = 'MH37W-N47XK-V7XM9-C7227-GCQG9'
    'Enterprise' = 'NPPR9-FWDCX-D2C8J-H872K-2YT43'
    'EnterpriseN' = 'DPH2V-TTNVB-4X9Q3-TJR4H-KHJW4'
    'Education' = 'NW6C2-QMPVW-D7KKK-3GKT6-VCFB2'
    'EducationN' = '2WH4N-8QGBV-H22JP-CT43Q-MDWWJ'
    'ProfessionalWorkstation' = 'NRG8B-VKK3Q-CXVCJ-9G2XF-6Q84J'
    'ProfessionalWorkstationN' = '9FNHH-K3HBT-3W4TD-6383H-6XYWF'
    'ProfessionalEducation' = '6TP4R-GNPTD-KYYHQ-7B7DP-J447Y'
    'ProfessionalEducationN' = 'YVWGF-BXNMC-HTQYQ-CPQ99-66QFC'
}

function Write-Result([string] $Status, [string] $Code = '') {
    [Console]::Out.WriteLine((@{ status = $Status; code = $Code } | ConvertTo-Json -Compress))
    exit 0
}

function Get-ErrorCode($ErrorRecord) {
    $exception = $ErrorRecord.Exception
    while ($exception.InnerException) { $exception = $exception.InnerException }
    $hresult = $exception.HResult
    if ($exception.PSObject.Properties['ErrorData'] -and $exception.ErrorData) {
        $native = $exception.ErrorData.CimInstanceProperties['error_Code']
        if ($native) { $hresult = [int64] $native.Value }
    }
    return ('0x{0:X8}' -f ($hresult -band 0xFFFFFFFF))
}

function Get-WindowsProduct {
    Get-CimInstance SoftwareLicensingProduct -Filter (
        "ApplicationID='$WindowsApplication' AND PartialProductKey IS NOT NULL"
    ) | Select-Object -First 1
}

try {
    $config = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $service = Get-CimInstance SoftwareLicensingService
    switch ($config.mode) {
        'none' {
            $product = Get-WindowsProduct
            if ($product -and $product.LicenseStatus -eq 1) { Write-Result 'activated' }
            Write-Result 'not_activated'
        }
        'key' { $key = [string] $config.product_key }
        'kms' {
            $edition = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').EditionID
            $key = $Gvlk[$edition]
            if (-not $key) { Write-Result 'failed' "edition_$edition" }
        }
        default { Write-Result 'failed' 'invalid_mode' }
    }
    if ($key -notmatch '^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$') { Write-Result 'failed' 'invalid_key' }

    $product = Get-WindowsProduct
    if (-not $product -or $product.PartialProductKey -ne $key.Substring(24)) {
        try {
            Invoke-CimMethod -InputObject $service -MethodName InstallProductKey `
                -Arguments @{ ProductKey = $key } | Out-Null
        } catch {
            Write-Result 'failed' (Get-ErrorCode $_)
        }
    }

    if ($config.mode -eq 'kms' -and $config.kms_host) {
        $hostName, $port = ([string] $config.kms_host).Split(':', 2)
        Invoke-CimMethod -InputObject $service -MethodName SetKeyManagementServiceMachine `
            -Arguments @{ MachineName = $hostName } | Out-Null
        if ($port) {
            Invoke-CimMethod -InputObject $service -MethodName SetKeyManagementServicePort `
                -Arguments @{ PortNumber = [uint32] $port } | Out-Null
        } else {
            Invoke-CimMethod -InputObject $service -MethodName ClearKeyManagementServicePort | Out-Null
        }
    } else {
        # Without a host, KMS clients discover the service through DNS.
        Invoke-CimMethod -InputObject $service -MethodName ClearKeyManagementServiceMachine | Out-Null
        Invoke-CimMethod -InputObject $service -MethodName ClearKeyManagementServicePort | Out-Null
    }

    $code = ''
    try {
        Invoke-CimMethod -InputObject (Get-WindowsProduct) -MethodName Activate | Out-Null
    } catch {
        $code = Get-ErrorCode $_
    }
    Invoke-CimMethod -InputObject $service -MethodName RefreshLicenseStatus | Out-Null
    try { Invoke-CimMethod -InputObject $service -MethodName ClearProductKeyFromRegistry | Out-Null } catch { }
    $product = Get-WindowsProduct
    if ($product -and $product.LicenseStatus -eq 1) { Write-Result 'activated' }
    if (-not $code) { $code = "license_status_$($product.LicenseStatus)" }
    Write-Result 'failed' $code
} catch {
    Write-Result 'failed' (Get-ErrorCode $_)
}
