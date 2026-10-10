param(
    [string]$KitPath = "",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8765/api"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$sessionId = $null
$result = [ordered]@{
    status = "FAIL"
    evidence_kind = "Actual Windows NVDA engineering diagnostic; no simulated NVDA events"
    actual_nvda_validation = "NOT_RUN"
    started_at = (Get-Date).ToUniversalTime().ToString("o")
    completed_at = $null
    session_id = $null
    output_dir = $null
    error = $null
    cleanup_error = $null
    nvda_input_count = 0
    nvda_timed_speech_count = 0
    browser_event_count = 0
    export_event_count = 0
}

if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot "diagnostic-results" }
try {
    $outputDir = Join-Path $OutputDirectory $stamp
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
} catch {
    $outputDir = Join-Path ([IO.Path]::GetTempPath()) "a11y-recorder-verification-$stamp"
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}
$result.output_dir = $outputDir

try {
    if (-not $KitPath) { $KitPath = $PSScriptRoot }
    $kit = (Resolve-Path -LiteralPath $KitPath).Path.TrimEnd('\')
    $chrome = Join-Path $kit "app\chrome\chrome.exe"
    $extension = Join-Path $kit "app\browser-extension"
    if (-not (Test-Path -LiteralPath $chrome)) { throw "Bundled Chrome was not found in $kit" }

    # Never restart or send keys while a participant session is active.
    $active = (Invoke-RestMethod "$base/active-session" -TimeoutSec 10).session
    if ($active) { throw "An active session already exists. Finish it before diagnostics; no session was changed." }
    $health = Invoke-RestMethod "$base/health" -TimeoutSec 10
    if (-not $health.nvda_connected) { throw "NVDA add-on is disconnected. Start the recorder kit before diagnostics." }

    $sessionBody = @{
        title = "End-to-end collection verification $stamp"
        participant = "SYSTEM-CHECK"
        target_url = "https://example.com/"
        scenario = "Navigate Example Domain with Tab and inspect the link"
        expected_announcement = "Example Domain, More information"
    } | ConvertTo-Json
    $session = (Invoke-RestMethod "$base/sessions" -Method Post -Body $sessionBody -ContentType "application/json; charset=utf-8" -TimeoutSec 10).session
    $sessionId = $session.id
    $result.session_id = $sessionId
    $step = (Invoke-RestMethod "$base/sessions/$sessionId/steps" -Method Post -Body (@{
        title = "Link navigation"
        expected_announcement = "More information"
    } | ConvertTo-Json) -ContentType "application/json; charset=utf-8" -TimeoutSec 10).step

    # A concurrent participant start fails this request before Chrome or keyboard use.
    Invoke-RestMethod "$base/sessions/$sessionId/start" -Method Post -Body "{}" -ContentType "application/json" -TimeoutSec 10 | Out-Null
    $result.actual_nvda_validation = "RUNNING"
    Invoke-RestMethod "$base/sessions/$sessionId/steps/$($step.id)/start" -Method Post -Body "{}" -ContentType "application/json" -TimeoutSec 10 | Out-Null
    $extensionVersion = (Get-Content -LiteralPath (Join-Path $extension "manifest.json") -Raw | ConvertFrom-Json).version
    $profile = Join-Path $kit "app\chrome-profile-$extensionVersion"
    # Start-Process flattens ArgumentList; quote entire native arguments explicitly.
    $chromeArguments = @(
        ('"--load-extension={0}"' -f $extension),
        ('"--user-data-dir={0}"' -f $profile),
        "--no-first-run",
        "--no-default-browser-check",
        "https://example.com/"
    ) -join ' '
    Start-Process -FilePath $chrome -ArgumentList $chromeArguments
    Start-Sleep -Seconds 6
    $active = (Invoke-RestMethod "$base/active-session" -TimeoutSec 10).session
    if (-not $active -or $active.id -ne $sessionId) { throw "Diagnostic session ended before browser activation." }
    $shell = New-Object -ComObject WScript.Shell
    if (-not $shell.AppActivate("Example Domain")) { throw "Could not activate the Example Domain browser window." }
    foreach ($key in @("{TAB}", "{TAB}", "+{TAB}")) {
        $active = (Invoke-RestMethod "$base/active-session" -TimeoutSec 10).session
        if (-not $active -or $active.id -ne $sessionId) { throw "Diagnostic session is no longer active; no further keys will be sent." }
        $shell.SendKeys($key)
        Start-Sleep -Milliseconds 700
    }
    Start-Sleep -Seconds 3
    Invoke-RestMethod "$base/sessions/$sessionId/steps/$($step.id)/finish" -Method Post -Body "{}" -ContentType "application/json" -TimeoutSec 10 | Out-Null
    Invoke-RestMethod "$base/sessions/$sessionId/stop" -Method Post -Body '{"status":"completed"}' -ContentType "application/json" -TimeoutSec 15 | Out-Null

    # Read after the collector drain; never inject NVDA events.
    Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $outputDir "session.json") -Uri "$base/sessions/$sessionId/export.json" -TimeoutSec 15
    Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $outputDir "events.csv") -Uri "$base/sessions/$sessionId/export.csv" -TimeoutSec 15
    Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $outputDir "interactions.csv") -Uri "$base/sessions/$sessionId/export-interactions.csv" -TimeoutSec 15
    $exported = Get-Content -LiteralPath (Join-Path $outputDir "session.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    $captured = @($exported.events)
    $result.export_event_count = $captured.Count
    $result.nvda_input_count = @($captured | Where-Object { $_.source -eq "nvda" -and $_.type -eq "input" }).Count
    $result.nvda_timed_speech_count = @($captured | Where-Object { $_.source -eq "nvda" -and $_.type -eq "speech_episode" -and $_.speech_end_ts -and $_.payload.raw_text -and -not $_.payload.redacted }).Count
    $result.browser_event_count = @($captured | Where-Object { $_.source -eq "browser" }).Count
    $result.actual_nvda_validation = "FAIL"
    if ($result.nvda_input_count -eq 0 -or $result.nvda_timed_speech_count -eq 0 -or $result.browser_event_count -eq 0) {
        throw "Incomplete actual collection: input=$($result.nvda_input_count), timed speech=$($result.nvda_timed_speech_count), browser=$($result.browser_event_count)."
    }
    if ($exported.status -ne "completed") { throw "Diagnostic session did not complete." }
    $csvEvents = @(Import-Csv -LiteralPath (Join-Path $outputDir "events.csv") -Encoding UTF8)
    if ($csvEvents.Count -ne $captured.Count) { throw "JSON and event CSV counts differ." }
    $csvInteractions = @(Import-Csv -LiteralPath (Join-Path $outputDir "interactions.csv") -Encoding UTF8)
    if ($csvInteractions.Count -ne @($exported.interactions).Count) { throw "JSON and interaction CSV counts differ." }
    $result.actual_nvda_validation = "PASS"
    $result.status = "PASS"
} catch {
    $result.error = $_.Exception.Message
    if ($result.actual_nvda_validation -eq "RUNNING") { $result.actual_nvda_validation = "FAIL" }
} finally {
    if ($sessionId) {
        try {
            $current = (Invoke-RestMethod "$base/sessions/$sessionId" -TimeoutSec 10).session
            if ($current.status -eq "active") {
                Invoke-RestMethod "$base/sessions/$sessionId/stop" -Method Post -Body '{"status":"abandoned"}' -ContentType "application/json" -TimeoutSec 15 | Out-Null
            }
            if (-not (Test-Path -LiteralPath (Join-Path $outputDir "session.json"))) {
                Invoke-WebRequest -UseBasicParsing -OutFile (Join-Path $outputDir "session.json") -Uri "$base/sessions/$sessionId/export.json" -TimeoutSec 15
            }
        } catch {
            $result.cleanup_error = $_.Exception.Message
            $result.status = "FAIL"
        }
    }
    $result.completed_at = (Get-Date).ToUniversalTime().ToString("o")
    $json = $result | ConvertTo-Json -Depth 6
    $json | Set-Content -LiteralPath (Join-Path $outputDir "diagnostic-result.json") -Encoding UTF8
    Write-Output $json
    Write-Host "Diagnostic result: $(Join-Path $outputDir 'diagnostic-result.json')"
}
if ($result.status -ne "PASS") { exit 1 }
