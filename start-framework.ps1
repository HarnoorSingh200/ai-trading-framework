# The operator's one click, in PowerShell rather than batch.
#
# start-module-1.cmd is only the door to this script. The logic used to live
# in the batch file, where labels and nested blocks are fragile - a .cmd saved
# with Unix line endings loses its labels entirely, and the window then closes
# before a single line is printed. PowerShell does not care about line endings
# and can say what went wrong, which is what a launcher has to do first time.
#
#   - starts the site quietly in the background, when it is installed;
#   - opens the browser only once Module 1 is actually answering;
#   - runs the central runner in this window (closing it stops everything);
#   - if the framework is already running, only shows the site and says so.
#
# FRAMEWORK_NO_BROWSER=1 keeps the browser closed (a machine with no screen).

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
try { $Host.UI.RawUI.WindowTitle = "Trading Framework - runner" } catch { }

$site = "http://127.0.0.1:5200"
$module1Health = "http://127.0.0.1:5201/health"
$siteScript = Join-Path $root "start-site.cmd"
$openerScript = Join-Path $root "open-site-when-ready.ps1"
$siteLog = Join-Path $root "logs\site.process.log"
$uiReady = Test-Path (Join-Path $root "ui\node_modules")

# One line per step, under logs\launcher.log: a launcher that fails silently
# is worse than one that fails loudly, and this file is where "it did not open
# the browser" can be answered without guessing.
$launcherLog = Join-Path $root "logs\launcher.log"
function Write-LauncherLog([string]$Message) {
    try {
        $logDir = Split-Path -Parent $launcherLog
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
        Add-Content -Path $launcherLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    } catch { }
}

function Test-Listening([int]$Port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-Listening([int]$Port, [int]$TimeoutSeconds = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Listening $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Open-Site {
    $candidates = @(
        "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
        "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe",
        "$env:LOCALAPPDATA\Mozilla Firefox\firefox.exe"
    )
    $firefox = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    # A window of its own, not a tab somewhere in a window the operator may
    # not be looking at: "it opens the site" has to be true at a glance.
    if ($firefox) { Start-Process -FilePath $firefox -ArgumentList "-new-window", $site }
    else { Start-Process $site }
}

function Start-SiteQuietly {
    # No new window, output to logs\site.process.log: it shares this console,
    # so closing the runner window stops the site with it.
    Start-Process -NoNewWindow -FilePath "cmd.exe" -ArgumentList "/c `"$siteScript`""
}

function Start-BrowserOpener {
    # Start-Process joins the argument list with spaces and quotes nothing, and
    # this folder's path has a space in it ("Stock market"): passing the pieces
    # separately made PowerShell read "-File C:\...\Projects\Stock" and die
    # before opening anything. One string, every path quoted by hand.
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$openerScript`" " +
        "-Site `"$site`" -Ready `"$module1Health`""
    Write-LauncherLog "launcher: starting the browser opener"
    Start-Process -WindowStyle Hidden -FilePath "powershell" -ArgumentList $arguments
}

Write-Host ""
Write-Host "  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
Write-Host "    Trading Framework v3"
Write-Host "  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~"
Write-Host ""

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "  [x] The framework has no Python environment yet."
    Write-Host "      Run this once from this folder, then start me again:"
    Write-Host ""
    Write-Host "          uv sync"
    Write-Host ""
    Read-Host "  Press Enter to close this window"
    exit 1
}

# Secrets live outside the repository. If the environment does not already
# name a file, use the operator's usual location.
if (-not $env:FRAMEWORK_SECRETS_FILE) {
    $env:FRAMEWORK_SECRETS_FILE = "$env:USERPROFILE\.trading-framework\secrets.env"
}

# -- the framework may already be running ----------------------------------- #
if (Test-Listening 5201) {
    Write-Host "  [i] The framework is already running."
    Write-LauncherLog "launcher: framework already running"
    if ($uiReady -and -not (Test-Listening 5200)) {
        Write-Host "  [ ] the site is not up; starting it in the background."
        Start-SiteQuietly
        [void](Wait-Listening 5200 120)
    }
    if ($uiReady -and $env:FRAMEWORK_NO_BROWSER -ne "1") { Open-Site }
    Write-Host ""
    Write-Host "  The runner is alive in another window; close THAT window to stop it."
    Start-Sleep -Seconds 10
    exit 0
}

# -- the operator site, when it is installed --------------------------------- #
if ($uiReady -and -not (Test-Listening 5200)) {
    Write-Host "  [ ] starting the site on $site"
    Start-SiteQuietly
    Write-LauncherLog "launcher: started the site"
    Write-Host "  [.] waiting for the site to answer ..."
    if (-not (Wait-Listening 5200 120)) {
        Write-Host "  [!] The site did not answer within two minutes; the framework runs without it."
    }
}
elseif (-not $uiReady) {
    Write-Host "  [!] The site has no packages installed; running headless."
    Write-Host "      To add it later:  cd ui  ^&^&  npm install"
}

# -- the browser opens when Module 1 actually answers ------------------------ #
if ($uiReady -and $env:FRAMEWORK_NO_BROWSER -ne "1") {
    Write-Host "  [.] the browser will open as soon as Module 1 answers."
    Start-BrowserOpener
}

# -- the framework ----------------------------------------------------------- #
Write-Host ""
Write-Host "  [ ] starting the runner: Module 1 first, then Module 4, then"
Write-Host "      Modules 3 and 2 for the trading day (the Control tab shows them all)."
Write-Host "      Decisions 09:30-14:30 IST. Flat by 14:50 IST."
Write-Host "      Orders and failures go to the operator's Telegram."
Write-Host ""
Write-LauncherLog "launcher: starting the runner"

$code = 0
try {
    & $python -m runner
    $code = $LASTEXITCODE
}
finally {
    Write-Host ""
    Write-Host "  The runner has stopped (exit $code); the site stops with it."
    Write-Host "  The site's own output, if you ever need it, is logs\site.process.log."
    Write-Host "  (If Windows asks 'Terminate batch job (Y/N)?', answer N.)"
    Write-LauncherLog "launcher: runner stopped (exit $code)"
}
Read-Host "  Press Enter to close this window"
exit $code
