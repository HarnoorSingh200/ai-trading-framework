# Open the operator site when the framework is actually answering.
#
# start-module-1.cmd launches this in the background before it starts the
# runner. Opening the browser first shows an error screen while Module 1 is
# still fetching history - and a browser that was handed a refused stream does
# not come back on its own - so the window is opened when Module 1 answers.
# It opens anyway after the timeout, so the operator can see the Control tab
# report whatever is wrong.
param(
    [string]$Site = "http://127.0.0.1:5200",
    [string]$Ready = "http://127.0.0.1:5201/health",
    [int]$TimeoutSeconds = 300
)

$launcherLog = Join-Path $PSScriptRoot "logs\launcher.log"
function Write-LauncherLog([string]$Message) {
    try {
        Add-Content -Path $launcherLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    } catch { }
}

Write-LauncherLog "opener: waiting for $Ready"
if ($env:FRAMEWORK_NO_BROWSER -eq "1") {
    Write-LauncherLog "opener: browser suppressed (FRAMEWORK_NO_BROWSER=1)"
    exit 0
}
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $Ready -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -eq 200) { break }
    } catch {
        # Not up yet; the framework is still starting.
    }
    Start-Sleep -Seconds 2
}

# Firefox is preferred when it is installed (this machine's operator browser);
# anything else falls back to the default handler for the URL.
$candidates = @(
    "$env:ProgramFiles\Mozilla Firefox\firefox.exe",
    "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe",
    "$env:LOCALAPPDATA\Mozilla Firefox\firefox.exe"
)
$firefox = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
try {
    # A window of its own, not a tab somewhere: "it opens the site" has to be
    # true at a glance.
    if ($firefox) { Start-Process -FilePath $firefox -ArgumentList "-new-window", $Site }
    else { Start-Process $Site }
    Write-LauncherLog "opener: opened $Site"
}
catch {
    Write-LauncherLog "opener: could not open $Site ($($_.Exception.Message))"
    throw
}
