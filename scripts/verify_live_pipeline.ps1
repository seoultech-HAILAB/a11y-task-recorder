param(
    [string]$KitPath = ""
)

$ErrorActionPreference = "Stop"

$base = "http://127.0.0.1:8765/api"
if (-not $KitPath) {
    $KitPath = Get-ChildItem "C:\A11yTaskRecorder-*" -Directory |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $KitPath -or -not (Test-Path $KitPath)) {
    throw "Recorder kit was not found. Pass -KitPath with the installed kit directory."
}
$kit = $KitPath
$health = Invoke-RestMethod "$base/health"
if (-not $health.nvda_connected) {
    throw "NVDA add-on is not connected. Start the recorder kit and retry."
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$outputDir = Join-Path $env:TEMP "a11y-recorder-verification-$stamp"
New-Item -ItemType Directory -Path $outputDir | Out-Null

$sessionBody = @{
    title = "End-to-end collection verification $stamp"
    participant = "SYSTEM-CHECK"
    target_url = "https://example.com/"
    scenario = "Navigate Example Domain with Tab and inspect the link"
    expected_announcement = "Example Domain, More information"
} | ConvertTo-Json
$session = (Invoke-RestMethod "$base/sessions" -Method Post -Body $sessionBody -ContentType "application/json; charset=utf-8").session
$sessionId = $session.id

$step = (Invoke-RestMethod "$base/sessions/$sessionId/steps" -Method Post -Body (@{
    title = "Link navigation"
    expected_announcement = "More information"
} | ConvertTo-Json) -ContentType "application/json; charset=utf-8").step

Invoke-RestMethod "$base/sessions/$sessionId/start" -Method Post -Body "{}" -ContentType "application/json" | Out-Null
Invoke-RestMethod "$base/sessions/$sessionId/steps/$($step.id)/start" -Method Post -Body "{}" -ContentType "application/json" | Out-Null

$chrome = Join-Path $kit "app\chrome\chrome.exe"
$extension = Join-Path $kit "app\browser-extension"
$extensionVersion = (Get-Content (Join-Path $extension "manifest.json") -Raw | ConvertFrom-Json).version
$profile = Join-Path $kit "app\chrome-profile-$extensionVersion"
Start-Process $chrome -ArgumentList @(
    "--load-extension=$extension",
    "--user-data-dir=$profile",
    "--no-first-run",
    "--no-default-browser-check",
    "https://example.com/"
)
Start-Sleep -Seconds 6

$shell = New-Object -ComObject WScript.Shell
$activated = $shell.AppActivate("Example Domain")
if (-not $activated) {
    throw "Could not activate the Example Domain browser window."
}
foreach ($key in @("{TAB}", "{TAB}", "+{TAB}")) {
    $shell.SendKeys($key)
    Start-Sleep -Milliseconds 700
}
Start-Sleep -Seconds 3

$captured = (Invoke-RestMethod "$base/sessions/$sessionId/events").events
$nvdaEventCount = @($captured | Where-Object { $_.source -eq "nvda" }).Count
if ($nvdaEventCount -eq 0) {
    Invoke-RestMethod "$base/sessions/$sessionId/steps/$($step.id)/finish" -Method Post -Body (@{
        outcome = "blocked"
        blocked_reason = "Automated verification did not receive NVDA events"
    } | ConvertTo-Json) -ContentType "application/json" | Out-Null
    Invoke-RestMethod "$base/sessions/$sessionId/stop" -Method Post -Body (@{
        status = "abandoned"
    } | ConvertTo-Json) -ContentType "application/json" | Out-Null
    throw "No NVDA events were captured. Restart the dedicated Chrome after NVDA is ready."
}

Invoke-RestMethod "$base/sessions/$sessionId/hints" -Method Post -Body (@{
    text = "Verification hint"
} | ConvertTo-Json) -ContentType "application/json; charset=utf-8" | Out-Null
$markerBody = @{
    session_id = $sessionId
    source = "dashboard"
    type = "marker"
    timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
    payload = @{ label = "Verification marker"; intensity = 3 }
} | ConvertTo-Json -Depth 4
Invoke-RestMethod "$base/events" -Method Post -Body $markerBody -ContentType "application/json; charset=utf-8" | Out-Null

Invoke-RestMethod "$base/sessions/$sessionId/steps/$($step.id)/finish" -Method Post -Body "{}" -ContentType "application/json" | Out-Null
Invoke-RestMethod "$base/sessions/$sessionId/stop" -Method Post -Body (@{ status = "completed" } | ConvertTo-Json) -ContentType "application/json" | Out-Null

curl.exe -s -o (Join-Path $outputDir "session.json") "$base/sessions/$sessionId/export.json"
curl.exe -s -o (Join-Path $outputDir "events.csv") "$base/sessions/$sessionId/export.csv"
curl.exe -s -o (Join-Path $outputDir "interactions.csv") "$base/sessions/$sessionId/export-interactions.csv"

@{
    session_id = $sessionId
    output_dir = $outputDir
    chrome_activated = $activated
    nvda_event_count = $nvdaEventCount
} | ConvertTo-Json
