# 功能：独立重启本地内核——按端口清理旧进程、直接启动 uvicorn；默认立即返回（-Wait 才等待就绪）
param(
    [int]$Port = 8766,
    [int]$TimeoutSec = 90,
    [string]$Python = "",
    [switch]$Wait
)

$ErrorActionPreference = "Stop"
$BandiDir = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $Python = Join-Path $BandiDir ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python not found: $Python"
}

$logDir = Join-Path $env:TEMP "bandi-kernel"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$outLog = Join-Path $logDir "kernel-$Port.out.log"
$errLog = Join-Path $logDir "kernel-$Port.err.log"
Remove-Item -LiteralPath $outLog, $errLog -ErrorAction SilentlyContinue

function Get-ListenerPid([int]$port) {
    $row = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($row) { return [int]$row.OwningProcess }
    return 0
}

$oldPid = Get-ListenerPid $Port
if ($oldPid -gt 0) {
    Write-Host "[bandi] stopping kernel pid=$oldPid on port $Port"
    taskkill /PID $oldPid /T /F 2>$null | Out-Null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 250
        if ((Get-ListenerPid $Port) -eq 0) { break }
    }
    if ((Get-ListenerPid $Port) -gt 0) {
        throw "port $Port is still occupied after taskkill"
    }
}

$watch = [System.Diagnostics.Stopwatch]::StartNew()
$env:BANDI_STARTUP_TRACE = "1"
$arguments = @("-m", "uvicorn", "bandi.api.app:app", "--host", "127.0.0.1", "--port", "$Port")
$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $BandiDir -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog -WindowStyle Hidden
Write-Host "[bandi] kernel starting pid=$($process.Id); logs: $outLog / $errLog"
if (-not $Wait) {
    Write-Host "[bandi] non-blocking start requested (add -Wait to block until /health is ready)"
    exit 0
}

$ready = $false
$lastDot = -1
while ($watch.Elapsed.TotalSeconds -lt $TimeoutSec) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        Write-Host ""
        Write-Host "[bandi] kernel exited early code=$($process.ExitCode)"
        break
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    } catch {
        # 内核尚未监听：继续等待
    }
    $second = [int]$watch.Elapsed.TotalSeconds
    if ($second -gt $lastDot) {
        Write-Host -NoNewline "."
        $lastDot = $second
    }
}

Write-Host ""
if ($ready) {
    Write-Host ("[bandi] kernel ready in {0:N1}s pid={1} port={2}" -f $watch.Elapsed.TotalSeconds, $process.Id, $Port)
    exit 0
}

Write-Host "[bandi] kernel NOT ready within $TimeoutSec s; stderr tail:"
Get-Content -LiteralPath $errLog -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 30
exit 1
