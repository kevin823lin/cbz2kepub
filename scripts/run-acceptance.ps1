param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][string]$ThreadId,
    [string]$ResultsRoot,
    [int[]]$Limits = @(15, 59, 60, 70)
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if (-not $ResultsRoot) {
    $ResultsRoot = Join-Path $projectRoot ('tmp\acceptance-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
New-Item -ItemType Directory -Path $ResultsRoot -Force | Out-Null
$logPath = Join-Path $ResultsRoot 'acceptance.log'
$statePath = Join-Path $ResultsRoot 'runner.json'
$state = [ordered]@{ status = 'running'; pid = $PID; started = (Get-Date).ToString('o'); log = $logPath }
$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
try {
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $acceptancePath = Join-Path $projectRoot 'tests\acceptance_real.py'
    $env:PYTHONUTF8 = '1'
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    & $pythonPath -B $acceptancePath --source-dir $SourceDir --results-root $ResultsRoot --limits @Limits 2>&1 |
        Tee-Object -FilePath $logPath
    if ($LASTEXITCODE -ne 0) { throw "Acceptance failed: exit $LASTEXITCODE" }
    $state.status = 'completed'
} catch {
    $state.status = 'failed'
    $state['error'] = $_.Exception.Message
    Add-Content -LiteralPath $logPath -Value $_.Exception.Message -Encoding utf8
} finally {
    $state['finished'] = (Get-Date).ToString('o')
    $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
    $message = "【cbz2kepub migration acceptance callback】狀態：$($state.status)。$($Limits -join '/') 章依序執行；結果：$ResultsRoot\acceptance.json；日誌：$logPath。請讀取結果完成驗收，核對封面閱讀頁與 metadata；失敗先診斷，不自動重跑整輪。"
    try {
        $codex = Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Filter codex.exe -File -Recurse |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $codex) { throw '找不到 Codex Desktop 內建 CLI' }
        & $codex.FullName queue --thread $ThreadId --message $message
        if ($LASTEXITCODE -ne 0) { throw "Callback failed: exit $LASTEXITCODE" }
    } catch {
        $state['callbackError'] = $_.Exception.Message
        $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8
        throw
    }
}
if ($state.status -ne 'completed') { throw $state.error }
