# Email Routine Dashboard - installer
#
# Sets the tool up on this machine and nowhere else: creates the local config from the
# shipped templates, initialises an EMPTY database, registers a login autostart, and
# launches the dashboard.
#
# IT INSTALLS NO ACCOUNTS AND NO CREDENTIALS. The plugin ships with zero mailboxes by
# design; adding one is a separate, deliberate step (see the onboarding skill). An
# installer that asked for an email password would be teaching exactly the habit this
# tool exists to protect people from.

param(
    [int]    $Port      = 9770,
    [switch] $NoAutostart,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
# This script sits BESIDE app/, not in a dist/ subfolder. An earlier version took the
# PARENT of its own directory, so every path pointed at a sibling of the plugin that does
# not exist - the config copy and the schema step both failed and nobody could install it.
# It shipped because the installer had only ever been syntax-checked, never run.
$AppRoot   = $PSScriptRoot
$App       = Join-Path $AppRoot 'app'
$Dashboard = Join-Path $App 'dashboard'
$ConfigDir = Join-Path $App 'config'

# Fail here, naming the layout, rather than 40 lines later on a confusing Join-Path error.
if (-not (Test-Path $Dashboard)) {
    Write-Host "`nERROR: expected the app tree beside this script." -ForegroundColor Red
    Write-Host "  looked for : $Dashboard" -ForegroundColor Red
    Write-Host "  script dir : $PSScriptRoot" -ForegroundColor Red
    Write-Host "  Run install.ps1 from the plugin folder that contains app\." -ForegroundColor Yellow
    exit 1
}
$StartupDir = [Environment]::GetFolderPath('Startup')
$VbsPath   = Join-Path $StartupDir 'EmailDashboard.vbs'

function Say($msg, $colour = 'Gray') { Write-Host "  $msg" -ForegroundColor $colour }

if ($Uninstall) {
    Write-Host "`nRemoving the autostart entry" -ForegroundColor Cyan
    if (Test-Path $VbsPath) { Remove-Item $VbsPath -Force; Say "removed $VbsPath" 'Green' }
    else { Say "no autostart entry found" }
    Say "Your config, database and mail data were NOT touched." 'Yellow'
    Say "Delete the plugin folder yourself if you want them gone." 'Yellow'
    return
}

Write-Host "`nEmail Routine Dashboard - install" -ForegroundColor Cyan

# --- 1. Python -------------------------------------------------------------------
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
    Write-Host "  Python 3 was not found on PATH. Install it and re-run." -ForegroundColor Red
    exit 1
}
$ver = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])")
Say "python $ver at $($py.Source)" 'Green'
# The tool is stdlib-only on purpose - nothing to pip install, nothing to keep updated,
# and no third-party code between a mailbox and its owner.
Say "no third-party packages required (stdlib only)" 'Green'

# --- 2. Local config from the templates -------------------------------------------
Write-Host "`nConfig" -ForegroundColor Cyan
foreach ($pair in @(
    @{ ex = 'protected.example.json'; local = 'protected.local.json'; dir = $ConfigDir },
    @{ ex = 'steam_refresh.example.json'; local = 'steam_refresh.local.json'; dir = $Dashboard },
    @{ ex = 'categorize.example.json'; local = 'categorize.local.json'; dir = $Dashboard },
    # Created even though an absent one is harmless, purely so it is DISCOVERABLE. The
    # label-extension mechanism is invisible until you need it, and by then the symptom is
    # labels quietly resolving to UNMAPPED with nothing pointing at the fix.
    @{ ex = 'concepts.example.json'; local = 'concepts.local.json'; dir = $Dashboard },
    @{ ex = 'dashboard.example.json'; local = 'dashboard.local.json'; dir = $ConfigDir }
)) {
    $src = Join-Path $pair.dir $pair.ex
    $dst = Join-Path $pair.dir $pair.local
    if (-not (Test-Path $src)) { continue }
    if (Test-Path $dst) { Say "$($pair.local) already exists - left alone" 'Yellow' }
    else { Copy-Item $src $dst; Say "created $($pair.local)" 'Green' }
}

$rulesEx = Join-Path $App 'rules-and-policies.example.md'
$rules   = Join-Path $App 'rules-and-policies.md'
if ((Test-Path $rulesEx) -and -not (Test-Path $rules)) {
    Copy-Item $rulesEx $rules
    Say "created rules-and-policies.md from the template" 'Green'
} elseif (Test-Path $rules) {
    Say "rules-and-policies.md already exists - left alone" 'Yellow'
}

$accounts = Join-Path $ConfigDir 'accounts.json'
if (-not (Test-Path $accounts)) {
    # Deliberately EMPTY. The onboarding skill fills this in, one mailbox at a time,
    # with the owner present.
    # WriteAllText with an explicit BOM-less encoder, NOT Set-Content -Encoding UTF8.
    # Windows PowerShell 5.1's "UTF8" means utf-8-WITH-BOM, and Python's json.load raises
    # on a leading BOM - so this one line made accounts.json unreadable on every fresh
    # install under 5.1, while working fine under PowerShell 7. Found by running the
    # installer for real; no amount of reading it would have shown this.
    [System.IO.File]::WriteAllText(
        $accounts, "{ `"accounts`": [] }`r`n",
        (New-Object System.Text.UTF8Encoding $false))
    Say "created accounts.json with NO mailboxes - add yours via the onboarding skill" 'Green'
} else {
    Say "accounts.json already exists - left alone" 'Yellow'
}

# --- 3. Empty database -------------------------------------------------------------
Write-Host "`nDatabase" -ForegroundColor Cyan
Push-Location $Dashboard
try {
    & python -c "import db; db.init_db(); print('ok')" | Out-Null
    Say "schema created (no mail data)" 'Green'
} finally { Pop-Location }

# --- 4. Autostart -------------------------------------------------------------------
Write-Host "`nAutostart" -ForegroundColor Cyan
if ($NoAutostart) {
    Say "skipped (-NoAutostart)" 'Yellow'
} else {
    $starter = Join-Path $Dashboard 'start-dashboard.ps1'
    # WScript.Shell with window style 0 so nothing flashes on login. The launcher itself
    # is polite: it no-ops if the port is already serving this dashboard, and refuses the
    # port rather than fighting for it if something else holds it.
    $vbs = @"
' Auto-starts the Email Routine Dashboard at Windows login (hidden, no window).
' Calls start-dashboard.ps1, which is a no-op if the dashboard is already running.
' Delete this file to disable autostart, or run install.ps1 -Uninstall.
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$starter"" -Port $Port", 0, False
"@
    $vbs | Set-Content $VbsPath -Encoding ASCII
    Say "registered $VbsPath" 'Green'
    Say "starts on login; the page will be at http://127.0.0.1:$Port" 'Green'
}

# --- 5. Start it now ----------------------------------------------------------------
Write-Host "`nStarting" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dashboard 'start-dashboard.ps1') -Port $Port | Out-Null
Start-Sleep -Seconds 2
try {
    $null = Invoke-WebRequest "http://127.0.0.1:$Port/api/whoami" -UseBasicParsing -TimeoutSec 5
    Say "dashboard responding on http://127.0.0.1:$Port" 'Green'
} catch {
    Say "could not reach the dashboard yet - try start-dashboard.ps1 by hand" 'Yellow'
}

Write-Host "`nInstalled." -ForegroundColor Cyan
Write-Host "  It has NO mailboxes and NO data yet - that is the intended starting state."
Write-Host "  Next: run the onboarding skill to add your first mailbox, then your first sweep."
Write-Host ""
