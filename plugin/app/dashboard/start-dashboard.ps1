# Starts the email dashboard on http://127.0.0.1:9770 (detached, no console window).
# Stays up after this window closes. Used for manual starts and auto-start at Windows login.
#
# Port 9770 is RESERVED for the email dashboard, deliberately kept clear of the spatial/power
# worker band (8765) and the GPU webgui (8081) so isolated worker instances never collide with it.
#
# This launcher is "polite": if 9770 is already serving the email dashboard it no-ops; if 9770 is
# occupied by something else (e.g. a stray worker server), it refuses to start rather than fight
# for the port, and it NEVER roams to a different port (which could invade worker space).
#
# -Port is a real parameter. It used to be a hardcoded 9770 while install.ps1 accepted a
# -Port switch of its own and passed it nowhere, so installing on another port silently
# installed on 9770 - a flag that reports success and does nothing.
param([int] $Port = 9770)

$ErrorActionPreference = 'SilentlyContinue'
$port = $Port

# A raw TcpClient connect, not Test-NetConnection: the cmdlet runs traceroute-ish probes and
# takes seconds for a loopback question answerable in milliseconds, and this runs at every
# login. Same semantics - connected means something is listening.
function Test-Port([int]$p) {
    $c = New-Object System.Net.Sockets.TcpClient
    try   { $null = $c.ConnectAsync('127.0.0.1', $p).Wait(400); return $c.Connected }
    catch { return $false }
    finally { $c.Dispose() }
}

if (Test-Port $port) {
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
