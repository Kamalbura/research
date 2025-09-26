param(
    [Parameter(Mandatory=$true)]
    [string[]] $Suites,
    [int] $FastSecs = 25,
    [int] $SlowSecs = 90
)

function Get-SafeName {
    param([string] $Name)
    return ($Name -replace "[^A-Za-z0-9_-]", "_")
}

if ($Suites.Count -eq 0) {
    Write-Error "Provide at least one suite identifier."
    exit 1
}

$logsRoot = Join-Path (Get-Location) "logs"
if (-not (Test-Path $logsRoot)) {
    New-Item -ItemType Directory -Path $logsRoot | Out-Null
}

$summaryCsv = Join-Path $logsRoot "matrix_gcs_summary.csv"

foreach ($suite in $Suites) {
    $safe = Get-SafeName -Name $suite
    $duration = if ($suite -match "sphincs") { $SlowSecs } else { $FastSecs }

    $proxyJson = Join-Path $logsRoot ("gcs_{0}.json" -f $safe)
    $trafficDir = Join-Path $logsRoot (Join-Path "traffic" $suite)
    if (-not (Test-Path $trafficDir)) {
        New-Item -ItemType Directory -Path $trafficDir -Force | Out-Null
    }

    $trafficOut = Join-Path $trafficDir "gcs_events.jsonl"
    $trafficSummary = Join-Path $trafficDir "gcs_summary.json"

    Write-Host "[GCS] Starting proxy for suite $suite ($duration s)"
    $proxyArgs = @("-m", "core.run_proxy", "gcs", "--suite", $suite, "--stop-seconds", $duration.ToString(), "--json-out", $proxyJson, "--quiet")
    $proxyProc = Start-Process -FilePath "python" -ArgumentList $proxyArgs -PassThru -WindowStyle Hidden

    Start-Sleep -Seconds 3

    Write-Host "[GCS] Running traffic generator"
    $trafficArgs = @("tools/traffic_gcs.py", "--count", "200", "--rate", "50", "--duration", [string]($duration - 5), "--out", $trafficOut, "--summary", $trafficSummary)
    $trafficExit = & python @trafficArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "traffic_gcs.py exited with code $LASTEXITCODE"
    }

    $proxyProc.WaitForExit()
    if ($proxyProc.ExitCode -ne 0) {
        Write-Warning "Proxy exited with code $($proxyProc.ExitCode)"
    }

    if (-not (Test-Path $proxyJson)) {
        Write-Warning "Proxy JSON $proxyJson not found"
        continue
    }
    if (-not (Test-Path $trafficSummary)) {
        Write-Warning "Traffic summary $trafficSummary not found"
        continue
    }

    $proxyData = Get-Content $proxyJson -Raw | ConvertFrom-Json
    $trafficData = Get-Content $trafficSummary -Raw | ConvertFrom-Json

    $row = [PSCustomObject]@{
        suite = $suite
        host = "gcs"
        ptx_out = $proxyData.counters.ptx_out
        ptx_in = $proxyData.counters.ptx_in
        enc_out = $proxyData.counters.enc_out
        enc_in = $proxyData.counters.enc_in
        drops = $proxyData.counters.drops
        drop_auth = $proxyData.counters.drop_auth
        drop_header = $proxyData.counters.drop_header
        drop_replay = $proxyData.counters.drop_replay
        traffic_sent_total = $trafficData.sent_total
        traffic_recv_total = $trafficData.recv_total
    }

    $row | Export-Csv -Path $summaryCsv -Append -NoTypeInformation -Encoding UTF8
}
