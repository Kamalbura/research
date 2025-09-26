param(
    [Alias("FastSecs")]
    [int] $DurationSec = 25,
    [Alias("SlowSecs")]
    [int] $SlowDurationSec = 90,
    [Alias("Count")]
    [int] $Pkts = 200,
    [int] $Rate = 50,
    [string] $OutDir,
    [string[]] $Suites
)

function Get-SafeName {
    param([string] $Name)
    return ($Name -replace "[^A-Za-z0-9_-]", "_")
}

$python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path $scriptDir -Parent

Push-Location $repoRoot
try {
    if (-not $OutDir -or $OutDir.Trim() -eq "") {
        $OutDir = Join-Path $repoRoot "logs"
    }

    if ([System.IO.Path]::IsPathRooted($OutDir)) {
        $OutDir = [System.IO.Path]::GetFullPath($OutDir)
    } else {
        $OutDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutDir))
    }

    if (-not $Suites -or $Suites.Count -eq 0) {
        $suiteOutput = & $python -c "from core import test_suites_config as t; print('\\n'.join(t.ALL_SUITES))"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to enumerate suites via python."
            exit 1
        }
        $Suites = $suiteOutput -split "`n" | Where-Object { $_ -ne "" }
    }

    $expandedSuites = @()
    foreach ($entry in $Suites) {
        if ($null -ne $entry) {
            $entry.Split(",") | ForEach-Object {
                $trimmed = $_.Trim()
                if ($trimmed.Length -gt 0) {
                    $expandedSuites += $trimmed
                }
            }
        }
    }
    $Suites = $expandedSuites

    if ($Suites.Count -eq 0) {
        Write-Error "No suites specified or discovered."
        exit 1
    }

    $logsRoot = $OutDir
    if (-not (Test-Path $logsRoot)) {
        New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null
    }

    $summaryCsv = Join-Path $logsRoot "matrix_gcs_summary.csv"

    foreach ($suite in $Suites) {
        $safe = Get-SafeName -Name $suite
        $duration = if ($suite -match "sphincs") { $SlowDurationSec } else { $DurationSec }

        $proxyJson = Join-Path $logsRoot ("gcs_{0}.json" -f $safe)
        $trafficDir = Join-Path $logsRoot (Join-Path "traffic" $safe)
        if (-not (Test-Path $trafficDir)) {
            New-Item -ItemType Directory -Path $trafficDir -Force | Out-Null
        }

        $trafficOut = Join-Path $trafficDir "gcs_events.jsonl"
        $trafficSummary = Join-Path $trafficDir "gcs_summary.json"

        Write-Host "[GCS] Starting proxy for suite $suite ($duration s)"
        $proxyArgs = @("-m", "core.run_proxy", "gcs", "--suite", $suite, "--stop-seconds", $duration.ToString(), "--json-out", $proxyJson, "--quiet")
        $proxyProc = Start-Process -FilePath $python -ArgumentList $proxyArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot

        Start-Sleep -Seconds 3

        Write-Host "[GCS] Running traffic generator"
        $runDuration = $duration - 5
        if ($runDuration -le 0) { $runDuration = $duration }
    $trafficArgs = @("-m", "tools.traffic_gcs", "--count", $Pkts.ToString(), "--rate", $Rate.ToString(), "--duration", $runDuration.ToString(), "--out", $trafficOut, "--summary", $trafficSummary)
        $trafficExit = & $python @trafficArgs
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
}
finally {
    Pop-Location
}
