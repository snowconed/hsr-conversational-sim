param(
    [string]$EnginePath = $env:VOICEVOX_ENGINE_PATH,
    [ValidateRange(5, 180)][int]$TimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
$engineUrl = 'http://127.0.0.1:50021'
$officialEngineId = '074fc39e-678b-4c13-8916-ffca8d505d1d'

function Get-VoicevoxVersion {
    try {
        $version = Invoke-RestMethod -Uri "$engineUrl/version" -TimeoutSec 1
        $manifest = Invoke-RestMethod -Uri "$engineUrl/engine_manifest" -TimeoutSec 1
    } catch {
        return $null
    }
    if (($version -isnot [string]) -or -not $version -or $manifest.uuid -ne $officialEngineId) {
        throw 'Port 50021 is occupied by another service. No process was changed.'
    }
    return $version
}

# Serialize launchers; a running user-owned engine is reused and never stopped.
$launchMutex = New-Object System.Threading.Mutex($false, 'Local\RobotHouseholdVoicevox50021')
$hasMutex = $false
$engineProcess = $null
try {
    try {
        $hasMutex = $launchMutex.WaitOne(65000)
    } catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) { throw 'Another VOICEVOX launcher is still starting. Please try again.' }
    $version = Get-VoicevoxVersion
    if ($version) {
        Write-Host "VOICEVOX $version is ready (existing engine reused)."
        return
    }

    if (-not $EnginePath) {
        $candidates = @(
            (Join-Path $PSScriptRoot 'tools\voicevox\run.exe')
        )
        foreach ($candidate in $candidates) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $EnginePath = $candidate
                break
            }
        }
    }
    if (-not $EnginePath -or -not (Test-Path -LiteralPath $EnginePath -PathType Leaf)) {
        throw 'VOICEVOX is missing. Start the VOICEVOX app, or set VOICEVOX_ENGINE_PATH to its engine run.exe.'
    }
    $resolvedEngine = (Resolve-Path -LiteralPath $EnginePath).ProviderPath
    $engineDirectory = Split-Path $resolvedEngine -Parent
    $manifestPath = Join-Path $engineDirectory 'engine_manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'The selected folder does not contain a VOICEVOX engine manifest.'
    }
    $localManifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($localManifest.uuid -ne $officialEngineId) {
        throw 'The selected executable is not the expected VOICEVOX engine.'
    }
    $logDirectory = Join-Path $PSScriptRoot 'logs'
    [void](New-Item -ItemType Directory -Path $logDirectory -Force)
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fff'
    $outputLog = Join-Path $logDirectory "voicevox_$stamp.out.log"
    $errorLog = Join-Path $logDirectory "voicevox_$stamp.err.log"
    Write-Host 'Preparing local VOICEVOX speech...'
    $engineProcess = Start-Process -FilePath $resolvedEngine `
        -ArgumentList @('--host', '127.0.0.1', '--port', '50021', '--cpu_num_threads', '4', '--output_log_utf8', '--disable_mutable_api') `
        -WorkingDirectory $engineDirectory -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $outputLog -RedirectStandardError $errorLog
    $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
    while ($elapsed.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        if ($engineProcess.HasExited) {
            throw "VOICEVOX exited during startup. See $errorLog"
        }
        $version = Get-VoicevoxVersion
        if ($version) {
            if ($engineProcess.HasExited) { throw 'VOICEVOX exited while becoming ready.' }
            Write-Host "VOICEVOX $version is ready. Voice: Zundamon."
            # The engine is shared with other local apps and remains running.
            $engineProcess = $null
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "VOICEVOX startup timed out. See $errorLog"
} finally {
    # Only a process created by this invocation is eligible for failure cleanup.
    if ($null -ne $engineProcess) {
        try { if (-not $engineProcess.HasExited) { $engineProcess.Kill() } } catch {}
        $engineProcess.Dispose()
    }
    if ($hasMutex) { $launchMutex.ReleaseMutex() }
    $launchMutex.Dispose()
}
