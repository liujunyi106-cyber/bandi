<#
.SYNOPSIS
  Bandi 依赖漏洞扫描（pip-audit 封装）
.DESCRIPTION
  用 venv 内的 pip-audit 扫描已安装依赖的已知漏洞。
  未安装 pip-audit 时输出安装命令并以 exit 2 退出（不视为扫描失败）。
  用法（仓库根执行）： .\bandi\scripts\dep-scan.ps1
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# 不发 stderr 的可用性探测（避免 PS 5.1 把 native stderr 当终止错误）
$probe = "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip_audit') else 1)"
& $python -c $probe
if ($LASTEXITCODE -ne 0) {
    Write-Host ("pip-audit 未安装。安装： & '{0}' -m pip install pip-audit" -f $python)
    exit 2
}

Write-Host "== pip-audit：扫描当前 venv 依赖 =="
& $python -m pip_audit --desc
exit $LASTEXITCODE
