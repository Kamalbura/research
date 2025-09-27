Param(
    [string]$RepoRoot = "C:/Users/burak/Desktop/research",
    [string]$SuitesFile = "suites_common.txt",
    [string]$DroneHost = $Env:DRONE_HOST,
    [string]$DroneUser = "dev",
    [string]$ResultsDir = "",
    [int]$TrafficSeconds = 60,
    [int]$ProxySeconds = 90,
    [int]$RekeySeconds = 120,
    [switch]$SkipPerSuite,
    [switch]$SkipRekey,
    [switch]$SkipTap
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' is not available in PATH."
    }
}

Require-Command python
Require-Command ssh
Require-Command scp

if (-not (Test-Path $RepoRoot)) {
    throw "Repo root '$RepoRoot' not found."
}

Set-Location $RepoRoot

if (-not (Test-Path $SuitesFile)) {
    throw "Suites file '$SuitesFile' not found."
}

if ([string]::IsNullOrWhiteSpace($DroneHost)) {
    throw "Drone host is not defined. Set DRONE_HOST or pass -DroneHost."
}

if ([string]::IsNullOrWhiteSpace($ResultsDir)) {
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $ResultsDir = Join-Path $RepoRoot "results-$timestamp"
}

if (-not (Test-Path $ResultsDir)) {
    New-Item -ItemType Directory -Path $ResultsDir | Out-Null
}

Write-Host "Results directory: $ResultsDir"

function Invoke-DroneCommand {
    param(
        [string]$Command,
        [switch]$NoPrefix
    )

    $prefix = "source ~/cenv/bin/activate && cd ~/research &&"
    $payload = if ($NoPrefix) { $Command } else { "$prefix $Command" }
    & ssh "$DroneUser@$DroneHost" $payload
}

