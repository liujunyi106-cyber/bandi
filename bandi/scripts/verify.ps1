<#
.SYNOPSIS
  Bandi daily delivery verification script
.DESCRIPTION
  Checks: ruff lint / ruff format / module imports / config load / directory tree / tests
  Exit: 0 = all pass, non-zero = failure
#>

$ErrorActionPreference = "Stop"
# UTF-8 强制（中文路径 + 非 UTF8 控制台下 execnet/xdist 子进程启动会 surrogate 编码崩溃）
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { throw "python not found" }
$ruff = Join-Path $root ".venv\Scripts\ruff.exe"
if (-not (Test-Path $ruff)) { $ruff = "ruff" }

$global:passed = 0
$global:failed = 0

function Check-Step($name, $scriptBlock) {
    try {
        & $scriptBlock
        Write-Host "  PASS $name"
        $global:passed++
    } catch {
        Write-Host "  FAIL $name -- $_"
        $global:failed++
    }
}

Write-Host "`n========================================"
Write-Host " Bandi Verify -- $(Get-Date -Format 'yyyy/MM/dd HH:mm')"
Write-Host "========================================`n"

# 1. ruff lint
Check-Step "ruff check" {
    if (-not (Test-Path $ruff)) { throw "ruff not installed" }
    $r = & $ruff check . 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ruff check failed: $r" }
}

# 2. ruff format check
Check-Step "ruff format" {
    if (-not (Test-Path $ruff)) { throw "ruff not installed" }
    $r = & $ruff format --check . 2>&1
    if ($LASTEXITCODE -ne 0) { throw "ruff format --check failed: $r" }
}

# 3. module imports — verify all subpackages are importable
Check-Step "module imports" {
    $script = Join-Path $root "scripts\_verify_imports.py"
    $r = & $python $script 2>&1
    if ($LASTEXITCODE -ne 0) { throw "import check failed: $r" }
}

# 4. config load — verify default config can be instantiated
Check-Step "config load" {
    $code = "from bandi.core.config import BandiConfig; c=BandiConfig(); print(f'config OK: agent={type(c.agent).__name__}, llm={type(c.llm).__name__}')"
    $r = & $python -c $code 2>&1
    if ($LASTEXITCODE -ne 0) { throw "config load failed: $r" }
}

# 5. directory tree — verify all source and test paths exist
Check-Step "directory tree (arch Section 19)" {
    $expected = @(
        "bandi\__main__.py",
        "bandi\agent", "bandi\agent\prompt_harness",
        "bandi\api", "bandi\cli", "bandi\company",
        "bandi\context", "bandi\context\sources",
        "bandi\core", "bandi\core\permission", "bandi\datasource",
        "bandi\eval", "bandi\llm",
        "bandi\llm\adapters", "bandi\llm\capabilities", "bandi\llm\providers",
        "bandi\memory", "bandi\memory\session",
        "bandi\observability", "bandi\ontology",
        "bandi\perception",
        "bandi\safety", "bandi\session",
        "bandi\resources",      "bandi\skills",         "bandi\tools", "bandi\utils",
        "tests\test_agent", "tests\test_api",
        "tests\test_cli", "tests\test_company",
        "tests\test_context", "tests\test_core",
        "tests\test_datasource", "tests\test_eval", "tests\integration",
        "tests\test_llm", "tests\test_memory",
        "tests\test_observability",
        "tests\test_perception", "tests\test_safety",
        "tests\test_session",         "tests\test_skills", "tests\test_tools", "tests\test_utils"
    )
    $missing = @()
    foreach ($p in $expected) {
        if (-not (Test-Path (Join-Path $root $p))) { $missing += $p }
    }
    if ($missing.Count -gt 0) { throw "missing dirs/files: $($missing -join ', ')" }
}

# 6. pytest — 三分组（主门禁 -n 4 不与墙钟预算/外部工具类争 CPU；两类仍必跑，非跳过）
#    分组依据（2026-09-28 实证：全量 -n 4 下 5 项红，隔离单跑 5 项全绿）：
#      - timing       真实并发临界区 + 墙钟预算（mem_engineering / robots）；隔离 25-51s
#      - external_tool 调外部可执行程序（LibreOffice）；隔离 ~71s
#    这两类是**负载敏感**而非缺陷，混跑必然误报；独立低并发分组让门禁变绿且不掩盖。
$pytestCommon = @("--tb=short")

Check-Step "pytest: main (-n 4, 排除 timing/external_tool)" {
    $r = & $python -m pytest @pytestCommon -n 4 -m "not timing and not external_tool" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "pytest(main) failed: $r" }
}

Check-Step "pytest: timing (串行, 墙钟预算类)" {
    $r = & $python -m pytest @pytestCommon -p no:randomly -m "timing" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "pytest(timing) failed: $r" }
}

Check-Step "pytest: external_tool (串行, 外部程序类)" {
    $r = & $python -m pytest @pytestCommon -p no:randomly -m "external_tool" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "pytest(external_tool) failed: $r" }
}

# 7. 安全门禁 — plan 写能力不外泄（L1-G05-02 门禁指标「越权拒绝率 100%」的自动化）
#    独立分组且**串行前置**：安全类门禁一票否决，不该被 -n 4 并发的偶发红掩盖；
#    纯静态/契约断言，不依赖 LLM 与网络，耗时 ~10s。
Check-Step "safety gates: plan 写能力不外泄" {
    $r = & $python -m pytest tests/test_contracts/test_contract_safety_gates.py `
        tests/test_contracts/test_contract_plan_mode_tool_scope.py `
        tests/test_contracts/test_contract_plan_gating.py `
        tests/test_contracts/test_contract_plan_whitelist_consistency.py `
        -q --tb=short -p no:randomly 2>&1
    if ($LASTEXITCODE -ne 0) { throw "safety gates failed: $r" }
}

# report
Write-Host "`n----------------------------------------"
Write-Host "  passed: $global:passed / failed: $global:failed / total: $($global:passed + $global:failed)"

if ($global:failed -gt 0) {
    Write-Host "  FAILED -- fix and retry"
    exit 1
} else {
    Write-Host "  ALL PASSED -- ready to commit"
    exit 0
}
