<#
.SYNOPSIS
  Bandi FM-8 same-family scan gate — enforces DoD-9 (同族清单免疫) + DoD-10 (契约族级覆盖) preparation.

.DESCRIPTION
  Scans bandi/bandi + bandi/tests for ALL occurrences of a pattern, producing a
  same-family instance list.  Every bug fix / new feature must run this script so a
  point-fix cannot silently miss sibling call sites (FM-8 same-family incomplete fix).

  Three use cases:
   1. 修 bug   : -Pattern <被修复的 API/模式> -> list all sibling call sites to annotate 已修/已登记/不适用
   2. 开发新功能: -Pattern <新增 API/模式>     -> find existing same-family implementations to reuse / extend
   3. 新增符号反向扫描 (机制补强 2026-08-18):
      -ReverseSync <符号1,符号2,...> -> 反向找出全仓所有「按名分组的注册容器」
      (白名单/守卫/注册表，如 _COLLABORATION_TOOLS / _WRITE_TOOLS / risk 表)，
      并输出「容器 × 新符号」覆盖矩阵。新增 N 个同类工具/API 时必跑，
      防止新增符号漏同步到既有注册点（本模式补齐 fm8-sweep 单向缺口：
      原 -Pattern 只从被修 API 出发找同族，无法发现「新增符号 → 既有容器需扩展」）。
      本模式尾部追加「反向冗余检测」(机制补强 2026-08-18, FM-21 免疫)：
      对每个容器的每个字符串成员，核对全仓是否在容器块之外存在引用
      (工具 name= 注册 / 守卫分支 / 注册表引用)。成员仅出现在自身容器内
      = DEAD-MEMBER 候选（如 _COLLABORATION_TOOLS 曾含 broadcast 死条目），
      输出供人工三判：删除 / 合法仅容器内使用 / 误报。

  Output is a checklist to record in the delivery doc (DoD-9). Zero hits is itself a
  finding — it means the pattern name is likely wrong or the family is truly empty.

  Exit: 0 always (read-only tool).

.EXAMPLE
  .\bandi\scripts\fm8-sweep.ps1 -Pattern "read_bytes" -Label "P1-9"
  .\bandi\scripts\fm8-sweep.ps1 -Pattern "_MAX_FILE_SIZE" -Scope source
  .\bandi\scripts\fm8-sweep.ps1 -Pattern "def close" -Include "bandi/company"
  .\bandi\scripts\fm8-sweep.ps1 -ReverseSync "send_dm,reply_message,route_work" -Label "OP-28"