function Wait-DroneFile {
    param(
        [string]$RemotePath,
        [int]$TimeoutSeconds = 120
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $result = Invoke-DroneCommand "if [ -f '$RemotePath' ]; then echo ready; else echo waiting; fi" -NoPrefix
        if ($result -is [Array]) {
            $result = $result[-1]
        }
        if ($result -match 'ready') {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Timed out waiting for $RemotePath"
}

function Sanitize-SuiteId {
    param([string]$Suite)
    return ($Suite -replace "[^a-zA-Z0-9\-]", "_")
}

$suites = Get-Content $SuitesFile | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

if (-not $SkipPerSuite) {
    foreach ($suite in $suites) {
        $suiteTrim = $suite.Trim()
        if (-not $suiteTrim) { continue }
        $safeSuite = Sanitize-SuiteId $suiteTrim
        Write-Host "--- Running suite $suiteTrim ---" -ForegroundColor Cyan

        # Clean previous artifacts
        if (Test-Path "$RepoRoot/gcs_debug.json") { Remove-Item "$RepoRoot/gcs_debug.json" -Force }
        Invoke-DroneCommand "rm -f ~/research/drone_debug.json ~/research/drone_traffic_$safeSuite.log" -NoPrefix | Out-Null

        # Start GCS proxy
        $gcsArgs = "-m","core.run_proxy","gcs","--suite",$suiteTrim,"--stop-seconds",$ProxySeconds.ToString(),"--json-out","gcs_debug.json"
        $gcsProxy = Start-Process -FilePath "python" -ArgumentList $gcsArgs -WorkingDirectory $RepoRoot -PassThru
        Start-Sleep -Seconds 2

        # Start drone proxy in background
        $droneProxyCmd = "nohup python -m core.run_proxy drone --suite '$suiteTrim' --stop-seconds $ProxySeconds --json-out drone_debug.json > drone_proxy_$safeSuite.log 2>&1 & echo $!"
        $droneProxyPid = Invoke-DroneCommand $droneProxyCmd
        $droneProxyPid = ($droneProxyPid | Select-Object -Last 1).Trim()
        Write-Host "Drone proxy PID: $droneProxyPid"
        Start-Sleep -Seconds 2

        # Launch drone traffic (background job)
        $droneTrafficCmd = "python tools/traffic_drone.py --seconds $TrafficSeconds"
        $droneTrafficJob = Start-Job -ScriptBlock {
            param($User,$Host,$Cmd)
            & ssh "$User@$Host" "source ~/cenv/bin/activate && cd ~/research && $Cmd"
        } -ArgumentList $DroneUser,$DroneHost,$droneTrafficCmd

        # Local traffic capture
        $gcsTrafficLog = Join-Path $ResultsDir "gcs_traffic_$safeSuite.log"
        Write-Host "Running local traffic for $TrafficSeconds seconds"
        python tools\traffic_gcs.py --seconds $TrafficSeconds 2>&1 | Tee-Object -FilePath $gcsTrafficLog | Out-Null

        Wait-Job $droneTrafficJob | Out-Null
        $droneOutput = Receive-Job $droneTrafficJob
        $droneTrafficLog = Join-Path $ResultsDir "drone_traffic_$safeSuite.log"
        [System.IO.File]::WriteAllLines($droneTrafficLog, $droneOutput)

        # Wait for proxies to flush
        $gcsProxy.WaitForExit()
        Start-Sleep -Seconds 5

        # Collect artifacts
        $gcsDebugSrc = Join-Path $RepoRoot "gcs_debug.json"
        if (Test-Path $gcsDebugSrc) {
            Move-Item $gcsDebugSrc (Join-Path $ResultsDir "gcs_debug_$safeSuite.json") -Force
        } else {
            Write-Warning "Missing gcs_debug.json for $suiteTrim"
        }

        Wait-DroneFile "~/research/drone_debug.json"
        & scp "$DroneUser@$DroneHost:~/research/drone_debug.json" (Join-Path $ResultsDir "drone_debug_$safeSuite.json") | Out-Null

        $latestGcsLog = Get-ChildItem "$RepoRoot/logs" -Filter "gcs-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latestGcsLog) {
            Copy-Item $latestGcsLog.FullName (Join-Path $ResultsDir "gcs_log_$safeSuite.log") -Force
        }

        $lastDroneLog = (Invoke-DroneCommand "ls -t ~/research/logs/drone-*.log | head -n1" -NoPrefix | Select-Object -Last 1).Trim()
        if ($lastDroneLog) {
            & scp "$DroneUser@$DroneHost:$lastDroneLog" (Join-Path $ResultsDir "drone_log_$safeSuite.log") | Out-Null
        }

        Write-Host "Completed suite $suiteTrim" -ForegroundColor Green
        Start-Sleep -Seconds 5
    }
}

if (-not $SkipRekey) {
    $baseline = "cs-mlkem768-aesgcm-mldsa65"
    $target = "cs-mlkem1024-aesgcm-falcon1024"
    if ($suites -notcontains $baseline -or $suites -notcontains $target) {
        Write-Warning "Rekey suites $baseline or $target not present; skipping rekey scenario."
    } else {
        Write-Host "--- Rekey scenario $baseline -> $target ---" -ForegroundColor Cyan
        if (Test-Path "$RepoRoot/gcs_rekey.json") { Remove-Item "$RepoRoot/gcs_rekey.json" -Force }
        Invoke-DroneCommand "rm -f ~/research/drone_rekey.json" -NoPrefix | Out-Null

        $gcsArgs = "-m","core.run_proxy","gcs","--suite",$baseline,"--stop-seconds",$RekeySeconds.ToString(),"--json-out","gcs_rekey.json"
        $gcsProxy = Start-Process -FilePath "python" -ArgumentList $gcsArgs -WorkingDirectory $RepoRoot -PassThru
        Start-Sleep -Seconds 2

        $droneProxyCmd = "nohup python -m core.run_proxy drone --suite '$baseline' --stop-seconds $RekeySeconds --json-out drone_rekey.json > drone_rekey_proxy.log 2>&1 & echo $!"
        $droneProxyPid = Invoke-DroneCommand $droneProxyCmd
        Start-Sleep -Seconds 2

        $rekeyTrafficLog = Join-Path $ResultsDir "gcs_rekey.log"
        python tools\traffic_gcs.py --seconds 100 --rekey $target --rekey_at 20 2>&1 | Tee-Object -FilePath $rekeyTrafficLog | Out-Null

        $droneRekeyTrafficLog = Join-Path $ResultsDir "drone_rekey.log"
        $droneRekeyJob = Start-Job -ScriptBlock {
            param($User,$Host)
            & ssh "$User@$Host" "source ~/cenv/bin/activate && cd ~/research && python tools/traffic_drone.py --seconds 100"
        } -ArgumentList $DroneUser,$DroneHost
        Wait-Job $droneRekeyJob | Out-Null
        $droneRekeyOutput = Receive-Job $droneRekeyJob
        [System.IO.File]::WriteAllLines($droneRekeyTrafficLog, $droneRekeyOutput)

        $gcsProxy.WaitForExit()
        Start-Sleep -Seconds 5

        Move-Item "$RepoRoot/gcs_rekey.json" (Join-Path $ResultsDir "gcs_rekey.json") -Force
        Wait-DroneFile "~/research/drone_rekey.json"
        & scp "$DroneUser@$DroneHost:~/research/drone_rekey.json" (Join-Path $ResultsDir "drone_rekey.json") | Out-Null

        $latestGcsLog = Get-ChildItem "$RepoRoot/logs" -Filter "gcs-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latestGcsLog) {
            Copy-Item $latestGcsLog.FullName (Join-Path $ResultsDir "gcs_rekey_log.log") -Force
        }
        $lastDroneLog = (Invoke-DroneCommand "ls -t ~/research/logs/drone-*.log | head -n1" -NoPrefix | Select-Object -Last 1).Trim()
        if ($lastDroneLog) {
            & scp "$DroneUser@$DroneHost:$lastDroneLog" (Join-Path $ResultsDir "drone_rekey_log.log") | Out-Null
        }
    }
}

if (-not $SkipTap) {
    Write-Host "--- Sequence number tap (manual supervision required) ---" -ForegroundColor Cyan
    Write-Host "Launch taps in separate terminals while proxies are running."
    $tapInstructions = @'
1. On drone:
   python tools/udp_forward_log.py --listen 0.0.0.0:46012 --forward 127.0.0.1:56012 --label enc_GCS_to_drone | tee enc_g2d_tap.log

2. On GCS:
   python tools\udp_forward_log.py --listen 0.0.0.0:46011 --forward 127.0.0.1:56011 --label enc_drone_to_GCS | Tee-Object -FilePath enc_d2g_tap.log

3. After ~30 seconds, Ctrl+C both taps and move resulting logs into `$ResultsDir`.
'@
    Write-Host $tapInstructions
}

Write-Host "LAN matrix automation completed. Review artifacts in $ResultsDir" -ForegroundColor Green
