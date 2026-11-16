# Read or write the console clipboard for the runner.
#
# Runs in root's interactive session (scheduled task "OpenCuria Clipboard"),
# because only that session owns the clipboard the desktop shows. Reads
# C:\ProgramData\opencuria\clipboard.request {"action": "read"|"write"|"copy",
# "text": <base64 UTF-8>, "shift": bool} and writes clipboard.result
# {"ok": bool, "text": <base64 UTF-8>, "error": str}.
$ErrorActionPreference = 'Stop'
$dir = 'C:\ProgramData\opencuria'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -Namespace OpenCuria -Name Clipboard -MemberDefinition @'
[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
'@
$result = [ordered]@{ ok = $true; text = ''; error = '' }

function Read-Text {
    $text = [System.Windows.Forms.Clipboard]::GetText()
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text))
}

try {
    $request = Get-Content -Raw "$dir\clipboard.request" | ConvertFrom-Json
    switch ($request.action) {
        'write' {
            $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([string] $request.text))
            if ($text) { [System.Windows.Forms.Clipboard]::SetText($text) }
            else { [System.Windows.Forms.Clipboard]::Clear() }
        }
        'read' { $result.text = Read-Text }
        'copy' {
            # Like XFixes on Linux guests: only a clipboard change counts.
            $before = [OpenCuria.Clipboard]::GetClipboardSequenceNumber()
            [System.Windows.Forms.SendKeys]::SendWait($(if ($request.shift) { '^+c' } else { '^c' }))
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
Remove-Item -Force -ErrorAction SilentlyContinue "$dir\clipboard.request"
$result | ConvertTo-Json -Compress | Set-Content -Encoding ascii "$dir\clipboard.result.tmp"
Move-Item -Force "$dir\clipboard.result.tmp" "$dir\clipboard.result"
