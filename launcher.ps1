# AM-Workflow 一键启动：隐藏窗口拉起 Mock 供应商 + 应用，就绪后打开浏览器。
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root "backend"
$python = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "logs"
$url = "http://127.0.0.1:8099/"

if (-not (Test-Path $python)) {
    Write-Host ""
    Write-Host "Python venv not found:" $python
    Write-Host "Please follow README section 2.1 to install dependencies first."
    exit 1
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port([int]$port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect("127.0.0.1", $port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Open-Browser {
    if ($env:AM_NO_BROWSER -eq "1") { return }
    Start-Process $url
}

if (Test-Port 8099) {
    Write-Host "AM-Workflow is already running, opening browser..."
    Open-Browser
    exit 0
}

$mockPid = $null
if (-not (Test-Port 8100)) {
    $mock = Start-Process -FilePath $python `
        -ArgumentList @("-m", "uvicorn", "scripts.mock_provider:app", "--host", "127.0.0.1", "--port", "8100") `
        -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "mock.log") `
        -RedirectStandardError (Join-Path $logs "mock.err.log")
    $mockPid = $mock.Id
    Write-Host "Mock provider started (pid $mockPid)."
} else {
    Write-Host "Mock provider already running."
}

$api = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8099") `
    -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logs "api.log") `
    -RedirectStandardError (Join-Path $logs "api.err.log")

@{ api = $api.Id; mock = $mockPid; startedAt = (Get-Date).ToString("s") } |
    ConvertTo-Json | Set-Content (Join-Path $logs "pids.json") -Encoding UTF8

for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Port 8099) {
        Start-Sleep -Milliseconds 800
        Open-Browser
        Write-Host ""
        Write-Host "AM-Workflow is ready: $url"
        Write-Host "Logs: $logs"
        Write-Host "To stop it, double-click stop-am-workflow.bat"
        exit 0
    }
}

Write-Host "Server did not become ready in 30s. Check $logs\api.err.log"
exit 1
