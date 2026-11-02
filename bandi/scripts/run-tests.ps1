# 功能：分段跑 pytest —— basetemp 收到系统临时目录、按组拆分，避免全量单跑占满系统盘
# 用法：
#   .\scripts\run-tests.ps1 -Group agent
#   .\scripts\run-tests.ps1 -Group contracts -Chunk 1 -Chunks 3
#   .\scripts\run-tests.ps1 -Group all
param(
    [ValidateSet("all", "agent", "memory", "core", "api", "company", "session", "tools", "llm", "contracts", "perception")]
    [string]$Group = "all",
    [int]$Chunk = 0,
    [int]$Chunks = 1,
    [string]$BaseTemp = (Join-Path $env:TEMP "bandi-pytest"),
    [switch]$StopOnFail
)

$ErrorActionPreference = "Stop"
$BandiDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $BandiDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "python not found: $Python"
}

# 该变量超 Windows 环境上限，测试内一律不继承
Remove-Item Env:ACC_PRODUCT_CONFIG_V3 -ErrorAction SilentlyContinue

$target = if ($Group -eq "all") { Join-Path $BandiDir "tests" } else { Join-Path $BandiDir "tests\test_$Group" }
if (-not (Test-Path -LiteralPath $target)) {
    throw "test group not found: $target"
}

$suffix = if ($Chunks -gt 1) { "-c$Chunk" } else { "" }
$basetemp = Join-Path $BaseTemp "$Group$suffix"
New-Item -ItemType Directory -Force -Path $basetemp | Out-Null

$arguments = @("-m", "pytest", $target, "-q", "--basetemp=$basetemp", "-p", "no:cacheprovider")

if ($Chunks -gt 1) {
    # 按文件名均分成 $Chunks 段——contracts 单目录跑满 40 分钟会撞 SQLite 资源上限
    $files = Get-ChildItem -Path $target -Filter "test_*.py" -Recurse | Sort-Object FullName
    $size = [math]::Ceiling($files.Count / $Chunks)
    $start = $Chunk * $size
    $take = [math]::Min($size, $files.Count - $start)
    if ($take -le 0) {
        throw "chunk $Chunk out of range (files=$($files.Count), chunks=$Chunks)"
    }
    $selected = $files[$start..($start + $take - 1)] | ForEach-Object { $_.FullName }
    $arguments = @("-m", "pytest") + $selected + @("-q", "--basetemp=$basetemp", "-p", "no:cacheprovider")
}

if ($StopOnFail) { $arguments += "-x" }

Write-Host "[bandi] group=$Group chunk=$Chunk/$Chunks basetemp=$basetemp"
& $Python @arguments
exit $LASTEXITCODE
