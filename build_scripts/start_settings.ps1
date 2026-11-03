param(
    [ValidateRange(1024,65535)][int]$Port = 19327,
    [ValidateRange(1024,65535)][int]$ServerPort = 16888,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$taskSettingsFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'src\settings.json'
if (Test-Path -LiteralPath $taskSettingsFile) {
    try {
        $taskSavedAdvanced = (Get-Content -LiteralPath $taskSettingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).advanced
        if (-not $PSBoundParameters.ContainsKey('Port') -and ($taskSavedAdvanced.browser_port -is [int] -or $taskSavedAdvanced.browser_port -is [long])) {
            if ($taskSavedAdvanced.browser_port -ge 1024 -and $taskSavedAdvanced.browser_port -le 65535) { $Port = [int]$taskSavedAdvanced.browser_port }
        }
        if (-not $PSBoundParameters.ContainsKey('ServerPort') -and ($taskSavedAdvanced.server_port -is [int] -or $taskSavedAdvanced.server_port -is [long])) {
            if ($taskSavedAdvanced.server_port -ge 1024 -and $taskSavedAdvanced.server_port -le 65535) { $ServerPort = [int]$taskSavedAdvanced.server_port }
        }
    } catch { }
}
if ($Port -eq $ServerPort) { throw 'Chrome 連接 Port 與設定網頁 Port 不可相同，請重新執行並使用不同數字。' }
$taskRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$taskEnvironment = Join-Path $taskRoot '.venv'
$taskPython = Join-Path $taskEnvironment 'Scripts\python.exe'
$taskRequirements = Join-Path $taskRoot 'requirement.txt'

function Test-RuntimePython($taskPath) {
    if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf)) { return $false }
    try {
        & $taskPath -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,10),(3,11)) and sys.maxsize > 2**32 else 1)" 2>$null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

function Find-RuntimePython {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($taskVersion in @('-3.11', '-3.10')) {
            try {
                $taskFound = & py $taskVersion -c "import sys; print(sys.executable if sys.maxsize > 2**32 else '')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $taskFound -and (Test-RuntimePython $taskFound.Trim())) {
                    return $taskFound.Trim()
                }
            } catch { continue }
        }
    }
    $taskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($taskCommand -and $taskCommand.Source -notlike '*\Microsoft\WindowsApps\*' -and
            (Test-RuntimePython $taskCommand.Source)) { return $taskCommand.Source }
    throw '找不到可用的 64 位元 Python 3.10 或 3.11。請先安裝其中一個版本（包含 Python Launcher），再雙擊 start-settings.cmd。Python 3.12 以上不適用目前專案。'
}

try {
    if (-not (Test-Path -LiteralPath (Join-Path $taskRoot 'src\settings.py'))) {
        throw '找不到 src\settings.py，請將整個專案下載或解壓縮完成後，再執行根目錄的 start-settings.cmd。'
    }
    if (-not (Test-Path -LiteralPath $taskRequirements)) { throw '缺少 requirement.txt，請先更新完整專案。' }
    if (-not (Test-RuntimePython $taskPython)) {
        $taskBasePython = Find-RuntimePython
        if (Test-Path -LiteralPath $taskEnvironment) {
            # Preserve a copied/broken environment before rebuilding it.
            $taskBackup = Join-Path $taskRoot ('.venv.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6))
            if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($taskEnvironment)) -ne $taskRoot -or
                    [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($taskBackup)) -ne $taskRoot) {
                throw '虛擬環境路徑超出專案目錄，已停止操作。'
            }
            Move-Item -LiteralPath $taskEnvironment -Destination $taskBackup
            Write-Host '原有環境無法使用，已保留備份，接著建立本機環境。'
        }
        Write-Host '首次啟動：正在建立 Python 環境…'
        & $taskBasePython -m venv $taskEnvironment
        if ($LASTEXITCODE -ne 0 -or -not (Test-RuntimePython $taskPython)) { throw 'Python 環境建立失敗。' }
    }

    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try {
        $taskHash = [BitConverter]::ToString($taskHasher.ComputeHash([IO.File]::ReadAllBytes($taskRequirements))).Replace('-', '')
    } finally { $taskHasher.Dispose() }
    $taskMarker = Join-Path $taskEnvironment '.tickets-hunter-requirements.sha256'
    $taskInstalledHash = if (Test-Path -LiteralPath $taskMarker) { (Get-Content -LiteralPath $taskMarker -Raw).Trim() } else { '' }
    if ($taskInstalledHash -ne $taskHash) {
        Write-Host '正在安裝或更新專案套件，第一次執行可能需要幾分鐘…'
        & $taskPython -m pip install -r $taskRequirements
        if ($LASTEXITCODE -ne 0) { throw '套件安裝失敗，請確認網路連線及上方錯誤訊息，再重新執行。' }
        & $taskPython -m pip check
        if ($LASTEXITCODE -ne 0) { throw '套件相依性檢查失敗，請檢查上方訊息。' }
        Set-Content -LiteralPath $taskMarker -Value $taskHash -Encoding ASCII
    }
    if ($CheckOnly) { Write-Host "本機開發環境已準備完成。Chrome Port=$Port；設定網頁 Port=$ServerPort"; exit 0 }
    Set-Location -LiteralPath (Join-Path $taskRoot 'src')
    Write-Host '正在啟動設定介面…'
    $env:TICKETS_HUNTER_START_BROWSER_PORT = [string]$Port
    $env:TICKETS_HUNTER_START_SERVER_PORT = [string]$ServerPort
    & $taskPython settings.py
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
