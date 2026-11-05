<#
.SYNOPSIS
  Bandi start-of-day gate script — enforces §1-§4 discipline before coding.
.DESCRIPTION
  Checks git state, reads plan log, and prints the launch checklist.
  The AI must present this card to the user and get confirmation before coding.
  Exit: 0 = all checks passed / non-zero = block
#>

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$scriptPath = $PSCommandPath  # e.g. C:\...\bandi\scripts\verify-start.ps1
$scriptDir = Split-Path -Parent $scriptPath             # C:\...\bandi\scripts
$memoDir   = Split-Path -Parent $scriptDir              # C:\...\bandi
$root      = Split-Path -Parent $memoDir                # C:\...\项目根
Set-Location $root

$global:problems = @()

function Check-Step($name, $scriptBlock) {
    try {
        & $scriptBlock
        Write-Host "  [OK] $name"
    } catch {
        Write-Host "  [!!] $name -- $_"
        $global:problems += $_
    }
}

# Resolve Chinese-named paths — use .NET APIs for encoding safety
$deliveryDir = [System.IO.Path]::Combine($root, "交付文档收集")
$planLog = [System.IO.Path]::Combine($root, "学习计划与执行日志.md")

Write-Host "`n========================================"
Write-Host " Bandi Start-of-Day Gate "
Write-Host " $(Get-Date -Format 'yyyy/MM/dd HH:mm')"
Write-Host "========================================`n"

# 1. git working tree clean
Check-Step "git working tree clean" {
    $s = git status --porcelain
    if ($s) { throw "uncommitted changes:`n$s" }
}

# 2. find last completed Day (name-sort aware of letter suffixes like 122a/l2/o)
Check-Step "last delivery doc readable" {
    if (-not [System.IO.Directory]::Exists($deliveryDir)) { throw "delivery dir not found: $deliveryDir" }
    $docs = [System.IO.Directory]::GetFiles($deliveryDir, "Day*-交付文档.md")
    if ($docs.Length -eq 0) { throw "no delivery docs found in $deliveryDir" }
    $latest = $docs | ForEach-Object {
        $base = [System.IO.Path]::GetFileNameWithoutExtension($_) -replace '-交付文档$', ''
        if ($base -match '^Day(\d+)([a-z0-9]*)(?:-.*)?$') {
            [pscustomobject]@{ Path = $_; Num = [int]$Matches[1]; Suffix = $Matches[2] }
        }
    } | Sort-Object Num, Suffix -Descending | Select-Object -First 1
    Write-Host "       latest doc: $([System.IO.Path]::GetFileName($latest.Path))"
}

Write-Host "`n--- Required Reading Checklist ---"
Write-Host ""

# 3. check plan log
if ([System.IO.File]::Exists($planLog)) {
    Write-Host "  [ ] Step 0 - Read 学习计划与执行日志.md, confirm Day and Phase"
} else {
    Write-Host "  [!!] 学习计划与执行日志.md not found"
    $global:problems += "missing 学习计划与执行日志.md"
}

# 4. print reminder list
Write-Host "  [ ] Step 1 - Read docs/架构设计细节.md 涉及 §节"
Write-Host "  [ ] Step 2 - Read target module ALL source files"
Write-Host "  [ ] Step 3 - Check docs/Harness工程差距分析与改进.md P0 items"
Write-Host "  [ ] Step 4 - Check docs/技术债务清单.md 涉及项"
Write-Host "  [ ] Step 5 - Read reference source modules (per ADR-16)"
Write-Host "  [ ] Step 6 - Load relevant skills (per §2 table)"
Write-Host "  [ ] Step 7 - git working tree clean"
Write-Host "  [§6b] Step 8 - 涉及修复/新功能已跑 .\bandi\scripts\fm8-sweep.ps1 产出同族清单（DoD-9）"
Write-Host "  [§6b] Step 9 - 涉及契约已含族级覆盖声明（DoD-10：列出全部生产调用路径）"
Write-Host ""

Write-Host "--- Gate Decision ---"
if ($global:problems.Count -gt 0) {
    Write-Host "  BLOCKED - $($global:problems.Count) problem(s):"
    foreach ($p in $global:problems) { Write-Host "    - $p" }
    exit 1
}

Write-Host "  All pre-checks passed."
Write-Host "  Present this checklist card to user."
Write-Host "  Do NOT start coding until all [ ] items are [x] and user confirms."
Write-Host ""
Write-Host "  (any item skipped with user override -> record in delivery doc)"
exit 0
