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
$ResultsRoot = (Resolve-Path -LiteralPath $ResultsRoot).Path
$SourceDir = (Resolve-Path -LiteralPath $SourceDir).Path
$logPath = Join-Path $ResultsRoot 'acceptance.log'
$statePath = Join-Path $ResultsRoot 'runner.json'
$acceptanceReportPath = Join-Path $ResultsRoot 'acceptance.json'
if ((Test-Path -LiteralPath $statePath) -or (Test-Path -LiteralPath $acceptanceReportPath) -or
    (Get-ChildItem -LiteralPath $ResultsRoot -Directory -Filter 'chapters-*' | Select-Object -First 1)) {
    throw 'Acceptance results already exist; choose a new results directory to preserve them'
}
$acceptancePath = Join-Path $projectRoot 'tests\acceptance_real.py'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$commit = (& git -c "safe.directory=$projectRoot" -C $projectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read startup commit' }
$kccRoot = Join-Path $projectRoot 'kcc'
$kccCommit = (& git -c "safe.directory=$kccRoot" -C $kccRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not read startup KCC commit' }
$dirtyLines = @(& git -c "safe.directory=$projectRoot" -C $projectRoot status --porcelain)
if ($LASTEXITCODE -ne 0) { throw 'Could not read startup working tree state' }
$dirty = $dirtyLines.Count -gt 0
$kccDirtyLines = @(& git -c "safe.directory=$kccRoot" -C $kccRoot status --porcelain)
if ($LASTEXITCODE -ne 0) { throw 'Could not read startup KCC working tree state' }
$kccDirty = $kccDirtyLines.Count -gt 0
$started = (Get-Date).ToString('o')
$command = @($pythonPath, '-B', $acceptancePath, '--source-dir', $SourceDir,
    '--results-root', $ResultsRoot, '--limits') + @($Limits | ForEach-Object { $_.ToString() })
$state = [ordered]@{ status = 'running'; pid = $PID; started = $started; log = $logPath; source_dir = $SourceDir;
    limits = @($Limits); commit = $commit; dirty = $dirty; kcc_commit = $kccCommit; kcc_dirty = $kccDirty; command = $command }
function Write-JsonAtomic([string]$Path, $Value) {
    $tempPath = $Path + '.tmp'
    [System.IO.File]::WriteAllText($tempPath, ($Value | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempPath -Destination $Path -Force
}
Write-JsonAtomic $statePath $state
$initialAcceptance = [ordered]@{ status = 'running'; source_dir = $SourceDir; limits = @($Limits);
    commit = $commit; dirty = $dirty; kcc_commit = $kccCommit; kcc_dirty = $kccDirty;
    command = $command; started_at = $started; cases = @() }
Write-JsonAtomic (Join-Path $ResultsRoot 'acceptance.json') $initialAcceptance
try {
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
    Write-JsonAtomic $statePath $state
    $message = "【cbz2kepub chapter-pairing acceptance callback】狀態：$($state.status)。$($Limits -join '/') 章依序執行；結果：$ResultsRoot\acceptance.json；日誌：$logPath。請核對配對章/組摘要與報告，以及封面和metadata；失敗先診斷，不自動重跑整輪。"
    try {
        $codex = Get-ChildItem -LiteralPath "$env:LOCALAPPDATA\OpenAI\Codex\bin" -Filter codex.exe -File -Recurse |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $codex) { throw '找不到 Codex Desktop 內建 CLI' }
        & $codex.FullName queue --thread $ThreadId --message $message
        if ($LASTEXITCODE -ne 0) { throw "Callback failed: exit $LASTEXITCODE" }
    } catch {
        $state['callbackError'] = $_.Exception.Message
        Write-JsonAtomic $statePath $state
        throw
    }
}
if ($state.status -ne 'completed') { throw $state.error }
