param([int]$Port = 8765, [switch]$Stop)
$projectRoot = $PSScriptRoot
$scriptPath = Join-Path $projectRoot 'webapp.py'
$pidPath = Join-Path $projectRoot 'reports\server.pid'
if ($Stop) {
    if (Test-Path -LiteralPath $pidPath) {
        $serverPid = [int](Get-Content -LiteralPath $pidPath)
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid"
        if ($processInfo -and $processInfo.CommandLine.Contains($scriptPath)) {
            Stop-Process -Id $serverPid
            Write-Output 'Stopped SSE50 workbench.'
        }
    }
    exit
}
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    if ($health.app -eq 'sse50-workbench') {
        Write-Output "Local: http://127.0.0.1:$Port"
        exit
    }
} catch {}
$runtimePython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$pythonCandidates = @((Join-Path $projectRoot '.venv\Scripts\python.exe'), $runtimePython)
$systemPython = Get-Command python -ErrorAction SilentlyContinue
if ($systemPython) { $pythonCandidates += $systemPython.Source }
$chosenPython = $null
foreach ($candidate in $pythonCandidates) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate -c 'import pyarrow' 2>$null
        if ($LASTEXITCODE -eq 0) { $chosenPython = $candidate; break }
    }
}
if (-not $chosenPython) { throw 'Install Python and pyarrow first: python -m pip install -r requirements.txt' }
$reportDir = Join-Path $projectRoot 'reports'
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$serverProcess = Start-Process -FilePath $chosenPython -ArgumentList @('-u', ('"' + $scriptPath + '"'), '--port', $Port) -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $reportDir 'server.out.log') -RedirectStandardError (Join-Path $reportDir 'server.err.log') -PassThru
Set-Content -LiteralPath $pidPath -Value $serverProcess.Id -Encoding ascii
Write-Output "Local: http://127.0.0.1:$Port"
