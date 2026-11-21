# Serve the console clipboard to the runner.
#
# Runs in root's interactive session (scheduled task "OpenCuria Clipboard", at
# logon and on demand), because only that session owns the clipboard the
# desktop shows. Starting PowerShell takes seconds, so one agent stays and
# answers every request: it reads C:\ProgramData\opencuria\clipboard.request
# {"action": "read"|"write"|"copy", "text": <base64 UTF-8>, "shift": bool} and
# writes clipboard.result {"ok": bool, "text": <base64 UTF-8>, "error": str}.
$ErrorActionPreference = 'Stop'
$dir = 'C:\ProgramData\opencuria'
$request = "$dir\clipboard.request"

# One agent per session; an on-demand start finds it running and leaves.
$mutex = New-Object System.Threading.Mutex($false, 'Local\OpenCuriaClipboard')
if (-not $mutex.WaitOne(0)) { exit 0 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace OpenCuria -Name Clipboard -MemberDefinition @'
[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
'@

function Read-Text {
    $text = [System.Windows.Forms.Clipboard]::GetText()
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
}

function Invoke-Request {
    $result = [ordered]@{ ok = $true; text = ''; error = '' }
    try {
        $order = Get-Content -Raw $request | ConvertFrom-Json
        switch ($order.action) {
            'write' {
                $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string] $order.text))
                if ($text) { [System.Windows.Forms.Clipboard]::SetText($text) }
                else { [System.Windows.Forms.Clipboard]::Clear() }
            }
            'read' { $result.text = Read-Text }
            'copy' {
                # Like XFixes on Linux guests: only a clipboard change counts.
                $before = [OpenCuria.Clipboard]::GetClipboardSequenceNumber()
                [System.Windows.Forms.SendKeys]::SendWait($(if ($order.shift) { '^+c' } else { '^c' }))
                $deadline = [DateTime]::UtcNow.AddSeconds(2.5)
                while ([OpenCuria.Clipboard]::GetClipboardSequenceNumber() -eq $before) {
                    if ([DateTime]::UtcNow -gt $deadline) { throw 'The selection was not copied' }
                    Start-Sleep -Milliseconds 50
                }
                $result.text = Read-Text
            }
            default { throw "Unknown clipboard action" }
        }
    } catch {
        $result.ok = $false
        $result.error = $_.Exception.Message
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $request
    $result | ConvertTo-Json -Compress | Set-Content -Encoding ascii "$dir\clipboard.result.tmp"
    Move-Item -Force "$dir\clipboard.result.tmp" "$dir\clipboard.result"
}

# The runner moves each request into place; the timeout only bounds a missed
# notification.
$watcher = New-Object System.IO.FileSystemWatcher $dir, 'clipboard.request'
while ($true) {
    if (Test-Path $request) {
        try { Invoke-Request } catch { Start-Sleep -Milliseconds 200 }
        continue
    }
    $watcher.WaitForChanged([IO.WatcherChangeTypes]::All, 1000) | Out-Null
}
