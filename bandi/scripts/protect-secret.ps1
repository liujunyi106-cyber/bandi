<#
.SYNOPSIS
  把明文密钥加密为 dpapi: 值（Windows DPAPI，供 config.yaml 使用）
.DESCRIPTION
  输出 dpapi:<base64> 字符串，可直接粘贴到 ~/.bandi/config.yaml 的 llm.api_key。
  加载时 core/config.py 自动解密（非 Windows/解密失败保留原值）。
  用法（仓库根执行）： .\bandi\scripts\protect-secret.ps1 -Secret "sk-..."
#>

param([Parameter(Mandatory = $true)][string]$Secret)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }
$env:PYTHONPATH = $root

$code = "import sys; from bandi.core.security.secret_protect import protect; print(protect(sys.argv[1]))"
& $python -c $code $Secret
exit $LASTEXITCODE
