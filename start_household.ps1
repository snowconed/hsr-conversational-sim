param([switch]$Headless)
$ErrorActionPreference = 'Stop'
# Speech runs on Windows; the browser reaches this loopback engine directly.
# A missing engine must not prevent the text/chat interface from starting.
try {
    & (Join-Path $PSScriptRoot 'start_voicevox.ps1')
} catch {
    Write-Warning ("Robot speech is unavailable: " + $_.Exception.Message)
}
# Pass the existing API key to the WSL process without printing or storing it.
foreach ($settingName in @('OPENAI_API_KEY', 'OPENAI_PROGRAM_MODEL', 'OPENAI_REALTIME_TRANSCRIBE_MODEL')) {
    if ([Environment]::GetEnvironmentVariable($settingName)) {
        $entries = @($env:WSLENV -split ':' | Where-Object {
            $_ -and ($_ -split '/')[0] -ne $settingName
        })
        $env:WSLENV = (($entries + ($settingName + '/u')) -join ':')
    }
}
# Forward slashes and --exec keep paths literal across Windows and Linux.
$windowsPath = $PSScriptRoot.Replace('\', '/')
$linuxPathOutput = & wsl.exe --distribution Ubuntu-24.04 --exec wslpath -a $windowsPath
if ($LASTEXITCODE -ne 0) { throw 'Could not convert the project path in Ubuntu-24.04.' }
$linuxPath = ($linuxPathOutput -join "`n").Trim()
if (-not $linuxPath) { throw 'WSL did not return the project path.' }
if ($Headless) {
    & wsl.exe --distribution Ubuntu-24.04 --exec bash "$linuxPath/start_household.sh" --headless
} else {
    & wsl.exe --distribution Ubuntu-24.04 --exec bash "$linuxPath/start_household.sh"
}
exit $LASTEXITCODE
