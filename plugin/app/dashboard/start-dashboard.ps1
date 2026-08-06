# Starts the email dashboard on http://127.0.0.1:9770 (detached, no console window).
# Stays up after this window closes. Used for manual starts and auto-start at Windows login.
#
# Port 9770 is RESERVED for the email dashboard, deliberately kept clear of the spatial/power
# worker band (8765) and the GPU webgui (8081) so isolated worker instances never collide with it.
#
# This launcher is "polite": if 9770 is already serving the email dashboard it no-ops; if 9770 is
# occupied by something else (e.g. a stray worker server), it refuses to start rather than fight
# for the port, and it NEVER roams to a different port (which could invade worker space).
$ErrorActionPreference = 'SilentlyContinue'
$port = 9770

if (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet) {
    $who = $null
    try { $who = (Invoke-RestMethod "http://127.0.0.1:$port/api/whoami" -TimeoutSec 3).app } catch {}
    if ($who -eq 'email-dashboard') {
        Write-Host "Dashboard already running at http://127.0.0.1:$port"
        exit 0
    }
    Write-Warning "Port $port is occupied by another app (not the email dashboard). Not starting, to avoid interfering. Free the port or change the reserved port, then retry."
    exit 1
}

$server = Join-Path $PSScriptRoot 'server.py'
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if ($pyw) {
    Start-Process $pyw -ArgumentList $server, '--port', "$port" -WindowStyle Hidden
} else {
    Start-Process python -ArgumentList $server, '--port', "$port" -WindowStyle Hidden
}
Start-Sleep -Seconds 2
Write-Host "Dashboard started at http://127.0.0.1:$port"