#>
param(
    [Parameter(Mandatory = $false)][string]$Pattern,
    [Parameter(Mandatory = $false)][string]$ReverseSync,
    [string]$Label = "",
    [ValidateSet("source", "tests", "all")][string]$Scope = "all",
    [string]$Include = "",
    [switch]$ShowTests
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

if ((-not $Pattern -and -not $ReverseSync) -or ($Pattern -and $ReverseSync)) {
    Write-Host "ERROR: specify exactly one of -Pattern <pattern> or -ReverseSync <sym1,sym2,...>"
    exit 2
}

$scriptDir = Split-Path -Parent $PSCommandPath      # ...\bandi\scripts
$memoDir   = Split-Path -Parent $scriptDir          # ...\bandi
$root      = Split-Path -Parent $memoDir            # 项目根

# Resolve scan roots: bandi/bandi (source) + bandi/tests (tests)
$targets = @()
if ($Scope -in @("source", "all")) { $targets += (Join-Path $memoDir "bandi") }
if ($Scope -in @("tests", "all"))  { $targets += (Join-Path $memoDir "tests") }
if ($Include) { $targets = @($targets | ForEach-Object { Join-Path $_ $Include }) }

$excludeParts = @("__pycache__", ".venv", "node_modules", "\.pytest_cache")
$rg = Get-Command rg -ErrorAction SilentlyContinue

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

# 通用行扫描：rg 可用则用 rg，否则 Select-String fallback。
# 返回对象列表 @{ File(相对 root); Line; Text }
function Invoke-LineScan([string]$base, [string]$pattern) {
    $results = @()
    if (-not (Test-Path $base)) { return $results }
    if ($rg) {
        $lines = & $rg.Path --line-number --no-heading --glob "*.py" $pattern $base 2>$null
        foreach ($line in $lines) {
            if ($line -match '^(.*?):(\d+):(.*)$') {
                $results += [pscustomobject]@{
                    File = $Matches[1].Substring($root.Length + 1)
                    Line = [int]$Matches[2]
                    Text = $Matches[3].Trim()
                }
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
Write-Host " Bandi FM-8 Same-Family Scan"
Write-Host " $(Get-Date -Format 'yyyy/MM/dd HH:mm')"
Write-Host "========================================"
if ($Label) { Write-Host "Label  : $Label" }

$allFiles = @()
foreach ($t in $targets) { $allFiles += Get-ScanFiles $t }

# ---------------------------------------------------------------------------
# Mode 3: Reverse-sync (新增符号 → 按名注册容器覆盖矩阵)
# ---------------------------------------------------------------------------
if ($ReverseSync) {
    $symbols = @($ReverseSync.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    Write-Host "Mode   : ReverseSync (new symbols -> named registry containers)"
    Write-Host "Symbols: $($symbols -join ', ')"
    Write-Host "Scope  : $Scope"
    Write-Host "Scanning $($allFiles.Count) .py files for registry containers..."
    Write-Host ""

    # 容器定义行识别：大写蛇形常量（含下划线开头）= { 字面量，名字含注册/白名单/守卫语义 token
    $containerDef = '(?:^|\s)([A-Z_][A-Z0-9_]*(?:TOOLS|ALLOWED|WRITE|COLLAB|SET|MAP|REGISTRY|WHITELIST|BLACKLIST|RISK|ACTIONS|COMMANDS|NAMES|PERMISSIONS|TYPES|FLAGS|PARAMS)[A-Z0-9_]*)\s*=\s*\{\s*(?:#.*)?$'

    # 1) 定位所有容器定义行（rg 可用则用 -l 拿文件清单，否则全仓 Select-String）
    $containerFiles = @()
    if ($rg) {
        foreach ($t in $targets) {
            if (-not (Test-Path $t)) { continue }
            $containerFiles += & $rg.Path -l $containerDef $t 2>$null
        }
    } else {
        foreach ($f in $allFiles) {
            $found = Select-String -Path $f.FullName -Pattern $containerDef
            if ($found) { $containerFiles += $f.FullName }
        }
    }
    $containerFiles = $containerFiles | Select-Object -Unique

    # 2) 逐文件解析容器块，提取成员（字符串字面量）
    $blocks = @()
    foreach ($f in $containerFiles) {
        $rel = $f.Substring($root.Length + 1)
        $lines = Get-Content -Path $f -Encoding UTF8
        for ($i = 0; $i -lt $lines.Count; $i++) {
            $ln = $lines[$i]
            if ($ln -match $containerDef) {
                $name = $Matches[1]
                $depth = ($ln.ToCharArray() | Where-Object { $_ -eq '{' }).Count
                $depth -= ($ln.ToCharArray() | Where-Object { $_ -eq '}' }).Count
                $blockText = $ln
                $j = $i
                while ($depth -gt 0 -and $j -lt $lines.Count - 1) {
                    $j++
                    $blockText += "`n" + $lines[$j]
                    $depth += ($lines[$j].ToCharArray() | Where-Object { $_ -eq '{' }).Count
                    $depth -= ($lines[$j].ToCharArray() | Where-Object { $_ -eq '}' }).Count
                }
                $members = @([regex]::Matches($blockText, '["'']([^"'']+)["'']') | ForEach-Object { $_.Groups[1].Value })
                $blocks += [pscustomobject]@{
                    Name      = $name
                    File      = $rel
                    StartLine = $i + 1
                    EndLine   = $j + 1
                    Members   = @($members)
                }
                $i = $j
            }
        }
    }

    Write-Host "Found $($blocks.Count) named registry container(s)."
    Write-Host ""

    # 3) 覆盖矩阵：每个新符号 × 每个容器
    Write-Host "=== Reverse-sync coverage matrix (new symbol x registry container) ==="
    Write-Host ""
    $matrix = @()
    foreach ($sym in $symbols) {
        $row = [pscustomobject]@{ Symbol = $sym; Containers = @(); AnyWhere = $false }
        foreach ($b in $blocks) {
            if ($b.Members -contains $sym) {
                $row.Containers += $b.Name
                $row.AnyWhere = $true
            }
        }
        $matrix += $row
    }
    foreach ($row in $matrix) {
        if ($row.AnyWhere) {
            Write-Host ("  [x] {0,-32} containers: {1}" -f $row.Symbol, ($row.Containers -join ', '))
        } else {
            Write-Host ("  [ ] {0,-32} <no registry container references it> — 判断是否需要同步到既有注册点" -f $row.Symbol)
        }
    }

    Write-Host ""
    Write-Host "=== Registry container member coverage (缺漏 = 需同步) ==="
    Write-Host ""
    foreach ($b in ($blocks | Sort-Object File, StartLine)) {
        $memberCount = @($b.Members).Count
        $hasAny = $false
        $missing = @()
        foreach ($sym in $symbols) {
            if ($b.Members -contains $sym) { $hasAny = $true } else { $missing += $sym }
        }
        $tag = if ($hasAny -and $missing.Count -eq 0) { "COVERED" } else { "NEEDS-SYNC" }
        Write-Host ("  {0,-10} {1} @ {2}:{3}  ({4} members)" -f $tag, $b.Name, $b.File, $b.StartLine, $memberCount)
        Write-Host ("      has: {0}" -f (($b.Members | Where-Object { $symbols -contains $_ }) -join ', '))
        if ($missing.Count -gt 0) {
            Write-Host ("      MISSING (new symbol(s) not in this registry): {0}" -f ($missing -join ', '))
        }
        Write-Host ""
    }

    # 4) 非容器引用：新符号出现在哪些普通文件
    Write-Host "=== Plain references of each new symbol (non-container) ==="
    foreach ($sym in $symbols) {
        $hits = @()
        foreach ($t in $targets) {
            foreach ($h in (Invoke-LineScan $t ([regex]::Escape($sym)))) {
                $hits += ("{0}:{1}" -f $h.File, $h.Line)
            }
        }
        $hits = $hits | Sort-Object -Unique
        if ($hits.Count -eq 0) {
            Write-Host ("  {0}: <no source/tests reference> — 符号可能拼写错误或尚未接线" -f $sym)
        } else {
            Write-Host ("  {0}: {1}" -f $sym, ($hits -join ', '))
        }
    }

    Write-Host ""
    Write-Host "=== For each NEEDS-SYNC registry: annotate 已同步 / 已登记 / 不适用 — record in delivery doc (DoD-9) ==="
    Write-Host "=== DoD-10: for each synchronized family group, contract must parametrize ALL members (N tools -> N rows) ==="

    # 5) 反向冗余检测：容器成员 → 容器块之外必须存在引用
    #    成员仅出现在自身容器内 = DEAD-MEMBER 候选（死条目，如 broadcast）。
    #    按容器语义分两档：符号注册容器（工具/命令名，成员应对应实体）高优先核对；
    #    数据值容器（SET/MAP/NAMES/FLAGS 等，成员本就是数据值）低优先仅列出。
    Write-Host ""
    Write-Host "=== Reverse redundancy check (container member must be referenced outside its block) ==="
    Write-Host ""
    $uniqueMembers = @($blocks | ForEach-Object { $_.Members } | Select-Object -Unique)
    $dead = @()
    foreach ($m in $uniqueMembers) {
        $quoted = '["'']' + [regex]::Escape($m) + '["'']'
        $outsideHits = @()
        foreach ($t in $targets) {
            foreach ($h in (Invoke-LineScan $t $quoted)) {
                $inBlock = $false
                foreach ($b in $blocks) {
                    if ($b.File -eq $h.File -and $h.Line -ge $b.StartLine -and $h.Line -le $b.EndLine) {
                        $inBlock = $true
                        break
                    }
                }
                if (-not $inBlock) { $outsideHits += $h }
            }
        }
        if ($outsideHits.Count -eq 0) {
            $owning = @($blocks | Where-Object { $_.Members -contains $m } | ForEach-Object { "$($_.Name)@$($_.File):$($_.StartLine)" })
            $dead += [pscustomobject]@{ Member = $m; Container = ($owning -join ', '); Refs = @($outsideHits).Count }
        }
    }
    if ($dead.Count -eq 0) {
        Write-Host "  No DEAD-MEMBER candidates — every container member has at least one reference outside its own block."
        Write-Host ""
    } else {
        # 符号注册容器：名字含工具/白名单/注册语义。
        # 注意：不含 COMMANDS —— _GIT_READ_ONLY_SUBCOMMANDS 是 git 子命令数据白名单（成员是数据值）
        $symContainerDef = '(?:TOOLS|ALLOWED|WRITE|COLLAB|WHITELIST|BLACKLIST|REGISTRY|ACTIONS|PERMISSIONS)'
        $symDead  = @($dead | Where-Object { $_.Container -match $symContainerDef })
        $dataDead = @($dead | Where-Object { $_.Container -notmatch $symContainerDef })

        if ($symDead.Count -gt 0) {
            Write-Host "  [HIGH] 符号注册容器 DEAD-MEMBER candidates (member should map to a real entity):"
            Write-Host ""
            foreach ($d in ($symDead | Sort-Object Container, Member)) {
                Write-Host ("  [!] {0,-32} in {1}  — 0 references outside block" -f $d.Member, $d.Container)
            }
            Write-Host ""
            Write-Host "  NOTE: manual triage per candidate — [删除] if no entity owns the name / [合法] if purely internal"
            Write-Host "        enum value used via member access / [误报] if referenced dynamically. Record verdict in delivery doc (DoD-9)."
            Write-Host ""
        }
        if ($dataDead.Count -gt 0) {
            Write-Host "  [LOW] 数据值容器 DEAD-MEMBER candidates (SET/MAP/NAMES/FLAGS — members are data values, usually legit:"
            Write-Host "        git subcommands, ANSI codes, prefix maps, reserved names):"
            Write-Host ""
            foreach ($d in ($dataDead | Sort-Object Container, Member)) {
                Write-Host ("  [i] {0,-32} in {1}  — 0 literal refs outside block" -f $d.Member, $d.Container)
            }
            Write-Host ""
        }
    }

    exit 0
}

# ---------------------------------------------------------------------------
# Mode 1/2: -Pattern scan
# ---------------------------------------------------------------------------
Write-Host "Pattern: $Pattern"
Write-Host "Scope  : $Scope"
Write-Host ""
Write-Host "Scanning $($allFiles.Count) .py files..."
Write-Host ""

$hits = @()
foreach ($t in $targets) {
    $hits += Invoke-LineScan $t $Pattern
}
$hits = $hits | Sort-Object File, Line

if ($hits.Count -eq 0) {
    Write-Host "No hits for '$Pattern'."
    Write-Host "NOTE: zero hits is itself a finding — verify the pattern name is correct,"
    Write-Host "      or that the family is truly empty (record 不适用 in delivery doc)."
    Write-Host ""
    Write-Host "--- DoD-9 outcome: NO_SIBLINGS (record in delivery doc) ---"
    exit 0
}

Write-Host "=== Same-family instance list ($($hits.Count) hits) ==="
Write-Host ""
$i = 0
foreach ($h in $hits) {
    $i++
    Write-Host ("{0,3}. {1}:{2}" -f $i, $h.File, $h.Line)
    Write-Host ("     {0}" -f $h.Text)
    Write-Host ""
}
Write-Host "=== For each hit annotate: [已修] [已登记] [不适用] — record in delivery doc (DoD-9) ==="
Write-Host "=== DoD-10: for each annotated 已修/已登记 group, list ALL production call paths — one contract per family ==="
exit 0