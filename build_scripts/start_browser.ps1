param(
    [ValidateRange(1024,65535)][int]$Port = 19327,
    [string]$UserDataDir = '',
    [string]$ChromePath = '',
    [switch]$CheckOnly,
    [switch]$PromptPort
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
function Read-StartupPort($taskLabel, [int]$taskDefault) {
    while ($true) {
        $taskAnswer = Read-Host "$taskLabel [直接按 Enter 使用 $taskDefault]"
        if ([string]::IsNullOrWhiteSpace($taskAnswer)) { return $taskDefault }
        $taskParsedPort = 0
        if ([int]::TryParse($taskAnswer.Trim(), [ref]$taskParsedPort) -and $taskParsedPort -ge 1024 -and $taskParsedPort -le 65535) {
            return $taskParsedPort
        }
        Write-Host '請輸入 1024 到 65535 的整數。' -ForegroundColor Yellow
    }
}
$taskSettingsFile = Join-Path (Split-Path -Parent $PSScriptRoot) 'src\settings.json'
if (Test-Path -LiteralPath $taskSettingsFile) {
    try {
        $taskSavedAdvanced = (Get-Content -LiteralPath $taskSettingsFile -Raw -Encoding UTF8 | ConvertFrom-Json).advanced
        if (-not $PSBoundParameters.ContainsKey('Port') -and ($taskSavedAdvanced.browser_port -is [int] -or $taskSavedAdvanced.browser_port -is [long])) {
            if ($taskSavedAdvanced.browser_port -ge 1024 -and $taskSavedAdvanced.browser_port -le 65535) { $Port = [int]$taskSavedAdvanced.browser_port }
        }

    } catch { }
}
if ($PromptPort -and -not $PSBoundParameters.ContainsKey('Port')) {
    $Port = Read-StartupPort 'Chrome 連接 Port（需與另一個啟動腳本相同）' $Port
}
if (-not $UserDataDir) {
    $UserDataDir = Join-Path $env:LOCALAPPDATA "TicketsHunter\Chrome-$Port"
}
$taskProfile = [IO.Path]::GetFullPath($UserDataDir)
if ($taskProfile.Contains('"')) { throw 'Invalid browser data folder.' }

if (-not $ChromePath) {
    $taskCandidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    $ChromePath = $taskCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $ChromePath -or -not (Test-Path -LiteralPath $ChromePath -PathType Leaf)) {
    throw '找不到 Chrome，請先安裝 Google Chrome。'
}
if ($CheckOnly) {
    @{port=$Port; profile=$taskProfile; chrome=$ChromePath} | ConvertTo-Json -Compress
    exit 0
}

function Get-BrowserEndpoint {
    try {
        $taskVersion = Invoke-RestMethod "http://127.0.0.1:$Port/json/version" -TimeoutSec 2
        return ($taskVersion.Browser -match 'Chrome/' -and $taskVersion.webSocketDebuggerUrl -match '^ws://(127\.0\.0\.1|localhost):')
    } catch { return $false }
}

if (Get-BrowserEndpoint) {
    Write-Host "專用 Chrome 已開啟，Port：$Port。請在設定介面填相同 Port。"
    exit 0
}
$taskProbe = New-Object Net.Sockets.TcpClient
try {
    $taskConnection = $taskProbe.ConnectAsync('127.0.0.1', $Port)
    if ($taskConnection.Wait(800) -and $taskProbe.Connected) {
        throw "Port $Port 已被其他程式使用，請換一個 Port。"
    }
} catch [AggregateException] {
    # Connection refused means this port is available.
} finally {
    $taskProbe.Dispose()
}

New-Item -ItemType Directory -Force -Path $taskProfile | Out-Null
$taskArguments = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    "--user-data-dir=`"$taskProfile`"",
    '--no-first-run', '--no-default-browser-check',
    '--restore-last-session'
)
Start-Process -FilePath $ChromePath -ArgumentList $taskArguments
for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
    if (Get-BrowserEndpoint) {
        Write-Host "Chrome 已開啟，Port：$Port。登入後請開啟購票頁面，再到設定介面按開始。"
        Write-Host "登入資料保存在：$taskProfile"
        exit 0
    }
    Start-Sleep -Milliseconds 300
}
throw "Chrome 未開放 Port $Port。請關閉使用同一資料夾的 Chrome 後重試，或改用其他 Port。"
