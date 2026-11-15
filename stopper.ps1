# AM-Workflow 一键停止：结束占用 8099 / 8100 的进程。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logs = Join-Path $root "logs"
$stopped = @()

function Stop-Port([int]$port) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        try {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop
            $script:stopped += $port
        } catch { }
    }
}

$pidFile = Join-Path $logs "pids.json"
if (Test-Path $pidFile) {
    try {
        $saved = Get-Content $pidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($procId in @($saved.api, $saved.mock)) {
            if ($procId) {
                try {
                    Stop-Process -Id $procId -Force -ErrorAction Stop
                    $script:stopped += "pid $procId"
                } catch { }
            }
        }
    } catch { }
}

Stop-Port 8099
Stop-Port 8100

if ($stopped.Count -gt 0) {
    Write-Host "Stopped: $($stopped -join ', ')"
} else {
    Write-Host "AM-Workflow is not running."
}
Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
