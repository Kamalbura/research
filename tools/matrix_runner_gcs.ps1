param(
    [Alias("FastSecs")]
    [int] $DurationSec = 25,
    [Alias("SlowSecs")]
    [int] $SlowDurationSec = 90,
    [Alias("Count")]
    [int] $Pkts = 200,
    [int] $Rate = 50,
    [string] $OutDir,
    [string] $SecretsDir,
    [int] $HandshakeTimeoutSec = 30,
    [string[]] $Suites
)

function Get-SafeName {
    param([string] $Name)
    return ($Name -replace "[^A-Za-z0-9_-]", "_")
}

$python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }
$autoDiscoveredSuites = $false
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path $scriptDir -Parent

if (-not $SecretsDir -or $SecretsDir.Trim() -eq "") {
    $SecretsDir = Join-Path $repoRoot "secrets"
}

if ([System.IO.Path]::IsPathRooted($SecretsDir)) {
    $SecretsDir = [System.IO.Path]::GetFullPath($SecretsDir)
} else {
    $SecretsDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $SecretsDir))
}

if (-not (Test-Path $SecretsDir)) {
    New-Item -ItemType Directory -Path $SecretsDir -Force | Out-Null
}

$matrixSecretsRoot = Join-Path $SecretsDir "matrix"

function Ensure-SuiteIdentity {
    param(
        [string] $SuiteName
    )

    $safeSuite = Get-SafeName -Name $SuiteName
    $suiteDir = Join-Path $matrixSecretsRoot $safeSuite
    $secretPath = Join-Path $suiteDir "gcs_signing.key"
    $publicPath = Join-Path $suiteDir "gcs_signing.pub"

    if (-not (Test-Path $secretPath) -or -not (Test-Path $publicPath)) {
        Write-Host "[GCS] Generating signing identity for suite $SuiteName"
        if (-not (Test-Path $suiteDir)) {
            New-Item -ItemType Directory -Path $suiteDir -Force | Out-Null
        }

        $result = & $python -m core.run_proxy init-identity --suite $SuiteName --output-dir $suiteDir
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to generate signing identity for suite $SuiteName"
            exit 1
        }

        if (-not (Test-Path $secretPath) -or -not (Test-Path $publicPath)) {
            Write-Error "Signing identity generation for suite $SuiteName did not produce expected files"
            exit 1
        }

        Write-Host "[GCS] Stored signing identity at $suiteDir"
        Write-Host "[GCS] Copy $publicPath to the drone host before running this suite"
    }

    return @{ Secret = $secretPath; Public = $publicPath; Directory = $suiteDir }
}

