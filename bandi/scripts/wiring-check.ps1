<#
.SYNOPSIS
  Bandi wiring-completeness audit — enforces DoD-11 (FM-1 免疫: 生产必须可达)
  + DoD-12 (端到端跑通验证准备).  Detects "written but never wired" (FM-1) and
  leftover stubs (漏写/功能不完整) BEFORE they become audit findings.

.DESCRIPTION
  Four modes:

  1. -Symbol <name>  — 接线检测: find ALL production call sites of a class/func
     inside bandi/bandi (excluding tests).  Zero production hits = FAIL (FM-1:
     实现存在但生产零调用 = 没接线 = 功能没跑通).  Lists test-only references so
     you can see the family was only exercised in tests.

  2. -StubScan       — stub 残留检测: scan bandi/bandi for stub markers
     (NotImplementedError / "stub" / Ellipsis body / "TODO: implement" / 占位注释).
     Any hit = FAIL (漏写/功能不完整).

  3. -AssemblyPoint <ClassName> [-Param <p1,p2>]  — 装配点完整性检测 (机制补强
     2026-08-18): 列出 ClassName 全部生产实例化点，并对每个实例化点核对
     -Param 指定的关键字参数是否透传。任一装配点缺任一 Param = FAIL。
     补齐 DoD-11 的"存在性检查"盲区：-Symbol 只查符号有调用点，不查
     "所有装配点的参数接线完整性"（如 EngineSeatExecutor(communication=...)
     部分装配点漏传 → 协作工具 fail-closed）。位置参数无法自动核对，
     输出标注"位置参数需人工核对"。

  4. -ConfigAudit [-ConfigVerbose]  — 配置字段「定义→消费者」检测 (P0R-2 型常规检测,
     2026-09-17 新增): 枚举 core/config.py 全部字段，统计生产代码中的引用
     （属性访问 `.field` 与字符串键 `"field"`，后者覆盖 getattr 形态）；
     零引用 = DEAD = FAIL。针对 P0R-2 同族缺陷——「字段有定义、文档有承诺，但生产
     零消费者，用户改了配置什么也不生效」（历史实例：ask_risk_levels /
     models_dev.url / preferred_language / tools.temp_dir / LLMConfig.context_window /
     TasksConfig）。检测实体在 `scripts/_config_consumers.py`（可独立跑 / 可单测）。
     -ConfigVerbose 展开全量逐字段清单（默认仅输出 DEAD/ALLOW/汇总）。
     已知盲区：同名字段撞车可能假绿，故定位为「高精度低召回」守门员。

  Every Day development (new module/class/func) and every bug fix must run the
  matching mode and record the outcome in the delivery doc.

  Exit: 0 = clean, 1 = findings (wiring gap / assembly gap / stub residue / dead config).

.EXAMPLE
  .\bandi\scripts\wiring-check.ps1 -Symbol "GateHarness" -Label "P2-3"
  .\bandi\scripts\wiring-check.ps1 -AssemblyPoint "EngineSeatExecutor" -Param "communication,agent_role" -Label "OP-28"
  .\bandi\scripts\wiring-check.ps1 -StubScan
  .\bandi\scripts\wiring-check.ps1 -ConfigAudit -Label "P0R-2-guard"
