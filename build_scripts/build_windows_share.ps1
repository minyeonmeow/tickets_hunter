$ErrorActionPreference = 'Stop'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONUTF8 = '1'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'

function Test-BuildPython($taskPath) {
    if (-not (Test-Path -LiteralPath $taskPath)) { return $false }
    try {
        & $taskPath -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,10),(3,11)) and sys.maxsize > 2**32 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

if (-not (Test-BuildPython $taskPython)) {
    $taskPython = Join-Path $taskRoot '.build-venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $taskPython)) {
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
            throw 'Building requires 64-bit Python 3.10 or 3.11 and the Python launcher. Recipients do not need Python.'
        }
        $taskVersion = $null
        foreach ($taskCandidate in @('-3.11', '-3.10')) {
            try {
                & py $taskCandidate -c "import sys; sys.exit(0 if sys.maxsize > 2**32 else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) { $taskVersion = $taskCandidate; break }
            } catch { continue }
        }
        if (-not $taskVersion) { throw 'Building requires 64-bit Python 3.10 or 3.11. Recipients do not need Python.' }
        & py $taskVersion -m venv (Join-Path $taskRoot '.build-venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create build environment.' }
    }
}
if (-not (Test-BuildPython $taskPython)) { throw 'Build environment must use 64-bit Python 3.10 or 3.11.' }

& $taskPython (Join-Path $PSScriptRoot 'build_windows_share.py')
exit $LASTEXITCODE