function Wait-ForHandshake {
    param(
        [System.Diagnostics.Process] $Process,
        [string] $LogPath,
        [int] $TimeoutSec,
        [string] $Label
    )

    if ($null -eq $Process) {
        Write-Warning "[GCS] Cannot wait for handshake for $Label because process handle is null"
        return $false
    }

    $pattern = '"PQC handshake completed successfully"'
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($stopwatch.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if ($Process.HasExited) {
            Write-Warning "[GCS] Proxy process for $Label exited before handshake completed"
            return $false
        }

        if (Test-Path $LogPath) {
            try {
                if (Select-String -Path $LogPath -Pattern $pattern -SimpleMatch -Quiet) {
                    return $true
                }
            }
            catch {
                # File may be locked briefly while being written; retry
            }
        }

        Start-Sleep -Milliseconds 200
    }

    Write-Warning "[GCS] Timed out waiting for handshake for $Label after $TimeoutSec seconds"
    return $false
}

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
        $autoDiscoveredSuites = $true
        $suiteJson = & $python -c "import json; from core.suites import list_suites; print(json.dumps(list(list_suites().keys())))"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Failed to enumerate suites via python."
            exit 1
        }
        try {
            $Suites = @(
                (ConvertFrom-Json -InputObject $suiteJson)
            )
        }
        catch {
            Write-Error "Failed to parse suite list from python: $_"
            exit 1
        }
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

    if ($autoDiscoveredSuites) {
        $sortedSuites = [System.Collections.Generic.List[string]]::new()
        $sortedSuites.AddRange([string[]]$Suites)
        $sortedSuites.Sort([System.StringComparer]::Ordinal)
        $Suites = $sortedSuites
    }

    $schedulePretty = ($Suites -join ", ")
    Write-Host "[GCS] Suite plan: $schedulePretty"

    $logsRoot = $OutDir
    if (-not (Test-Path $logsRoot)) {
        New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null
    }

    $summaryCsv = Join-Path $logsRoot "matrix_gcs_summary.csv"

    $suiteIndex = 0
    $totalSuites = $Suites.Count
    foreach ($suite in $Suites) {
        $suiteIndex += 1
        $safe = Get-SafeName -Name $suite
        $duration = if ($suite -match "sphincs") { $SlowDurationSec } else { $DurationSec }

        $identity = Ensure-SuiteIdentity -SuiteName $suite

        $proxyJson = Join-Path $logsRoot ("gcs_{0}.json" -f $safe)
        $trafficDir = Join-Path $logsRoot (Join-Path "traffic" $safe)
        if (-not (Test-Path $trafficDir)) {
            New-Item -ItemType Directory -Path $trafficDir -Force | Out-Null
        }

        $trafficOut = Join-Path $trafficDir "gcs_events.jsonl"
        $trafficSummary = Join-Path $trafficDir "gcs_summary.json"
        $handshakeDir = Join-Path $logsRoot "handshake"
        if (-not (Test-Path $handshakeDir)) {
            New-Item -ItemType Directory -Path $handshakeDir -Force | Out-Null
        }
        $proxyLog = Join-Path $handshakeDir ("gcs_{0}_handshake.log" -f $safe)
        $proxyErr = Join-Path $handshakeDir ("gcs_{0}_handshake.err" -f $safe)
        if (Test-Path $proxyLog) {
            Remove-Item $proxyLog -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $proxyErr) {
            Remove-Item $proxyErr -Force -ErrorAction SilentlyContinue
        }

    Write-Host "[GCS][$suiteIndex/$totalSuites] Starting proxy for suite $suite ($duration s)"
        $suiteStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
        $proxyArgs = @(
            "-m", "core.run_proxy", "gcs",
            "--suite", $suite,
            "--gcs-secret-file", $identity.Secret,
            "--stop-seconds", $duration.ToString(),
            "--json-out", $proxyJson,
            "--quiet"
        )
        try {
            $proxyProc = Start-Process -FilePath $python -ArgumentList $proxyArgs -PassThru -WindowStyle Hidden -WorkingDirectory $repoRoot -RedirectStandardOutput $proxyLog -RedirectStandardError $proxyErr
        }
        catch {
            Write-Error ("[GCS][{0}/{1}] Failed to launch proxy for suite {2}: {3}" -f $suiteIndex, $totalSuites, $suite, $_.Exception.Message)
            continue
        }
        if ($null -eq $proxyProc) {
            Write-Error ("[GCS][{0}/{1}] Proxy process did not start for suite {2}" -f $suiteIndex, $totalSuites, $suite)
            continue
        }

        Write-Host "[GCS][$suiteIndex/$totalSuites] Waiting for handshake signal"
        $handshakeOk = Wait-ForHandshake -Process $proxyProc -LogPath $proxyLog -TimeoutSec $HandshakeTimeoutSec -Label $suite
        if (-not $handshakeOk) {
            if (-not $proxyProc.HasExited) {
                $proxyProc.WaitForExit()
            }
            Write-Warning "[GCS][$suiteIndex/$totalSuites] Skipping traffic for suite $suite due to handshake failure"
            continue
        }
        Write-Host "[GCS][$suiteIndex/$totalSuites] Handshake confirmed"

    Write-Host "[GCS][$suiteIndex/$totalSuites] Running traffic generator"
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

        $suiteStopwatch.Stop()

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

    Write-Host "[GCS][$suiteIndex/$totalSuites] Completed suite $suite in $([Math]::Round($suiteStopwatch.Elapsed.TotalSeconds, 2)) s (sent=$($row.traffic_sent_total) recv=$($row.traffic_recv_total) drops=$($row.drops))"
    }
}
finally {
    Pop-Location
}