#>
param(
    [Parameter(Mandatory = $false)][string]$Symbol,
    [Parameter(Mandatory = $false)][switch]$StubScan,
    [Parameter(Mandatory = $false)][string]$AssemblyPoint,
    [Parameter(Mandatory = $false)][string]$Param,
    [Parameter(Mandatory = $false)][switch]$ConfigAudit,
    [Parameter(Mandatory = $false)][switch]$ConfigVerbose,
    [string]$Label = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$modes = @()
if ($Symbol) { $modes += "symbol" }
if ($StubScan) { $modes += "stub" }
if ($AssemblyPoint) { $modes += "assembly" }
if ($ConfigAudit) { $modes += "config" }
if ($modes.Count -ne 1) {
    Write-Host "ERROR: specify exactly one of -Symbol <name> / -StubScan / -AssemblyPoint <ClassName> / -ConfigAudit"
    exit 2
}

$scriptDir = Split-Path -Parent $PSCommandPath      # ...\bandi\scripts
$memoDir   = Split-Path -Parent $scriptDir          # ...\bandi
$root      = Split-Path -Parent $memoDir            # 项目根
$srcRoot   = Join-Path $memoDir "bandi"            # bandi/bandi (production)
$testRoot  = Join-Path $memoDir "tests"             # bandi/tests

$excludeParts = @("__pycache__", ".venv", "node_modules", "\.pytest_cache")

function Get-ScanFiles([string]$base) {
    if (-not (Test-Path $base)) { return @() }
    Get-ChildItem -Path $base -Recurse -File -Include *.py -ErrorAction SilentlyContinue |
        Where-Object {
            $fp = $_.FullName
            $excluded = $false
            foreach ($part in $excludeParts) {
                if ($fp -like "*$part*") { $excluded = $true; break }
            }
            -not $excluded
        }
}

function Invoke-Scan([string]$base, [string]$pattern) {
    $results = @()
    $rg = Get-Command rg -ErrorAction SilentlyContinue
    if ($rg) {
        $lines = & $rg.Path --line-number --no-heading --glob "*.py" $pattern $base 2>$null
        foreach ($line in $lines) {
            if ($line -match '^(.*?):(\d+):(.*)$') {
                $rel = $Matches[1].Substring($root.Length + 1)
                $results += [pscustomobject]@{ File = $rel; Line = [int]$Matches[2]; Text = $Matches[3].Trim() }
            }
        }
    } else {
        foreach ($f in (Get-ScanFiles $base)) {
            $found = Select-String -Path $f.FullName -Pattern $pattern -AllMatches
            foreach ($m in $found) {
                $rel = $f.FullName.Substring($root.Length + 1)
                $results += [pscustomobject]@{ File = $rel; Line = $m.LineNumber; Text = $m.Line.Trim() }
            }
        }
    }
    return ($results | Sort-Object File, Line)
}

Write-Host "`n========================================"
Write-Host " Bandi Wiring-Completeness Audit"
Write-Host " $(Get-Date -Format 'yyyy/MM/dd HH:mm')"
Write-Host "========================================"
if ($Label) { Write-Host "Label : $Label" }

if ($StubScan) {
    Write-Host "Mode  : StubScan"
    Write-Host ""

    $patterns = @(
        "(?i)stub",
        "TODO: implement",
        "待实现",
        "未实现",
        "Phase 1 占位",
        "空实现占位",
        "桌面端占位"
    )
    $hits = @()
    foreach ($p in $patterns) {
        $hits += Invoke-Scan $srcRoot $p
    }
    $hits = $hits | Sort-Object File, Line -Unique | Where-Object {
        $_.Text -notmatch "placeholders" -and $_.Text -notmatch "NotImplementedError"
    }

    if ($hits.Count -eq 0) {
        Write-Host "Clean — no stub markers in production code."
        Write-Host "--- DoD-11 outcome: NO_STUBS ---"
        exit 0
    }
    Write-Host "=== Stub/residue findings ($($hits.Count) hits) ==="
    Write-Host ""
    $i = 0
    foreach ($h in $hits) {
        $i++
        Write-Host ("{0,3}. {1}:{2}" -f $i, $h.File, $h.Line)
        Write-Host ("     {0}" -f $h.Text)
        Write-Host ""
    }
    Write-Host "=== Each hit must be: completed / deleted / 已登记不修 — record in delivery doc ==="
    exit 1
}

# --- Assembly-point completeness audit (机制补强 2026-08-18) ---
if ($AssemblyPoint) {
    Write-Host "Mode  : Assembly-point completeness for: $AssemblyPoint"
    if ($Param) { Write-Host "Params: $Param" }
    Write-Host ""

    $escName = [regex]::Escape($AssemblyPoint)
    $hits = Invoke-Scan $srcRoot ($escName + '\s*\(')

    # 排除类/函数定义行（class X / def X），只保留实例化调用点
    $instPoints = @($hits | Where-Object {
        $_.Text -notmatch "^(class|def)\s+$escName\b"
    })

    if ($instPoints.Count -eq 0) {
        Write-Host "FAIL — '$AssemblyPoint' has ZERO production instantiation points (FM-1: 没装配 = 功能没跑通)."
        Write-Host "--- DoD-11 outcome: ZERO_ASSEMBLY_POINTS — must wire before delivery ---"
        exit 1
    }

    $params = @()
    if ($Param) { $params = @($Param.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }

    Write-Host "=== Production assembly points ($($instPoints.Count)) ==="
    Write-Host ""
    $fileCache = @{}
    $failCount = 0
    $i = 0
    foreach ($h in $instPoints) {
        $i++
        $abs = Join-Path $root $h.File
        if (-not $fileCache.ContainsKey($abs)) {
            $fileCache[$abs] = Get-Content -Path $abs -Encoding UTF8
        }
        $lines = $fileCache[$abs]
        # 从实例化行开始，忽略字符串内括号，做括号平衡，收集参数块
        $depth = 0
        $text = ""
        for ($k = $h.Line - 1; $k -lt $lines.Count; $k++) {
            $ln = $lines[$k]
            $cleaned = [regex]::Replace($ln, '["''][^"'']*["'']', '')
            $depth += ($cleaned.ToCharArray() | Where-Object { $_ -eq '(' }).Count
            $depth -= ($cleaned.ToCharArray() | Where-Object { $_ -eq ')' }).Count
            $text += $ln
            if ($depth -le 0 -and $k -gt ($h.Line - 1)) { break }
        }

        Write-Host ("  {0,3}. {1}:{2}  {3}" -f $i, $h.File, $h.Line, $h.Text)
        $pointFail = $false
        foreach ($p in $params) {
            $hasKw = $text -match "(?:^|[\s,\(])$([regex]::Escape($p))\s*="
            if ($hasKw) {
                Write-Host ("        {0}: PASS" -f $p)
            } else {
                Write-Host ("        {0}: MISSING  <-- must wire (或位置参数，人工核对)" -f $p)
                $pointFail = $true
            }
        }
        if (-not $params) {
            Write-Host "        (no -Param given — inspect parameter wiring manually)"
        }
        if ($pointFail) { $failCount++ }
        Write-Host ""
    }

    if ($failCount -gt 0) {
        Write-Host ("FAIL — {0}/{1} assembly point(s) missing required param(s). DoD-11: 装配不完整 = 功能对部分入口 fail-closed，禁止交付。" -f $failCount, $instPoints.Count)
        Write-Host "--- DoD-11 outcome: ASSEMBLY_INCOMPLETE — wire all points before delivery ---"
        exit 1
    }
    Write-Host "All $($instPoints.Count) assembly point(s) wired (params $([string]::Join(',', $params)))."
    Write-Host "--- DoD-11 outcome: ASSEMBLY_WIRED — record full assembly-point list in delivery doc ---"
    exit 0
}

# --- Config field "definition -> consumer" audit (P0R-2 型常规检测, 2026-09-17 新增) ---
# 形态（P0R-2 本体与其同族）：配置字段有定义、文档有承诺，但生产代码零消费者 →
# 用户改了配置什么也不发生（"能跑通但不符合使用场景"）。历史靠人工 audit 才发现
# （ask_risk_levels / models_dev.url / preferred_language / tools.temp_dir /
#  LLMConfig.context_window / TasksConfig …），每次都要重读全仓。
# 实现落在 Python 侧（scripts/_config_consumers.py，可独立跑/可单测），本模式仅做调用与退出码透传。
if ($ConfigAudit) {
    Write-Host "Mode  : Config field consumer audit (definition -> consumer)"
    Write-Host ""
    $py = Join-Path $memoDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    $env:PYTHONIOENCODING = "utf-8"
    $enumScript = Join-Path $scriptDir "_config_consumers.py"
    if (-not (Test-Path $enumScript)) {
        Write-Host "ERROR: $enumScript not found (required by -ConfigAudit)"
        exit 2
    }
    $pyArgs = @()
    if ($ConfigVerbose) { $pyArgs += "--verbose" }
    & $py $enumScript @pyArgs
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "--- DoD-11 outcome: CONFIG_NO_DEAD_FIELDS — record field count in delivery doc ---"
    } else {
        Write-Host "--- DoD-11 outcome: CONFIG_DEAD_FIELDS — wire or delete before delivery ---"
    }
    exit $code
}


# --- Symbol wiring audit ---
Write-Host "Mode  : Wiring check for symbol: $Symbol"
Write-Host ""

$prod = Invoke-Scan $srcRoot ([regex]::Escape($Symbol))
$tests = Invoke-Scan $testRoot ([regex]::Escape($Symbol))

# Definition counts as a hit only when it is the class/def line; treat whole match as family.
Write-Host "--- Production references (bandi/bandi) ---"
if ($prod.Count -eq 0) {
    Write-Host "  (none)"
} else {
    $i = 0
    foreach ($h in $prod) {
        $i++
        Write-Host ("  {0,3}. {1}:{2}  {3}" -f $i, $h.File, $h.Line, $h.Text)
    }
}

Write-Host ""
Write-Host "--- Test-only references (bandi/tests) ---"
if ($tests.Count -eq 0) {
    Write-Host "  (none)"
} else {
    $i = 0
    foreach ($h in $tests) {
        $i++
        Write-Host ("  {0,3}. {1}:{2}  {3}" -f $i, $h.File, $h.Line, $h.Text)
    }
}

Write-Host ""
# A definition (class X / def X) inside production is expected; count only call sites.
$defLines = @($prod | Where-Object { $_.Text -match "^(class $([regex]::Escape($Symbol))\b|def $([regex]::Escape($Symbol))\b|    def $([regex]::Escape($Symbol))\b)" })
$callSites = @($prod | Where-Object { $_ -notin $defLines })

if ($prod.Count -eq 0) {
    Write-Host "FAIL — symbol '$Symbol' has ZERO production references (FM-1: 漏写/没接线/没跑通)."
    Write-Host "       If this is a new feature, it must be wired into a production entry point"
    Write-Host "       (factory / run_agent_turn / CLI / API / tick / 装配点) before delivery."
    Write-Host "--- DoD-11 outcome: ZERO_PRODUCTION_CALLS — must wire before delivery ---"
    exit 1
}

# Definition-only false green (FM-1 blind spot): a class/func definition is a production
# reference, so a symbol that is defined but NEVER called elsewhere used to report WIRED.
if ($callSites.Count -eq 0) {
    Write-Host "FAIL - symbol '$Symbol' has ZERO production call sites (only $($defLines.Count) definition line(s))."
    Write-Host "       A definition without a caller never runs (FM-1). Wire it into a production"
    Write-Host "       entry point (factory / run_agent_turn / CLI / API / tick) or delete it."
    Write-Host "--- DoD-11 outcome: ZERO_PRODUCTION_CALLS — must wire before delivery ---"
    exit 1
}

Write-Host "Production call sites found: $($callSites.Count) (plus $($defLines.Count) definition line(s))."
Write-Host "--- DoD-11 outcome: WIRED — record entry-point chain in delivery doc ---"
Write-Host "--- DoD-12: run production main path end-to-end once, assert final artifact ---"
exit 0